package com.linkhelm.app

import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
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

    /** v1.4.82/1.4.9: per-IP quota is 200 with ≤2 active devices, 150 once ≥3. */
    @Test
    fun dynamicPerClientQuotaSwitchesAtThreeDevices() {
        val proxy = startProxy(coreThreads = 1, maxThreads = 480, perClient = 200).also { servers.add(it) }
        assertEquals(200, proxy.perClientQuotaFor(1))
        assertEquals(200, proxy.perClientQuotaFor(2))
        assertEquals(150, proxy.perClientQuotaFor(3))
        assertEquals(150, proxy.perClientQuotaFor(10))
        // Small explicit configs stay authoritative for ≤2 devices (test harness
        // relies on this): the 150 ceiling must never RAISE a smaller base.
        val small = startProxy(coreThreads = 1, maxThreads = 4, perClient = 4).also { servers.add(it) }
        assertEquals(4, small.perClientQuotaFor(2))
        assertEquals(150, small.perClientQuotaFor(3))
    }

    /**
     * v1.5.0: the ONLY network-side tuning added this release — extreme
     * congestion (≥90% pool busy) tightens NEW-connection per-IP admission
     * to congestionQuotaPerClient; it must overlay (never raise) the normal
     * tiers and disappear entirely when congestion clears.
     */
    @Test
    fun congestionModeTightensNewConnectionQuotaAndRecovers() {
        val proxy = startProxy(coreThreads = 1, maxThreads = 480, perClient = 200).also { servers.add(it) }
        // Normal path untouched when the pool is healthy. NOTE: the (1) probe
        // must run BEFORE any (3) probe — perClientQuotaFor(3) arms the 30s
        // debounce, which would legitimately hold 150 for (1) afterwards.
        assertEquals(200, proxy.perClientQuotaFor(1))

        // Extreme congestion: both tiers are capped by the overlay.
        proxy.congestionActive.set(true)
        assertEquals(100, proxy.perClientQuotaFor(1))
        assertEquals(100, proxy.perClientQuotaFor(3))

        // Congestion clears: the ≥3 tier returns to 150 (the (1) tier stays
        // inside the debounce window set by the (3) probe above, so it is
        // intentionally not re-asserted here).
        proxy.congestionActive.set(false)
        assertEquals(150, proxy.perClientQuotaFor(3))

        // Overlay applies via minOf(base, congestionQuotaPerClient): it can
        // never RAISE a smaller configured base (small test configs keep 4).
        val small = startProxy(coreThreads = 1, maxThreads = 4, perClient = 4).also { servers.add(it) }
        small.congestionActive.set(true)
        assertEquals(4, small.perClientQuotaFor(1))
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

    /** v1.4.9: the 150 tier is held for quotaDebounceMs after the last ≥3 observation. */
    @Test
    fun quotaTierHoldsForDebounceWindowAfterThreeDevices() {
        val proxy = ProxyServer(
            ProxyServer.Config(
                port = 0,
                coreThreads = 1,
                maxThreads = 480,
                maxConnectionsPerClient = 200,
                quotaDebounceMs = 120,
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
        assertTrue("proxy must bind", proxy.awaitStarted(5_000))
        assertEquals(150, proxy.perClientQuotaFor(3)) // arms the debounce
        assertEquals(150, proxy.perClientQuotaFor(2)) // still held inside the window
        Thread.sleep(200) // > 120ms window
        assertEquals(200, proxy.perClientQuotaFor(2)) // back to base after the window
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
        assertUdpEndpointRejected(secondUdp, first.relayPort, udpEcho.port)
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
                maxConnectionsPerClient = 4
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
                maxConnectionsPerClient = 4
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

    private fun assertUdpEndpointRejected(socket: DatagramSocket, relayPort: Int, targetPort: Int) {
        val previousTimeout = socket.soTimeout
        socket.soTimeout = 250
        val payload = "wrong-endpoint".toByteArray()
        val request = Socks5Parser.buildUdpHeader(InetAddress.getByName("127.0.0.1"), targetPort) + payload
        socket.send(DatagramPacket(request, request.size, InetAddress.getByName("127.0.0.1"), relayPort))
        try {
            socket.receive(DatagramPacket(ByteArray(1024), 1024))
            throw AssertionError("UDP association accepted traffic from a different source endpoint")
        } catch (_: SocketTimeoutException) {
            // Expected: an association stays bound to the first UDP source endpoint it observes.
        } finally {
            socket.soTimeout = previousTimeout
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
