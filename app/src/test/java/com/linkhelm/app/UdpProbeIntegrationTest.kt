package com.linkhelm.app

import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.Closeable
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.InetSocketAddress
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.concurrent.thread

/**
 * v1.4.3 — UdpProbe end-to-end JVM integration test.
 *
 * Topology: UdpProbe -> a real ProxyServer (loopback allow-list) -> UDP relay -> a local mock DNS server.
 * Proves the SOCKS5 UDP ASSOCIATE DNS probe path works on real sockets; also covers
 * the direct UDP DNS query path (against the mock DNS server).
 */
class UdpProbeIntegrationTest {
    private val resources = mutableListOf<Closeable>()
    private val servers = mutableListOf<ProxyServer>()

    @After
    fun tearDown() {
        resources.reversed().forEach { runCatching { it.close() } }
        servers.reversed().forEach {
            it.stop()
            assertTrue("Proxy workers did not stop", it.awaitStopped())
        }
    }

    /** Minimal UDP DNS server: replies to any query with a QR=1, same-ID response. */
    private class MockDnsServer : Closeable {
        val socket = DatagramSocket(InetSocketAddress("127.0.0.1", 0))
        val port: Int = socket.localPort
        private val open = AtomicBoolean(true)

        init {
            socket.soTimeout = 500
            thread(isDaemon = true) {
                val buf = ByteArray(1024)
                while (open.get()) {
                    try {
                        val packet = DatagramPacket(buf, buf.size)
                        socket.receive(packet)
                        if (packet.length < 12) continue
                        val reply = ByteArray(packet.length)
                        System.arraycopy(buf, 0, reply, 0, packet.length)
                        reply[2] = (reply[2].toInt() or 0x80).toByte() // QR=1
                        socket.send(
                            DatagramPacket(
                                reply, reply.size, packet.address, packet.port
                            )
                        )
                    } catch (_: Exception) {
                        // timeout loop
                    }
                }
            }
        }

        override fun close() {
            open.set(false)
            socket.close()
        }
    }

    @Test
    fun socks5UdpDnsProbeSucceedsThroughRealProxy() {
        val dns = MockDnsServer()
        resources.add(dns)
        val proxy = startProxy()
        val error = UdpProbe.socks5UdpDnsProbe(
            socksHost = "127.0.0.1",
            socksPort = proxy.localPort(),
            dnsHost = "127.0.0.1",
            dnsPort = dns.port,
            timeoutMs = 3_000,
            socksUser = ProxyServer.DEFAULT_SOCKS_USER,
            socksPass = ProxyServer.DEFAULT_SOCKS_PASS
        )
        assertNull("expected success, got: $error", error)
    }

    @Test
    fun directUdpDnsProbeSucceeds() {
        val dns = MockDnsServer()
        resources.add(dns)
        val error = UdpProbe.dnsQueryViaUdp("127.0.0.1", dns.port, timeoutMs = 3_000)
        assertNull("expected success, got: $error", error)
    }

    @Test
    fun directUdpDnsProbeTimesOutWhenTargetSilent() {
        // a receive-only UDP port that never answers -> should report a timeout
        val sink = DatagramSocket(InetSocketAddress("127.0.0.1", 0))
        resources.add(sink)
        val error = UdpProbe.dnsQueryViaUdp("127.0.0.1", sink.localPort, timeoutMs = 700)
        assertNotNull("expected timeout error", error)
        assertTrue(error!!.contains("timeout", ignoreCase = true))
    }

    @Test
    fun socks5ProbeFailsGracefullyWhenProxyDown() {
        // nothing listening on the port -> fast failure with a readable error
        val error = UdpProbe.socks5UdpDnsProbe(
            socksHost = "127.0.0.1",
            socksPort = 1, // 保留端口，连接必被拒
            dnsHost = "127.0.0.1",
            timeoutMs = 1_000
        )
        assertNotNull("expected failure", error)
    }

    private fun startProxy(): ProxyServer {
            val proxy = ProxyServer(
            ProxyServer.Config(
                port = 0,
                coreThreads = 2,
                maxThreads = 8,
                maxConnectionsPerClient = 8,
                // beta1.0.51: pin session caps to the tiny test server
                // (production defaults 800/375 exceed maxThreads).
                maxTcpSessions = 8,
                maxTcpSessionsPerClient = 8,
                workerKeepAliveMs = 500,
                connectTimeoutMs = 2_000,
                handshakeTimeoutMs = 2_000,
                socketPollIntervalMs = 100,
                tcpIdleTimeoutMs = 2_000,
                udpTargetTimeoutMs = 2_000,
                udpIdleTimeoutMs = 5_000,
                udpCleanupIntervalMs = 200
            )
        )
        servers.add(proxy)
        proxy.start()
        assertTrue("Proxy did not bind", proxy.awaitStarted())
        return proxy
    }
}
