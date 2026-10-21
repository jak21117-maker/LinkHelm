package com.linkhelm.app

import java.io.ByteArrayOutputStream
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.net.SocketException
import java.net.SocketTimeoutException
import java.util.Locale
import java.util.concurrent.CountDownLatch
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong
import kotlin.concurrent.thread

/**
 * v1.4.0 — Independent HTTP proxy module ("iPhone proxy-free" / no-VPN clients).
 *
 * WHY THIS EXISTS
 * SOCKS5 (ProxyServer on 8080) is invisible to iPhones: iOS offers no system-wide
 * SOCKS setting. Both iOS and Android DO offer a per-network *manual HTTP proxy*
 * in their Wi-Fi settings, though. Pointing that setting at this listener gives a
 * no-app, no-VPN, no-jailbreak data path:
 *
 *   client (iPhone / Android without VPN)
 *     → Wi-Fi manual HTTP proxy 192.168.49.1:8081
 *     → this module (plain GET/POST absolute-URI forward + CONNECT tunnel)
 *     → phone default network (cellular) → internet
 *
 * SCOPE / BOUNDARIES (per the "standalone module" design doc):
 *  - ProxyServer.kt and HostService P2P logic stay frozen; this runs alongside on
 *    its own port and lifecycle.
 *  - It is a PROXY, not a transparent NAT: only traffic explicitly pointed at it
 *    is carried. UDP/games/QUIC bypass it (use the Connect tab for those).
 *  - Same client allow-list policy as the SOCKS5 server (loopback + P2P /24).
 */
class HttpProxyServer(
    private val port: Int = 8081,
    private val bindAddress: String = "0.0.0.0",
    private val allowedClientRanges: List<String> = emptyList(),
    // v1.4.4: shares the SOCKS5 server's kick blocklist so a device the host
    // disconnected cannot simply switch to the HTTP proxy port instead.
    private val blockedClients: Set<String> = emptySet(),
    private val connectTimeoutMs: Int = 15_000,
    private val handshakeTimeoutMs: Int = 15_000,
) {
    private val running = AtomicBoolean(false)
    private var serverSocket: ServerSocket? = null
    private var acceptThread: Thread? = null
    private var executor: ExecutorService? = null
    private val startLatch = CountDownLatch(1)
    private val activeConnections = AtomicLong(0)

    /** Non-null after a successful start; the OS-assigned port (== [port] unless 0). */
    @Volatile
    var listeningPort: Int = -1
        private set

    fun start() {
        require(port in 0..65535) { "invalid http proxy port: $port" }
        allowedClientRanges.forEach { require(parseCidr(it) != null) { "invalid CIDR range: $it" } }
        if (!running.compareAndSet(false, true)) return
        acceptThread = thread(name = "HttpProxy-Accept", isDaemon = true) { acceptLoop() }
    }

    fun stop() {
        if (!running.compareAndSet(true, false)) return
        try {
            serverSocket?.close()
        } catch (_: IOException) {
        }
        acceptThread?.interrupt()
        executor?.shutdownNow()
    }

    fun awaitStarted(timeoutMs: Long = 5_000L): Boolean = startLatch.await(timeoutMs, TimeUnit.MILLISECONDS)

    fun activeConnectionCount(): Long = activeConnections.get()

    private fun acceptLoop() {
        var pool: ExecutorService? = null
        try {
            val socket = ServerSocket()
            socket.reuseAddress = true
            socket.bind(InetSocketAddress(bindAddress, port), 64)
            serverSocket = socket
            listeningPort = socket.localPort
            pool = Executors.newCachedThreadPool { r ->
                Thread(r, "HttpProxy-Worker").apply { isDaemon = true }
            }
            executor = pool
            startLatch.countDown()
            LogCompat.d("HttpProxyServer", "HTTP proxy listening on 0.0.0.0:$listeningPort (ranges=$allowedClientRanges)")

            while (running.get() && !socket.isClosed) {
                val client = try {
                    socket.accept()
                } catch (error: SocketException) {
                    if (running.get()) LogCompat.w("HttpProxyServer", "accept failed: ${error.message}")
                    continue
                } catch (error: IOException) {
                    if (running.get()) LogCompat.w("HttpProxyServer", "accept failed: ${error.message}")
                    continue
                }
                if (!clientAllowed(client)) {
                    LogCompat.w("HttpProxyServer", "rejected client ${client.inetAddress.hostAddress}: outside allowed ranges")
                    tryClose(client)
                    continue
                }
                val exec = pool ?: break
                activeConnections.incrementAndGet()
                exec.execute {
                    try {
                        handleClient(client)
                    } catch (_: Throwable) {
                        // worker must never take down the accept loop
                    } finally {
                        activeConnections.decrementAndGet()
                    }
                }
            }
        } catch (error: Throwable) {
            LogCompat.e("HttpProxyServer", "HTTP proxy failed to start", error)
            startLatch.countDown()
            running.set(false)
            pool?.shutdownNow()
        } finally {
            try {
                serverSocket?.close()
            } catch (_: IOException) {
            }
        }
    }

    private fun clientAllowed(client: Socket): Boolean {
        val clientIp = client.inetAddress.hostAddress ?: return false
        // v1.4.4: host-kicked devices are denied on this port too.
        if (clientIp in blockedClients) return false
        if (allowedClientRanges.isEmpty()) return true
        return allowedClientRanges.any { range ->
            parseCidr(range)?.let { ipMatchesCidr(clientIp, it) } == true
        }
    }

    private fun handleClient(client: Socket) {
        client.tcpNoDelay = true
        client.soTimeout = handshakeTimeoutMs
        client.use { c ->
            val reader = c.getInputStream()
            val out = c.getOutputStream()
            // A client may issue several requests on one connection (keep-alive);
            // serve them sequentially until idle timeout or EOF.
            while (running.get()) {
                val requestLine = readLine(reader) ?: return
                if (requestLine.isBlank()) continue
                val parts = requestLine.split(" ")
                if (parts.size < 3) {
                    out.writeBadRequest("malformed request line")
                    return
                }
                val method = parts[0].uppercase(Locale.US)
                when {
                    method == "CONNECT" -> {
                        // drain the rest of the CONNECT request (headers + blank line) so
                        // no request bytes leak into the tunnel data stream
                        while (true) {
                            val headerLine = readLine(reader) ?: break
                            if (headerLine.isEmpty()) break
                        }
                        handleConnect(c, parts[1])
                        return // tunnel owns the socket end-to-end
                    }
                    method in HTTP_METHODS -> {
                        if (!handlePlainRequest(c, reader, out, requestLine)) return
                    }
                    else -> {
                        out.writeBadRequest("unsupported method $method")
                        return
                    }
                }
            }
        }
    }

    /**
     * Plain HTTP forward. The request line uses absolute-URI form
     * ("GET http://host/path HTTP/1.1"); falls back to the Host header.
     * We force "Connection: close" upstream and close the client socket after
     * the response stream ends — every mainstream HTTP stack re-connects for
     * the next request, so this keeps the handler dead simple and leak-free.
     */
    private fun handlePlainRequest(client: Socket, reader: InputStream, out: OutputStream, requestLine: String): Boolean {
        val headers = mutableMapOf<String, String>()
        var contentLength = 0
        while (true) {
            val line = readLine(reader) ?: return false
            if (line.isEmpty()) break
            val idx = line.indexOf(':')
            if (idx <= 0) continue
            val name = line.substring(0, idx).trim()
            val value = line.substring(idx + 1).trim()
            headers[name.lowercase(Locale.US)] = value
            if (name.equals("content-length", ignoreCase = true)) contentLength = value.toIntOrNull() ?: 0
        }
        // v1.4.4: read the request body INTO MEMORY and forward it upstream.
        // v1.4.0 drained the body but still re-announced Content-Length upstream,
        // so POST/PUT hung until timeout (origin waited for bytes never sent).
        // Keeping the body also leaves the keep-alive stream in a clean state.
        var body: ByteArray? = null
        if (contentLength > 0) {
            val buf = ByteArrayOutputStream(contentLength)
            var remaining = contentLength
            val chunk = ByteArray(8 * 1024)
            while (remaining > 0) {
                val n = reader.read(chunk, 0, minOf(chunk.size.toLong(), remaining.toLong()).toInt())
                if (n < 0) return false
                buf.write(chunk, 0, n)
                remaining -= n
            }
            body = buf.toByteArray()
        }
        if (headers.containsKey("transfer-encoding")) {
            // chunked bodies are not supported by this minimal forwarder
            out.writeBadRequest("Transfer-Encoding not supported by this proxy")
            return false
        }

        val target = resolveTarget(requestLine, headers["host"])
            ?: run {
                out.writeBadRequest("cannot resolve target host")
                return false
            }

        val upstream = try {
            Socket().apply {
                tcpNoDelay = true
                connect(InetSocketAddress(target.first, target.second), connectTimeoutMs)
                soTimeout = handshakeTimeoutMs
            }
        } catch (error: IOException) {
            LogCompat.w("HttpProxyServer", "upstream connect failed ${target.first}:${target.second}: ${error.message}")
            out.writeBadGateway()
            return false
        }

        try {
            upstream.use { up ->
                val upOut = up.getOutputStream()
                // rewrite to origin-form and pin close semantics upstream
                val parts = requestLine.split(" ")
                val path = extractPath(parts[1], headers["host"])
                upOut.write("${parts[0]} $path HTTP/1.1\r\n".toByteArray())
                var wroteHost = false
                headers.forEach { (name, value) ->
                    if (name in HOP_BY_HOP || name == "host") return@forEach
                    if (name == "connection" || name == "proxy-connection") return@forEach
                    if (name == "keep-alive") return@forEach
                    if (name == "content-length") return@forEach // body already drained; re-add below
                    upOut.write("$name: $value\r\n".toByteArray())
                    if (name == "host") wroteHost = true
                }
                if (!wroteHost) upOut.write("Host: ${headers["host"] ?: "${target.first}:${target.second}"}\r\n".toByteArray())
                if (contentLength > 0) upOut.write("Content-Length: $contentLength\r\n".toByteArray())
                upOut.write("Connection: close\r\n\r\n".toByteArray())
                // v1.4.4: actually send the captured request body
                body?.let { upOut.write(it) }
                upOut.flush()
                // request fully forwarded; go straight to relaying the response
                // until the server closes.
                pumpUntilEof(up.getInputStream(), out)
            }
        } catch (error: IOException) {
            LogCompat.w("HttpProxyServer", "relay error: ${error.message}")
            return false
        }
        // close the client side; the next keep-alive request opens a new connection
        return false
    }

    /** CONNECT host:port — raw bidirectional tunnel after a 200 reply. */
    private fun handleConnect(client: Socket, authority: String) {
        val idx = authority.lastIndexOf(':')
        if (idx <= 0) {
            client.getOutputStream().writeBadRequest("bad CONNECT authority")
            return
        }
        val host = authority.substring(0, idx)
        val port = authority.substring(idx + 1).toIntOrNull()
        if (port == null || port !in 1..65535) {
            client.getOutputStream().writeBadRequest("bad CONNECT port")
            return
        }
        val upstream = try {
            Socket().apply {
                tcpNoDelay = true
                connect(InetSocketAddress(host, port), connectTimeoutMs)
                soTimeout = 0 // tunnel may idle; rely on caller-side timeouts
            }
        } catch (error: IOException) {
            LogCompat.w("HttpProxyServer", "CONNECT $host:$port failed: ${error.message}")
            try {
                client.getOutputStream().writeBadGateway()
            } catch (_: IOException) {
            }
            return
        }
        try {
            client.getOutputStream().write("HTTP/1.1 200 Connection Established\r\n\r\n".toByteArray())
            client.getOutputStream().flush()
            client.soTimeout = 0
            bidirectionalPump(client, upstream)
            LogCompat.d("HttpProxyServer", "CONNECT tunnel closed $host:$port")
        } catch (_: IOException) {
        } finally {
            tryClose(upstream)
            tryClose(client)
        }
    }

    private fun resolveTarget(requestLine: String, hostHeader: String?): Pair<String, Int>? {
        val parts = requestLine.split(" ")
        if (parts.size < 2) return null
        val uri = parts[1]
        val absMatch = Regex("^https?://([^/:]+)(?::(\\d+))?").find(uri)
        if (absMatch != null) {
            return absMatch.groupValues[1] to (absMatch.groupValues[2].toIntOrNull() ?: 80)
        }
        // origin-form: rely on Host header ("host" or "host:port")
        if (hostHeader != null) {
            val hIdx = hostHeader.lastIndexOf(':')
            return if (hIdx > 0) {
                hostHeader.substring(0, hIdx) to (hostHeader.substring(hIdx + 1).toIntOrNull() ?: 80)
            } else {
                hostHeader to 80
            }
        }
        return null
    }

    private fun extractPath(uri: String, hostHeader: String?): String {
        val schemeIdx = uri.indexOf("://")
        if (schemeIdx > 0) {
            val slash = uri.indexOf('/', schemeIdx + 3)
            return if (slash > 0) uri.substring(slash) else "/"
        }
        return if (uri.startsWith("/")) uri else "/$uri"
    }

    private fun bidirectionalPump(a: Socket, b: Socket) {
        val t1 = thread(name = "HttpProxy-Pump", isDaemon = true) {
            try {
                pumpUntilEof(a.getInputStream(), b.getOutputStream())
            } catch (_: IOException) {
            } finally {
                tryClose(b) // unblock the reverse pump
            }
        }
        try {
            pumpUntilEof(b.getInputStream(), a.getOutputStream())
        } catch (_: IOException) {
        } finally {
            t1.interrupt()
            tryClose(a)
        }
    }

    private fun pumpUntilEof(input: InputStream, output: OutputStream) {
        val buf = ByteArray(64 * 1024)
        while (true) {
            val n = try {
                input.read(buf)
            } catch (_: SocketTimeoutException) {
                return
            } catch (_: IOException) {
                return
            }
            if (n < 0) return
            output.write(buf, 0, n)
            output.flush()
        }
    }

    /** Reads a CRLF/LF-terminated line as Latin-1 (HTTP header bytes are ASCII-safe here). */
    private fun readLine(input: InputStream): String? {
        val sb = StringBuilder(80)
        while (true) {
            val b = try {
                input.read()
            } catch (_: SocketTimeoutException) {
                return if (sb.isEmpty()) null else sb.toString()
            } catch (_: IOException) {
                return if (sb.isEmpty()) null else sb.toString()
            }
            if (b < 0) return if (sb.isEmpty()) null else sb.toString()
            if (b == '\n'.code) {
                if (sb.isNotEmpty() && sb.last() == '\r') sb.setLength(sb.length - 1)
                return sb.toString()
            }
            sb.append(b.toChar())
            if (sb.length > 16 * 1024) return sb.toString() // header line cap
        }
    }

    private fun OutputStream.writeBadRequest(reason: String) {
        runCatching {
            write(
                ("HTTP/1.1 400 Bad Request\r\n" +
                    "Content-Type: text/plain\r\n" +
                    "Connection: close\r\n\r\n" +
                    "LinkHelm HTTP proxy: $reason\r\n").toByteArray()
            )
            flush()
        }
    }

    private fun OutputStream.writeBadGateway() {
        runCatching {
            write("HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Length: 0\r\n\r\n".toByteArray())
            flush()
        }
    }

    private fun tryClose(socket: Socket?) {
        runCatching { socket?.close() }
    }

    private companion object {
        val HTTP_METHODS = setOf("GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "PATCH")
        val HOP_BY_HOP = setOf("te", "trailers", "upgrade", "via")
    }
}

/**
 * Log shim: on-device this forwards to android.util.Log; JVM unit tests inject
 * [jvmSink] so no android.jar stubbing is needed.
 */
internal object LogCompat {
    @Volatile
    var jvmSink: ((level: Char, tag: String, msg: String, error: Throwable?) -> Unit)? = null

    private fun emit(level: Char, tag: String, msg: String, error: Throwable?) {
        val sink = jvmSink
        if (sink != null) {
            sink(level, tag, msg, error)
        } else {
            when (level) {
                'D' -> android.util.Log.d(tag, msg, error)
                'W' -> android.util.Log.w(tag, msg, error)
                else -> android.util.Log.e(tag, msg, error)
            }
        }
    }

    fun d(tag: String, msg: String) = emit('D', tag, msg, null)

    fun w(tag: String, msg: String) = emit('W', tag, msg, null)

    fun e(tag: String, msg: String, error: Throwable? = null) = emit('E', tag, msg, error)
}
