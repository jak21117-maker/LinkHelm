package com.linkhelm.app

import org.junit.After
import org.junit.Assert.assertEquals
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
 * v1.4.0 — Verifies that a client WITHOUT any VPN/SOCKS support can get content
 * through the host using only its native Wi-Fi "manual HTTP proxy" setting
 * (the only system-level no-app path an iPhone has).
 *
 * Chain under test:  HttpURLConnection(Proxy HTTP) → HttpProxyServer → origin.
 * The origin is a minimal in-test HTTP server (com.sun.httpserver is not on the
 * android.jar unit-test classpath).
 */
class HttpProxyIntegrationTest {

    private var originServer: ServerSocket? = null
    private var originPort: Int = -1
    private lateinit var proxy: HttpProxyServer

    private val originHits = CopyOnWriteArrayList<String>()

    /** Minimal HTTP/1.1 origin: answers every request "GET <path>" with the path echo. */
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
                            while (reader.readLine()?.isNotEmpty() == true) { /* headers */ }
                            val path = requestLine.split(" ").getOrNull(1) ?: "/"
                            originHits.add(path)
                            val body = "HELLO_FROM_ORIGIN:$path".toByteArray()
                            val head = "HTTP/1.1 200 OK\r\n" +
                                "Content-Type: text/plain\r\n" +
                                "Content-Length: ${body.size}\r\n" +
                                "Connection: close\r\n\r\n"
                            c.getOutputStream().apply {
                                write(head.toByteArray(Charsets.ISO_8859_1))
                                write(body)
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

        originServer = startOrigin()
        originPort = originServer!!.localPort

        proxy = HttpProxyServer(
            port = 0, // ephemeral
            bindAddress = "127.0.0.1",
            allowedClientRanges = listOf("127.0.0.0/8", "192.168.49.0/24")
        )
        proxy.start()
        assertTrue("http proxy must bind", proxy.awaitStarted(5_000))
        assertTrue(proxy.listeningPort > 0)
    }

    @After
    fun tearDown() {
        proxy.stop()
        runCatching { originServer?.close() }
        LogCompat.jvmSink = null
    }

    private fun proxyAddr() = Proxy(Proxy.Type.HTTP, InetSocketAddress("127.0.0.1", proxy.listeningPort))

    @Test
    fun `plain GET through the HTTP proxy returns origin content`() {
        val conn = URL("http://127.0.0.1:$originPort/alpha").openConnection(proxyAddr()) as HttpURLConnection
        conn.connectTimeout = 5_000
        conn.readTimeout = 5_000
        try {
            assertEquals(200, conn.responseCode)
            val body = conn.inputStream.bufferedReader().readText()
            assertEquals("HELLO_FROM_ORIGIN:/alpha", body)
        } finally {
            conn.disconnect()
        }
        assertEquals(listOf("/alpha"), originHits)
    }

    @Test
    fun `successive requests on separate connections keep working`() {
        // Simulates the field pattern: the proxy closes each client socket after a
        // response; every mainstream HTTP stack must transparently reconnect.
        repeat(2) { i ->
            val path = if (i == 0) "/one" else "/two"
            val conn = URL("http://127.0.0.1:$originPort$path").openConnection(proxyAddr()) as HttpURLConnection
            conn.connectTimeout = 5_000
            conn.readTimeout = 5_000
            try {
                assertEquals(200, conn.responseCode)
                assertEquals("HELLO_FROM_ORIGIN:$path", conn.inputStream.bufferedReader().readText())
            } finally {
                conn.disconnect()
            }
        }
        assertEquals(listOf("/one", "/two"), originHits)
    }

    @Test
    fun `CONNECT tunnel carries an end-to-end request`() {
        // iPhone-style: system proxy opens a CONNECT tunnel, then speaks plain HTTP inside it.
        Socket("127.0.0.1", proxy.listeningPort).use { sock ->
            sock.soTimeout = 5_000
            val out = sock.getOutputStream()
            out.write("CONNECT 127.0.0.1:$originPort HTTP/1.1\r\nHost: 127.0.0.1:$originPort\r\n\r\n".toByteArray())
            out.flush()

            val reader = BufferedReader(InputStreamReader(sock.getInputStream(), Charsets.ISO_8859_1))
            val status = reader.readLine()
            assertTrue("expected 200, got: $status", status.contains("200"))
            // consume the rest of the proxy reply headers
            while (reader.readLine()?.isNotEmpty() == true) { /* headers */ }

            out.write("GET /tunnel HTTP/1.1\r\nHost: 127.0.0.1:$originPort\r\nConnection: close\r\n\r\n".toByteArray())
            out.flush()
            val response = StringBuilder()
            while (true) {
                val line = reader.readLine() ?: break
                response.append(line).append('\n')
            }
            val text = response.toString()
            assertTrue("origin content must pass through the tunnel", text.contains("HELLO_FROM_ORIGIN:/tunnel"))
        }
        assertEquals(listOf("/tunnel"), originHits)
    }

    @Test
    fun `client outside allowed ranges is rejected`() {
        val strict = HttpProxyServer(
            port = 0,
            bindAddress = "127.0.0.1",
            allowedClientRanges = listOf("192.168.49.0/24") // loopback deliberately NOT allowed
        )
        strict.start()
        assertTrue(strict.awaitStarted(5_000))
        try {
            val conn = URL("http://127.0.0.1:$originPort/nope").openConnection(
                Proxy(Proxy.Type.HTTP, InetSocketAddress("127.0.0.1", strict.listeningPort))
            ) as HttpURLConnection
            conn.connectTimeout = 3_000
            conn.readTimeout = 3_000
            // The proxy must either close the connection outright or return an error status;
            // it must NEVER relay the request to the origin.
            val rejected = try {
                conn.responseCode >= 400
            } catch (_: Exception) {
                true
            } finally {
                conn.disconnect()
            }
            assertTrue("out-of-range client must not be served", rejected)
            assertTrue("origin must not see rejected clients", originHits.isEmpty())
        } finally {
            strict.stop()
        }
    }

    @Test
    fun `unsupported method gets a 400 and never reaches the origin`() {
        Socket("127.0.0.1", proxy.listeningPort).use { sock ->
            sock.soTimeout = 5_000
            val out = sock.getOutputStream()
            out.write("NOTAMETHOD /x HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".toByteArray())
            out.flush()
            val reader = BufferedReader(InputStreamReader(sock.getInputStream(), Charsets.ISO_8859_1))
            val status = reader.readLine()
            assertTrue("expected 400, got: $status", status.contains("400"))
        }
        assertTrue(originHits.isEmpty())
    }
}
