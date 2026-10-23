package com.linkhelm.app

import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.BufferedReader
import java.io.InputStreamReader
import java.net.HttpURLConnection
import java.net.InetSocketAddress
import java.net.Proxy
import java.net.ServerSocket
import java.net.Socket
import java.net.URL
import java.util.concurrent.CopyOnWriteArrayList
import kotlin.concurrent.thread

/**
 * v1.4.4 — host-side downstream-device roster ("connected devices") and kick.
 *
 * 1. A SOCKS5 client shows up in [ProxyServer.clientSnapshot].
 * 2. [ProxyServer.kickClient] closes its live sessions AND blocks reconnects.
 * 3. [ProxyServer.unblockClient] lets it back in.
 * 4. The HTTP proxy enforces the same blocklist (kicked device cannot switch ports).
 * 5. HTTP proxy forwards POST bodies (v1.4.0 regression: body was drained but
 *    Content-Length re-announced → origin hung waiting for bytes).
 */
class ClientRosterIntegrationTest {

    private lateinit var proxy: ProxyServer
    private var originServer: ServerSocket? = null
    private var originPort: Int = -1
    private val originBodies = CopyOnWriteArrayList<String>()

    /** Minimal origin: echoes request path; captures POST bodies. */
    private fun startOrigin(): ServerSocket {
        val server = ServerSocket()
        server.reuseAddress = true
        server.bind(InetSocketAddress("127.0.0.1", 0), 16)
        thread(name = "test-origin", isDaemon = true) {
            while (!server.isClosed) {
                val conn = try {
                    server.accept()
                } catch (_: Exception) {
                    return@thread
                }
                thread(isDaemon = true) {
                    try {
                        conn.use { c ->
                            c.soTimeout = 5_000
                            val reader = BufferedReader(InputStreamReader(c.getInputStream(), Charsets.ISO_8859_1))
                            val requestLine = reader.readLine() ?: return@thread
                            var contentLength = 0
                            while (true) {
                                val line = reader.readLine() ?: break
                                if (line.isEmpty()) break
                                if (line.startsWith("Content-Length:", ignoreCase = true)) {
                                    contentLength = line.substringAfter(':').trim().toIntOrNull() ?: 0
                                }
                            }
                            val body = CharArray(contentLength)
                            var read = 0
                            while (read < contentLength) {
                                val n = reader.read(body, read, contentLength - read)
                                if (n < 0) break
                                read += n
                            }
                            originBodies.add(String(body, 0, read))
                            val path = requestLine.split(" ").getOrNull(1) ?: "/"
                            val payload = "OK:$path".toByteArray()
                            val head = "HTTP/1.1 200 OK\r\nContent-Length: ${payload.size}\r\nConnection: close\r\n\r\n"
                            c.getOutputStream().apply {
                                write(head.toByteArray(Charsets.ISO_8859_1))
                                write(payload)
                                flush()
                            }
                        }
                    } catch (_: Exception) {
                    }
                }
            }
        }
        return server
    }

    @Before
    fun setUp() {
        LogCompat.jvmSink = { _, _, _, _ -> } // silence android.util.Log on JVM
        proxy = ProxyServer(
            ProxyServer.Config(
                port = 0,
                coreThreads = 2,
                maxThreads = 8,
                maxConnectionsPerClient = 8,
                // beta1.0.51: Config requires maxTcpSessions <= maxThreads —
                // the new production defaults (800/375) no longer fit a tiny
                // test server, so pin them to the test capacity.
                maxTcpSessions = 8,
                maxTcpSessionsPerClient = 8,
                allowedClientRanges = listOf("127.0.0.0/8")
            )
        )
        proxy.start()
        assertTrue("proxy must bind", proxy.awaitStarted(5_000))
    }

    @After
    fun tearDown() {
        proxy.stop()
        runCatching { originServer?.close() }
        LogCompat.jvmSink = null
    }

    /** Raw SOCKS5 CONNECT to 127.0.0.1:[targetPort]; throws if the handshake fails. */
    private fun socksConnect(targetPort: Int): Socket {
        val socket = Socket("127.0.0.1", proxy.localPort())
        socket.soTimeout = 5_000
        val out = socket.getOutputStream()
        val input = socket.getInputStream()
        out.write(byteArrayOf(5, 2, 0, 2))
        out.flush()
        assertEquals(5, input.read())
        assertEquals(2, input.read()) // METHOD_USERPASS (v1.5.43: credentials enforced)
        out.write(Socks5Parser.buildUserPassRequest(ProxyServer.DEFAULT_SOCKS_USER, ProxyServer.DEFAULT_SOCKS_PASS))
        out.flush()
        assertEquals(1, input.read())
        assertEquals(0, input.read()) // auth success
        val request = byteArrayOf(5, 1, 0, 1, 127, 0, 0, 1, (targetPort shr 8).toByte(), (targetPort and 0xFF).toByte())
        out.write(request)
        out.flush()
        val reply = ByteArray(10)
        var read = 0
        while (read < reply.size) {
            val n = input.read(reply, read, reply.size - read)
            if (n < 0) throw IllegalStateException("connection closed during handshake")
            read += n
        }
        assertEquals("CONNECT must succeed", 0, reply[1].toInt())
        return socket
    }

    @Test
    fun `connected client appears in the roster with active connections`() {
        originServer = startOrigin()
        originPort = originServer!!.localPort
        val client = socksConnect(originPort)
        try {
            val roster = proxy.clientSnapshot()
            val me = roster.firstOrNull { it.ip == "127.0.0.1" }
            assertTrue("client must appear in roster", me != null)
            assertTrue(me!!.activeConnections >= 1)
            assertFalse(me.blocked)
        } finally {
            client.close()
        }
    }

    @Test
    fun `kick closes live sessions and blocks reconnects until unblocked`() {
        originServer = startOrigin()
        originPort = originServer!!.localPort
        val client = socksConnect(originPort)
        Thread.sleep(100)
        val closed = proxy.kickClient("127.0.0.1")
        assertTrue("at least the live session socket must close", closed >= 1)
        assertTrue(proxy.clientSnapshot().first { it.ip == "127.0.0.1" }.blocked)
        // the live socket must be dead on the client side
        client.soTimeout = 2_000
        assertEquals("kicked socket should read EOF", -1, client.getInputStream().read())
        // reconnect attempt is rejected before the handshake completes:
        // v1.5.3 the pre-greeting rejection answers the standard SOCKS5
        // method-failure reply \x05\xff then closes (EOF otherwise).
        val rejected = try {
            val s = Socket("127.0.0.1", proxy.localPort())
            s.soTimeout = 2_000
            s.getOutputStream().write(byteArrayOf(5, 1, 0))
            s.getOutputStream().flush()
            val first = s.getInputStream().read()
            val second = if (first >= 0) s.getInputStream().read() else -1
            s.close()
            (first == 5 && second == 0xFF) || first == -1 // refusal reply or closed
        } catch (_: Exception) {
            true
        }
        assertTrue("kicked device must not be able to reconnect", rejected)
        // unblock → back to normal
        assertTrue(proxy.unblockClient("127.0.0.1"))
        val again = socksConnect(originPort)
        again.close()
        assertFalse(proxy.clientSnapshot().first { it.ip == "127.0.0.1" }.blocked)
    }

    @Test
    fun `http proxy denies blocked clients and forwards POST bodies`() {
        originServer = startOrigin()
        originPort = originServer!!.localPort
        // a) POST body must actually reach the origin (v1.4.0 regression fix)
        val httpProxy = HttpProxyServer(
            port = 0,
            bindAddress = "127.0.0.1",
            allowedClientRanges = listOf("127.0.0.0/8")
        )
        httpProxy.start()
        assertTrue(httpProxy.awaitStarted(5_000))
        try {
            val conn = URL("http://127.0.0.1:$originPort/submit").openConnection(
                Proxy(Proxy.Type.HTTP, InetSocketAddress("127.0.0.1", httpProxy.listeningPort))
            ) as HttpURLConnection
            conn.requestMethod = "POST"
            conn.doOutput = true
            conn.connectTimeout = 5_000
            conn.readTimeout = 5_000
            conn.setFixedLengthStreamingMode(11)
            conn.outputStream.write("hello=world".toByteArray())
            conn.outputStream.flush()
            assertEquals(200, conn.responseCode)
            assertEquals("hello=world", originBodies.last())
        } finally {
            httpProxy.stop()
        }
        // b) kicked device is denied on the HTTP port too
        val guarded = HttpProxyServer(
            port = 0,
            bindAddress = "127.0.0.1",
            allowedClientRanges = listOf("127.0.0.0/8"),
            blockedClients = setOf("127.0.0.1")
        )
        guarded.start()
        assertTrue(guarded.awaitStarted(5_000))
        try {
            val conn = URL("http://127.0.0.1:$originPort/x").openConnection(
                Proxy(Proxy.Type.HTTP, InetSocketAddress("127.0.0.1", guarded.listeningPort))
            ) as HttpURLConnection
            conn.connectTimeout = 5_000
            conn.readTimeout = 5_000
            var denied = false
            try {
                conn.responseCode
            } catch (_: Exception) {
                denied = true
            }
            assertTrue("blocked client must be denied on the HTTP proxy", denied)
        } finally {
            guarded.stop()
        }
        assertNull(null)
    }
}
