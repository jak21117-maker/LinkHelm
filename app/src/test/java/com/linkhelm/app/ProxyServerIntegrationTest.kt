package com.linkhelm.app

import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.Closeable
import java.io.EOFException
import java.io.InputStream
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.net.SocketException
import java.net.SocketTimeoutException
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.concurrent.thread

class ProxyServerIntegrationTest {
    private val resources = mutableListOf<Closeable>()
    private val servers = mutableListOf<ProxyServer>()

    @After
    fun tearDown() {
        resources.reversed().forEach {
            try {
                it.close()
            } catch (_: Exception) {
            }
        }
        servers.reversed().forEach {
            it.stop()
            assertTrue("Proxy workers did not stop", it.awaitStopped())
        }
    }

    @Test
    fun connectionBeyondCoreWorkerStartsImmediately() {
        val echo = TcpEchoServer().also { resources.add(it) }
        val proxy = startProxy(coreThreads = 1, maxThreads = 4, perClient = 4)

        val first = openTcpTunnel(proxy.localPort(), echo.port).also { resources.add(it) }
        assertEcho(first, "first")

        val second = openTcpTunnel(proxy.localPort(), echo.port, "127.0.0.2").also { resources.add(it) }
        assertEcho(second, "second")

        val metrics = proxy.metrics()
        assertEquals(2, metrics.activeConnections)
        assertEquals(2, metrics.activeTcpSessions)
        assertTrue("Executor never grew beyond its core worker", metrics.executorPoolSize >= 2)
        assertEquals(0, metrics.rejectedConnections)
    }

    @Test
    fun perClientAndGlobalLimitsPreserveCapacityAndReleaseItAfterDisconnect() {
        val echo = TcpEchoServer().also { resources.add(it) }
        val proxy = startProxy(coreThreads = 1, maxThreads = 2, perClient = 1)

        val first = openTcpTunnel(proxy.localPort(), echo.port).also { resources.add(it) }
        assertEcho(first, "held-open")

        val rejected = Socket("127.0.0.1", proxy.localPort()).also { resources.add(it) }
        rejected.soTimeout = 2_000
        rejected.getOutputStream().write(byteArrayOf(5, 1, 0))
        assertSocks5Refused(rejected)
        assertTrue(awaitCondition { proxy.metrics().rejectedConnections == 1 })

        val secondDevice = openTcpTunnel(proxy.localPort(), echo.port, "127.0.0.2").also { resources.add(it) }
        assertEcho(secondDevice, "second-device")

        val globallyRejected = connectToProxy(proxy.localPort(), "127.0.0.3").also { resources.add(it) }
        globallyRejected.soTimeout = 2_000
        globallyRejected.getOutputStream().write(byteArrayOf(5, 1, 0))
        assertSocks5Refused(globallyRejected)
        assertTrue(awaitCondition { proxy.metrics().rejectedConnections == 2 })

        first.close()
        assertTrue(awaitCondition { proxy.metrics().activeByClient["127.0.0.1"] == null })
        assertEcho(secondDevice, "survives-first-device-disconnect")

        val replacement = openTcpTunnel(proxy.localPort(), echo.port).also { resources.add(it) }
        assertEcho(replacement, "replacement")
        assertEquals(1, proxy.metrics().activeByClient["127.0.0.1"] ?: 0)
        assertEquals(1, proxy.metrics().activeByClient["127.0.0.2"] ?: 0)
    }

    /**
     * beta1.0.51: ONE shared session budget — TCP sessions and UDP
     * associations draw from the SAME per-device quota (user contract:
     * TCP 和 UDP 不设比例). Each association costs TWO slots (its control
     * connection + the association itself). With perClient=6, 2 TCP tunnels +
     * 2 UDP associations exhaust the device's 6 slots; the next connection
     * must be refused at accept time.
     */
    @Test
    fun tcpAndUdpSessionsShareOnePerDeviceBudget() {
        val echo = TcpEchoServer().also { resources.add(it) }
        val udpEcho = UdpEchoServer().also { resources.add(it) }
        val proxy = startProxy(coreThreads = 1, maxThreads = 8, perClient = 6)

        val tcp1 = openTcpTunnel(proxy.localPort(), echo.port).also { resources.add(it) }
        val tcp2 = openTcpTunnel(proxy.localPort(), echo.port).also { resources.add(it) }
        val udp1 = openUdpAssociation(proxy.localPort()).also { resources.add(it) }
        val udp2 = openUdpAssociation(proxy.localPort()).also { resources.add(it) }
        assertEcho(tcp1, "tcp-1")
        assertEcho(tcp2, "tcp-2")
        assertUdpEcho(DatagramSocket().apply { soTimeout = 2_000 }.also { resources.add(it) }, udp1.relayPort, udpEcho.port, "udp-1")

        // 2 TCP + 2 associations (2 control + 2 association slots) = the whole
        // 6-slot per-device budget; the next connection is refused at accept.
        assertTrue(awaitCondition { proxy.metrics().activeSessions == 6 })
        val refused = connectToProxy(proxy.localPort()).also { resources.add(it) }
        try {
            refused.soTimeout = 2_000
            refused.getOutputStream().write(byteArrayOf(5, 1, 0))
            refused.getOutputStream().flush()
            assertSocks5Refused(refused)
        } finally {
            refused.close()
        }
        assertTrue(awaitCondition { proxy.metrics().rejectedConnections == 1 })

        // Freeing a TCP slot re-opens exactly one shared slot.
        tcp2.close()
        assertTrue(awaitCondition { proxy.metrics().activeSessions == 5 })
        val admitted = openTcpTunnel(proxy.localPort(), echo.port).also { resources.add(it) }
        assertEcho(admitted, "after-release")
    }

    /** beta1.0.51: per-device quota = shared dynamic quota for ≤2 devices,
     *  fair share of the shared session budget (floored at 200 — 资源占满时
     *  最少两百) for ≥3. */
    @Test
    fun dynamicPerClientQuotaSwitchesAtThreeDevices() {
        val proxy = startProxy(coreThreads = 1, maxThreads = 480, perClient = 200).also { servers.add(it) }
        assertEquals(200, proxy.perClientQuotaFor(1))
        assertEquals(200, proxy.perClientQuotaFor(2))
        // fair share: 480/3 = 160 < 200 floor → the floor binds
        assertEquals(200, proxy.perClientQuotaFor(3))
        // fair share 480/10 = 48 < 200 floor
        assertEquals(200, proxy.perClientQuotaFor(10))
        // Small explicit configs stay authoritative for ≤2 devices (test harness
        // relies on this): the fair-share floor must never RAISE a smaller base.
        val small = startProxy(coreThreads = 1, maxThreads = 4, perClient = 4).also { servers.add(it) }
        assertEquals(4, small.perClientQuotaFor(2))
        assertEquals(4, small.perClientQuotaFor(3))
    }

    /**
     * beta1.0.51: extreme congestion (≥95% of the SHARED session budget)
     * tightens NEW-session per-device admission to 300 (拥堵时 300); it must
     * overlay (never raise) the normal tiers and disappear entirely when
     * congestion clears.
     */
    @Test
    fun congestionModeTightensNewConnectionQuotaAndRecovers() {
        val proxy = ProxyServer(
            ProxyServer.Config(
                port = 0,
                coreThreads = 1,
                maxThreads = 1216,
                maxConnectionsPerClient = 400,
                maxTcpSessions = 1200,
                maxTcpSessionsPerClient = 400,
                workerKeepAliveMs = 500,
                connectTimeoutMs = 2_000,
                handshakeTimeoutMs = 2_000,
                socketPollIntervalMs = 100,
                tcpIdleTimeoutMs = 2_000,
                udpTargetTimeoutMs = 500,
                udpIdleTimeoutMs = 2_000,
                udpCleanupIntervalMs = 100
            )
        ).also { servers.add(it) }
        proxy.start()
        assertTrue("Proxy did not bind", proxy.awaitStarted())
        // Normal path untouched when the budget is healthy: the shared dynamic
        // quota (starting value 375).
        assertEquals(375, proxy.perClientQuotaFor(1))

        // Congestion: every big-profile tier is capped by the 300 overlay.
        proxy.congestionActive.set(true)
        assertEquals(300, proxy.perClientQuotaFor(1))
        // 3 devices: fair share 1200/3 = 400, inside the 375 cap → 375; overlay → 300.
        assertEquals(300, proxy.perClientQuotaFor(3))

        // Pressure clears: the dynamic quota returns.
        proxy.congestionActive.set(false)
        assertEquals(375, proxy.perClientQuotaFor(3))

        // Overlay applies via minOf(base, 300): it can never RAISE a smaller
        // configured base (small test configs keep 4).
        val small = startProxy(coreThreads = 1, maxThreads = 4, perClient = 4).also { servers.add(it) }
        small.congestionActive.set(true)
        assertEquals(4, small.perClientQuotaFor(1))
    }

    /**
     * beta1.0.51: TURBO uses the SAME shared dynamic per-device quota
     * (350-400, starting 375) as STANDARD — one session budget, no TCP:UDP
     * ratio — with a 1400 global total; ≥3 devices share it fairly (floor
     * 200) and congestion overlays 300.
     */
    @Test
    fun turboProfileSharesOneSessionBudget() {
        val proxy = ProxyServer(
            ProxyServer.Config(
                port = 0,
                coreThreads = 1,
                maxThreads = ProxyServer.TURBO_MAX_THREADS,
                maxConnectionsPerClient = ProxyServer.PER_DEVICE_MAX,
                maxTcpSessions = 1400,
                maxTcpSessionsPerClient = ProxyServer.PER_DEVICE_MAX,
                workerKeepAliveMs = 500,
                connectTimeoutMs = 2_000,
                handshakeTimeoutMs = 2_000,
                socketPollIntervalMs = 100,
                tcpIdleTimeoutMs = 2_000,
                udpTargetTimeoutMs = 500,
                udpIdleTimeoutMs = 2_000,
                udpCleanupIntervalMs = 100
            )
        ).also { servers.add(it) }
        proxy.start()
        assertTrue("Proxy did not bind", proxy.awaitStarted())
        assertEquals(ProxyServer.PER_DEVICE_START, proxy.dynamicPerDeviceQuota) // starting value
        assertEquals(ProxyServer.PER_DEVICE_START, proxy.perClientQuotaFor(1))

        // Sampler steps are bounded and slow (±5 per 30s, 350-400).
        proxy.dynamicPerDeviceQuota = 390
        assertEquals(390, proxy.perClientQuotaFor(1))

        // 5 devices: fair share 1400/5 = 280, inside the dynamic cap.
        assertEquals(280, proxy.perClientQuotaFor(5))

        // Congestion overlays 300 — but never below the fair-share result.
        proxy.congestionActive.set(true)
        assertEquals(300, proxy.perClientQuotaFor(1))
        assertEquals(280, proxy.perClientQuotaFor(5))
    }

    /**
     * v1.5.0: the turbo toggle must resize the RUNNING pool immediately —
     * no share stop/start (user contract "manual toggle only" + "immediate effect").
     */
    @Test
    fun turboToggleResizesRunningPoolImmediately() {
        val proxy = startProxy(
            coreThreads = 1, maxThreads = ProxyServer.NORMAL_MAX_THREADS, perClient = 200
        ).also { servers.add(it) }
        val pool = proxy.clientPool!!
        assertEquals(ProxyServer.NORMAL_MAX_THREADS, proxy.config.maxThreads)
        assertEquals(ProxyServer.NORMAL_MAX_THREADS, pool.maximumPoolSize)

        proxy.applyTurboMode(true)
        assertEquals(ProxyServer.TURBO_MAX_THREADS, proxy.config.maxThreads)
        assertEquals(ProxyServer.TURBO_MAX_THREADS, pool.maximumPoolSize)

        proxy.applyTurboMode(false)
        assertEquals(ProxyServer.NORMAL_MAX_THREADS, proxy.config.maxThreads)
        assertEquals(ProxyServer.NORMAL_MAX_THREADS, pool.maximumPoolSize)

        // Idempotent toggle is a no-op.
        proxy.applyTurboMode(false)
        assertEquals(ProxyServer.NORMAL_MAX_THREADS, proxy.config.maxThreads)
    }

    /**
     * v1.5.2 (LinkHelm dual-lane): the standby port is a second door into the
     * SAME house — both lanes serve CONNECT through one executor and one
     * per-IP quota, and a standby bind failure degrades to single-port mode
     * without ever touching the primary.
     */
    @Test
    fun secondaryLaneServesConnectAndSharesQuotaWithPrimary() {
        val echo = TcpEchoServer().also { resources.add(it) }
        val proxy = startProxy(
            coreThreads = 1, maxThreads = 4, perClient = 3, secondaryPort = 0 // port 0 → OS-assigned standby
        )

        assertTrue(
            "standby lane must bind when secondaryPort >= 0",
            awaitCondition { proxy.secondaryListeningPort > 0 }
        )
        val standby = proxy.secondaryListeningPort
        assertNotEquals(proxy.localPort(), standby)

        // Both lanes proxy CONNECT to the same echo target (echo is verified
        // by reading the payload back through the tunnel).
        val viaPrimary = openTcpTunnel(proxy.localPort(), echo.port)
        assertEcho(viaPrimary, "lane-a")

        val viaStandby = openTcpTunnel(standby, echo.port)
        assertEcho(viaStandby, "lane-b")

        // Single shared quota: the 3-per-IP ceiling counts BOTH lanes
        // together — the 4th connection (any lane) must be refused. v1.5.3:
        // the refusal is the standard SOCKS5 method-failure reply
        // (\x05\xff) followed by a close, so tun2socks sees a protocol-level
        // rejection instead of a forced RST.
        val c3 = openTcpTunnel(standby, echo.port)
        val c4 = connectToProxy(proxy.localPort())
        try {
            c4.soTimeout = 2_000
            c4.getOutputStream().write(byteArrayOf(5, 1, 0))
            c4.getOutputStream().flush()
            assertSocks5Refused(c4)
        } finally {
            c4.close(); c3.close(); viaStandby.close(); viaPrimary.close()
        }
    }

    @Test
    fun quotaWaitAdmitsConnectionWhenSlotFreesQuickly() {
        // v1.5.32: a quota rejection inside quotaWaitMs becomes a bounded
        // wait, so a burst connection is admitted once a slot frees instead
        // of being refused (refusals count as lane failures on the Windows
        // router and short bursts used to degrade both lanes).
        val echo = TcpEchoServer().also { resources.add(it) }
        val proxy = startProxy(coreThreads = 1, maxThreads = 4, perClient = 1, quotaWaitMs = 1_500)

        val first = openTcpTunnel(proxy.localPort(), echo.port)
        // Free the only per-IP slot shortly after the second connection
        // arrives — the waiter must be admitted, not refused.
        thread(start = true) {
            Thread.sleep(300)
            first.close()
        }
        val second = connectToProxy(proxy.localPort())
        try {
            second.soTimeout = 4_000
            negotiate(second)
            val request = byteArrayOf(5, 1, 0, 1, 127, 0, 0, 1) + portBytes(echo.port)
            second.getOutputStream().write(request)
            second.getOutputStream().flush()
            val reply = readExactly(second.getInputStream(), 10)
            assertEquals("waited connection must be admitted after slot frees", 0, reply[1].toInt())
            assertEcho(second, "waited")
        } finally {
            second.close()
        }
        assertTrue(awaitCondition { proxy.metrics().rejectedConnections == 0 })
    }

    @Test
    fun standbyBindConflictDegradesToSingleLaneWithoutHurtingPrimary() {
        // Occupy a port so the standby listener's bind must fail; the primary
        // (OS-assigned) keeps serving and the service stays up.
        val blocker = ServerSocket(0)
        try {
            val proxy = startProxy(
                coreThreads = 1, maxThreads = 4, perClient = 4, secondaryPort = blocker.localPort
            )
            assertTrue(proxy.secondaryListeningPort == -1)
            val echo = TcpEchoServer().also { resources.add(it) }
            val tunnel = openTcpTunnel(proxy.localPort(), echo.port)
            assertEcho(tunnel, "primary-still-fine")
            tunnel.close()
            // The degrade path must leave the whole service running.
            assertTrue(awaitCondition { proxy.metrics().activeConnections == 0 })
        } finally {
            blocker.close()
        }
    }

    /**
     * v1.4.9 (regression report §4.1): the bind-time loopback self-test must
     * never inflate the quota device count — self-test + 2 real devices used to
     * silently drop the per-IP ceiling to 150.
     */
    @Test
    fun loopbackSelfTestDoesNotCountTowardQuotaDevices() {
        val echo = TcpEchoServer().also { resources.add(it) }
        val proxy = startProxy(coreThreads = 1, maxThreads = 8, perClient = 8)
        val loop = openTcpTunnel(proxy.localPort(), echo.port).also { resources.add(it) }
        assertEcho(loop, "loopback")
        assertTrue(awaitCondition { proxy.metrics().activeByClient["127.0.0.1"] != null })
        assertEquals(0, proxy.quotaDeviceCount("127.0.0.5", true))
        assertEquals(1, proxy.quotaDeviceCount("192.168.49.9", true))
        assertEquals(0, proxy.quotaDeviceCount("192.168.49.9", false))
    }

    /** beta1.0.51: compat is a NO-OP switch now — it must NOT change capacity,
     *  and it no longer gates UDP behaviour (multi-port tracking is always on). */
    @Test
    fun compatModeNoLongerChangesCapacityProfile() {
        val proxy = startProxy(coreThreads = 1, maxThreads = ProxyServer.NORMAL_MAX_THREADS, perClient = 160)
        val sessionsBefore = proxy.config.maxTcpSessions
        val threadsBefore = proxy.config.maxThreads
        proxy.applyCompatMode(true)
        assertEquals(sessionsBefore, proxy.config.maxTcpSessions)
        assertEquals(threadsBefore, proxy.config.maxThreads)
        assertTrue(proxy.compatMode)
        proxy.applyCompatMode(false)
        assertFalse(proxy.compatMode)
    }

    @Test
    fun udpAssociationsRemainIndependentAndShutdownCleansEverything() {
        val udpEcho = UdpEchoServer().also { resources.add(it) }
        val proxy = startProxy(coreThreads = 1, maxThreads = 4, perClient = 4)

        val first = openUdpAssociation(proxy.localPort()).also { resources.add(it) }
        val second = openUdpAssociation(proxy.localPort()).also { resources.add(it) }
        val firstUdp = DatagramSocket().apply { soTimeout = 2_000 }.also { resources.add(it) }
        val secondUdp = DatagramSocket().apply { soTimeout = 2_000 }.also { resources.add(it) }

        assertUdpEcho(firstUdp, first.relayPort, udpEcho.port, "alpha")
        assertUdpEcho(secondUdp, second.relayPort, udpEcho.port, "beta")

        firstUdp.send(
            DatagramPacket(byteArrayOf(0, 0, 1, 1), 4, InetAddress.getByName("127.0.0.1"), first.relayPort)
        )
        assertUdpEcho(firstUdp, first.relayPort, udpEcho.port, "after-malformed")
        // beta1.0.51: an ALTERNATE source port of the SAME client IP is now
        // ACCEPTED (always-on multi-port tracking) and its reply is routed
        // precisely back to that port — this is what fixes "UDP check OK but
        // the game gets nothing" for clients that rotate source ports
        // (hev per-PCB sockets, sing-box/Xray pools, power-saving rebuilds).
        assertUdpEcho(secondUdp, first.relayPort, udpEcho.port, "rotated-port")
        // The security boundary is the client IP, not the port: a datagram
        // from a DIFFERENT IP is still dropped.
        assertUdpForeignIpDropped(first.relayPort, udpEcho.port)
        assertUdpEcho(firstUdp, first.relayPort, udpEcho.port, "still-isolated")
        assertEquals(2, proxy.metrics().activeUdpAssociations)

        first.close()
        assertTrue(awaitCondition { proxy.metrics().activeUdpAssociations == 1 })
        assertUdpEcho(secondUdp, second.relayPort, udpEcho.port, "survives-first-association-disconnect")
        second.close()
        assertTrue(awaitCondition { proxy.metrics().activeUdpAssociations == 0 })

        proxy.stop()
        assertTrue(proxy.awaitStopped())
        assertTrue(awaitCondition { proxy.metrics().activeConnections == 0 })
        assertEquals(0, proxy.metrics().activeTcpSessions)
        assertEquals(0, proxy.metrics().activeUdpAssociations)
    }

    /**
     * beta1.0.52 ROOT-CAUSE regression: the UDP ASSOCIATE reply must advertise a
     * REACHABLE BND.ADDR — never 0.0.0.0. The old relay-socket construction
     * (`DatagramSocket()` then `bind(...)`) always threw "Already bound", fell
     * back to a wildcard socket and replied BND=0.0.0.0. hev-socks5-tunnel then
     * connect()s its UDP socket to 0.0.0.0 — Linux tolerates it, Windows refuses
     * with WSAEADDRNOTAVAIL (10049) — so EVERY tunneled UDP session (all DNS)
     * died while the direct relay probe still answered. This test replicates
     * hev's exact flow: connect -> auth -> ASSOCIATE -> parse BND -> UDP
     * connect(BND) -> relayed datagram.
     */
    @Test
    fun udpAssociateRepliesReachableBndAddrAndRelayWorksWithhevStyleConnect() {
        val udpEcho = UdpEchoServer().also { resources.add(it) }
        val proxy = startProxy(coreThreads = 1, maxThreads = 4, perClient = 4)

        val control = Socket("127.0.0.1", proxy.localPort()).also { resources.add(it) }
        control.soTimeout = 2_000
        negotiate(control)
        control.getOutputStream().write(byteArrayOf(5, 3, 0, 1, 0, 0, 0, 0, 0, 0))
        control.getOutputStream().flush()
        val reply = readExactly(control.getInputStream(), 10)
        assertEquals("ASSOCIATE failed", 0, reply[1].toInt())
        assertEquals("BND must be ATYP=IPv4", 1, reply[3].toInt())
        val bndAddr = InetAddress.getByAddress(reply.copyOfRange(4, 8))
        val bndPort = ((reply[8].toInt() and 0xFF) shl 8) or (reply[9].toInt() and 0xFF)
        assertFalse(
            "BND.ADDR must never be 0.0.0.0 (hev on Windows cannot connect to it)",
            bndAddr.isAnyLocalAddress
        )
        assertEquals(
            "BND.ADDR must be the address the client faces (the control connection's local address)",
            "127.0.0.1", bndAddr.hostAddress
        )

        // Exactly what hev does: bind-free UDP socket, connect() to the advertised BND.
        val datagram = DatagramSocket().apply { soTimeout = 2_000 }.also { resources.add(it) }
        datagram.connect(bndAddr, bndPort)
        val payload = "hev-style".toByteArray()
        val request = Socks5Parser.buildUdpHeader(InetAddress.getByName("127.0.0.1"), udpEcho.port) + payload
        datagram.send(DatagramPacket(request, request.size))
        val buffer = ByteArray(1024)
        val response = DatagramPacket(buffer, buffer.size)
        datagram.receive(response)
        val target = Socks5Parser.parseUdpHeader(buffer, response.length)
        assertTrue(target != null)
        assertArrayEquals(payload, buffer.copyOfRange(target!!.headerLength, response.length))
    }

    @Test
    fun shutdownClosesActiveTcpAndUdpConnections() {
        val echo = TcpEchoServer().also { resources.add(it) }
        val udpEcho = UdpEchoServer().also { resources.add(it) }
        val proxy = startProxy(coreThreads = 1, maxThreads = 4, perClient = 4)
        val tcp = openTcpTunnel(proxy.localPort(), echo.port).also { resources.add(it) }
        val udp = openUdpAssociation(proxy.localPort()).also { resources.add(it) }
        val datagrams = DatagramSocket().apply { soTimeout = 2_000 }.also { resources.add(it) }
        assertEcho(tcp, "active-tcp")
        assertUdpEcho(datagrams, udp.relayPort, udpEcho.port, "active-udp")
        proxy.stop()
        assertTrue(proxy.awaitStopped())
        assertSocketClosed(tcp)
        assertSocketClosed(udp.socket)
        assertEquals(0, proxy.metrics().activeConnections)
        assertEquals(0, proxy.metrics().activeTcpSessions)
        assertEquals(0, proxy.metrics().activeUdpAssociations)
        assertTrue(proxy.metrics().activeByClient.isEmpty())
    }

    /**
     * v1.3.2 regression guard for the field incident "SOCKS_HANDSHAKE_FAILED +
     * pending assignment": HostService binds 0.0.0.0 with allow-list [loopback, P2P /24] and
     * runs its bind-time self-test from 127.0.0.1. The self-test MUST succeed.
     * (In v1.3.1 the allow-list was P2P-only, so loopback was rejected before
     * the handshake and the service entered FAILED.)
     */
    @Test
    fun loopbackSelfTestSucceedsWithWildcardBindAndP2pAllowList() {
        val proxy = ProxyServer(
            ProxyServer.Config(
                port = 0,
                bindAddress = "0.0.0.0",
                allowedClientRanges = listOf("127.0.0.0/8", "192.168.49.0/24"),
                coreThreads = 1,
                maxThreads = 4,
                maxConnectionsPerClient = 4,
                maxTcpSessions = 4,
                maxTcpSessionsPerClient = 4
            )
        ).also { servers.add(it) }
        proxy.start()
        assertTrue("Proxy did not bind", proxy.awaitStarted())

        // Exactly what HostService.socksSelfTest does after binding.
        Socket("127.0.0.1", proxy.localPort()).use { socket ->
            socket.soTimeout = 2_000
            negotiate(socket)
        }
        assertEquals(0, proxy.metrics().rejectedConnections)
    }

    /**
     * Pins the rejection semantics: with a P2P-only allow-list, a loopback
     * client is closed before the SOCKS5 handshake. Documents why the loopback
     * range must stay in the HostService allow-list.
     */
    @Test
    fun loopbackClientIsRejectedByP2pOnlyAllowList() {
        val proxy = ProxyServer(
            ProxyServer.Config(
                port = 0,
                bindAddress = "0.0.0.0",
                allowedClientRanges = listOf("192.168.49.0/24"),
                coreThreads = 1,
                maxThreads = 4,
                maxConnectionsPerClient = 4,
                maxTcpSessions = 4,
                maxTcpSessionsPerClient = 4
            )
        ).also { servers.add(it) }
        proxy.start()
        assertTrue("Proxy did not bind", proxy.awaitStarted())

        Socket("127.0.0.1", proxy.localPort()).use { socket ->
            socket.soTimeout = 2_000
            socket.getOutputStream().write(byteArrayOf(5, 1, 0))
            socket.getOutputStream().flush()
            assertSocks5Refused(socket)
        }
        assertTrue(awaitCondition { proxy.metrics().rejectedConnections == 1 })
    }

    private fun startProxy(
        coreThreads: Int,
        maxThreads: Int,
        perClient: Int,
        secondaryPort: Int = 0,
        quotaWaitMs: Long = 0
    ): ProxyServer {
        val proxy = ProxyServer(
            ProxyServer.Config(
                port = 0,
                coreThreads = coreThreads,
                maxThreads = maxThreads,
                maxConnectionsPerClient = perClient,
                maxTcpSessions = maxOf(1, maxThreads),
                maxTcpSessionsPerClient = perClient,
                maxUdpAssociations = maxOf(1, maxThreads),
                maxUdpAssociationsPerClient = perClient,
                workerKeepAliveMs = 500,
                connectTimeoutMs = 2_000,
                handshakeTimeoutMs = 2_000,
                socketPollIntervalMs = 100,
                tcpIdleTimeoutMs = 2_000,
                udpTargetTimeoutMs = 500,
                udpIdleTimeoutMs = 2_000,
                udpCleanupIntervalMs = 100,
                secondaryPort = secondaryPort,
                quotaWaitMs = quotaWaitMs
            )
        )
        servers.add(proxy)
        proxy.start()
        assertTrue("Proxy did not bind", proxy.awaitStarted())
        return proxy
    }

    private fun openTcpTunnel(proxyPort: Int, targetPort: Int, localIp: String? = null): Socket {
        val socket = connectToProxy(proxyPort, localIp)
        socket.soTimeout = 2_000
        negotiate(socket)
        val request = byteArrayOf(5, 1, 0, 1, 127, 0, 0, 1) + portBytes(targetPort)
        socket.getOutputStream().write(request)
        socket.getOutputStream().flush()
        val reply = readExactly(socket.getInputStream(), 10)
        assertEquals("SOCKS CONNECT failed", 0, reply[1].toInt())
        return socket
    }

    private fun connectToProxy(proxyPort: Int, localIp: String? = null): Socket = Socket().apply {
        if (localIp != null) bind(InetSocketAddress(localIp, 0))
        connect(InetSocketAddress("127.0.0.1", proxyPort), 2_000)
    }

    private fun openUdpAssociation(proxyPort: Int): UdpControl {
        val socket = Socket("127.0.0.1", proxyPort)
        socket.soTimeout = 2_000
        negotiate(socket)
        socket.getOutputStream().write(byteArrayOf(5, 3, 0, 1, 0, 0, 0, 0, 0, 0))
        socket.getOutputStream().flush()
        val reply = readExactly(socket.getInputStream(), 10)
        assertEquals("SOCKS UDP ASSOCIATE failed", 0, reply[1].toInt())
        return UdpControl(socket, ((reply[8].toInt() and 0xFF) shl 8) or (reply[9].toInt() and 0xFF))
    }

    private fun negotiate(socket: Socket) {
        // v1.5.43: the host enforces RFC 1929 credentials — offer no-auth +
        // user/pass, then authenticate with the default account.
        socket.getOutputStream().write(byteArrayOf(5, 2, 0, 2))
        socket.getOutputStream().flush()
        assertArrayEquals(byteArrayOf(5, 2), readExactly(socket.getInputStream(), 2))
        socket.getOutputStream().write(
            Socks5Parser.buildUserPassRequest(ProxyServer.DEFAULT_SOCKS_USER, ProxyServer.DEFAULT_SOCKS_PASS)
        )
        socket.getOutputStream().flush()
        assertArrayEquals(byteArrayOf(1, 0), readExactly(socket.getInputStream(), 2))
    }

    private fun assertEcho(socket: Socket, value: String) {
        val bytes = value.toByteArray()
        socket.getOutputStream().write(bytes)
        socket.getOutputStream().flush()
        assertArrayEquals(bytes, readExactly(socket.getInputStream(), bytes.size))
    }

    private fun assertUdpEcho(socket: DatagramSocket, relayPort: Int, targetPort: Int, value: String) {
        val payload = value.toByteArray()
        val request = Socks5Parser.buildUdpHeader(InetAddress.getByName("127.0.0.1"), targetPort) + payload
        socket.send(DatagramPacket(request, request.size, InetAddress.getByName("127.0.0.1"), relayPort))

        val buffer = ByteArray(1024)
        val response = DatagramPacket(buffer, buffer.size)
        socket.receive(response)
        val target = Socks5Parser.parseUdpHeader(response.data, response.length)
        assertTrue(target != null)
        assertArrayEquals(payload, response.data.copyOfRange(target!!.headerLength, response.length))
    }

    private fun assertUdpForeignIpDropped(relayPort: Int, targetPort: Int) {
        // beta1.0.51: the association's security boundary is the client IP —
        // a datagram from a different IP must be dropped even though same-IP
        // source ports are now always accepted.
        val socket = DatagramSocket(InetSocketAddress("127.0.0.2", 0)).apply {
            soTimeout = 250
        }
        try {
            val payload = "foreign-ip".toByteArray()
            val request = Socks5Parser.buildUdpHeader(InetAddress.getByName("127.0.0.1"), targetPort) + payload
            socket.send(DatagramPacket(request, request.size, InetAddress.getByName("127.0.0.1"), relayPort))
            try {
                socket.receive(DatagramPacket(ByteArray(1024), 1024))
                throw AssertionError("UDP association accepted traffic from a foreign client IP")
            } catch (_: SocketTimeoutException) {
                // Expected: only the association's own client IP is served.
            }
        } finally {
            socket.close()
        }
    }

    private fun assertSocketClosed(socket: Socket) {
        try {
            assertEquals(-1, socket.getInputStream().read())
        } catch (_: SocketException) {
            // A reset is also a valid immediate rejection.
        }
    }

    /** v1.5.3: rejections answer the standard SOCKS5 \x05\xff before closing. */
    private fun assertSocks5Refused(socket: Socket) {
        socket.soTimeout = 2_000
        val reply = readExactly(socket.getInputStream(), 2)
        assertEquals(
            "server must send \\x05\\xff method-failure refusal",
            byteArrayOf(5, (0xFF).toByte()).toList(),
            reply.toList(),
        )
        assertSocketClosed(socket)
    }

    private fun readExactly(input: InputStream, length: Int): ByteArray {
        val result = ByteArray(length)
        var offset = 0
        while (offset < length) {
            val count = input.read(result, offset, length - offset)
            if (count < 0) throw EOFException("Expected $length bytes, received $offset")
            offset += count
        }
        return result
    }

    private fun portBytes(port: Int) = byteArrayOf((port ushr 8).toByte(), (port and 0xFF).toByte())

    private fun awaitCondition(timeoutMs: Long = 3_000, condition: () -> Boolean): Boolean {
        val deadline = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(timeoutMs)
        while (System.nanoTime() < deadline) {
            if (condition()) return true
            Thread.sleep(10)
        }
        return condition()
    }

    private data class UdpControl(val socket: Socket, val relayPort: Int) : Closeable {
        override fun close() = socket.close()
    }

    private class TcpEchoServer : Closeable {
        private val running = AtomicBoolean(true)
        private val server = ServerSocket(0)
        private val clients = ConcurrentHashMap.newKeySet<Socket>()
        val port: Int = server.localPort

        init {
            thread(name = "Test-TCP-Echo", isDaemon = true) {
                try {
                    while (running.get()) {
                        val client = server.accept()
                        clients.add(client)
                        thread(isDaemon = true) {
                            try {
                                val buffer = ByteArray(1024)
                                while (true) {
                                    val count = client.getInputStream().read(buffer)
                                    if (count < 0) break
                                    client.getOutputStream().write(buffer, 0, count)
                                    client.getOutputStream().flush()
                                }
                            } catch (_: Exception) {
                            } finally {
                                clients.remove(client)
                                client.close()
                            }
                        }
                    }
                } catch (_: SocketException) {
                }
            }
        }

        override fun close() {
            running.set(false)
            server.close()
            clients.toList().forEach { it.close() }
        }
    }

    private class UdpEchoServer : Closeable {
        private val running = AtomicBoolean(true)
        private val socket = DatagramSocket(0)
        val port: Int = socket.localPort

        init {
            thread(name = "Test-UDP-Echo", isDaemon = true) {
                val buffer = ByteArray(1024)
                try {
                    while (running.get()) {
                        val packet = DatagramPacket(buffer, buffer.size)
                        socket.receive(packet)
                        socket.send(DatagramPacket(packet.data, packet.length, packet.address, packet.port))
                    }
                } catch (_: SocketException) {
                }
            }
        }

        override fun close() {
            running.set(false)
            socket.close()
        }
    }
}
