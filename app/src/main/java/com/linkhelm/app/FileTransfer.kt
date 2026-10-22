package com.linkhelm.app

import android.content.ContentResolver
import android.net.Uri
import android.provider.DocumentsContract
import android.provider.OpenableColumns
import android.util.Base64
import android.util.Log
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.File
import java.io.IOException
import java.io.InputStream
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.security.MessageDigest
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.concurrent.thread

/**
 * v1.5.45 (beta1.0.1): file-transfer TRIAL — single file, <= 1 GB, Wi-Fi Direct only.
 *
 * Wire protocol (newline-terminated ASCII header, then raw bytes), port 8765:
 *   -> "AUTH <user> <pass>\n"            <- "OK\n" | "ERR auth\n"
 *   -> "OFFER <nameB64> <size> <sha256>\n" <- "ACCEPT\n" | "REJECT <reason>\n"
 *   -> <raw file bytes>                  <- "DONE\n" | "FAIL <reason>\n"
 * Closing the socket at any point cancels the transfer (the receiver deletes
 * its partial file). Auth reuses the SOCKS5 credentials (RFC 1929 account) —
 * there is no second secret in the trial.
 *
 * The channel is deliberately independent of the SOCKS5 exit: files never
 * touch the phone's uplink, and the transfer cannot change system routes.
 */
internal object FileTransferProtocol {
    const val PORT = 8765
    const val MAX_FILE_BYTES = 1_000_000_000L
    const val CHUNK = 128 * 1024
    const val PREFS_KEY_DIR = "ft_dir_uri"
    const val PREFS_KEY_PAIRED = "ft_paired_ips"
}

/**
 * beta1.0.3: IP pairing. A transfer is only allowed between MUTUALLY paired
 * devices (host <-> client, client <-> client): the peer's IP must be in this
 * device's paired set AND this device's IP must be in the peer's set. The
 * PAIR handshake below establishes both directions in one tap, because the
 * responder records the requester's IP while the requester records the
 * responder's from the connection itself. IPs are persisted — Wi-Fi Direct
 * leases can change, so a failed transfer after a re-group is fixed by
 * pairing again.
 */
internal object FileTransferPairs {
    // beta1.0.3 hotfix: the pair store is read during UI composition, which can
    // happen BEFORE HostService has registered the app context (fresh install /
    // cold start). Every accessor must be crash-safe and degrade to "no pairs".
    private fun prefs(): android.content.SharedPreferences? =
        runCatching {
            appContext().getSharedPreferences("linkhelm_prefs", android.content.Context.MODE_PRIVATE)
        }.getOrNull()

    fun pairedSet(): Set<String> =
        prefs()?.getStringSet(FileTransferProtocol.PREFS_KEY_PAIRED, emptySet()) ?: emptySet()

    fun isPaired(ip: String): Boolean = pairedSet().contains(ip)

    fun addPaired(ip: String) {
        prefs()?.edit()?.putStringSet(
            FileTransferProtocol.PREFS_KEY_PAIRED, pairedSet() + ip
        )?.apply()
    }

    fun removePaired(ip: String) {
        prefs()?.edit()?.putStringSet(
            FileTransferProtocol.PREFS_KEY_PAIRED, pairedSet() - ip
        )?.apply()
    }

    /**
     * The local IP as seen on the Wi-Fi Direct link. The group owner is
     * conventionally 192.168.49.1, but the displayed value always follows the
     * ACTUAL bound address (it changes when the group is re-created). Falls
     * back to the first site-local IPv4 when no 192.168.49.* address exists.
     */
    fun localLanIp(): String {
        return try {
            val candidates = java.net.NetworkInterface.getNetworkInterfaces().asSequence()
                .filter { it.isUp && !it.isLoopback }
                .flatMap { nif -> nif.inetAddresses.asSequence() }
                .filterIsInstance<java.net.Inet4Address>()
                .map { it.hostAddress ?: "" }
                .filter { it.isNotEmpty() && !it.startsWith("127.") }
                .toList()
            candidates.firstOrNull { it.startsWith("192.168.49.") }
                ?: candidates.firstOrNull { it.startsWith("192.168.") }
                ?: candidates.firstOrNull()
                ?: ""
        } catch (_: Exception) {
            ""
        }
    }
}

/** Compose-observable transfer state (written from background threads). */
object FileTransferBus {
    var state by mutableStateOf(FileTransferState())
        internal set

    fun reset() {
        state = FileTransferState()
    }
}

data class FileTransferState(
    val active: Boolean = false,
    val direction: String = "idle", // "send" | "receive" | "idle"
    val fileName: String = "",
    val totalBytes: Long = 0,
    val doneBytes: Long = 0,
    val status: String = "",
    val error: String? = null
)

private fun sha256Hex(input: InputStream): String {
    val digest = MessageDigest.getInstance("SHA-256")
    val buf = ByteArray(FileTransferProtocol.CHUNK)
    var read = input.read(buf)
    while (read >= 0) {
        digest.update(buf, 0, read)
        read = input.read(buf)
    }
    return digest.digest().joinToString("") { "%02x".format(it) }
}

private fun writeLine(output: BufferedOutputStream, line: String) {
    output.write((line + "\n").toByteArray(Charsets.UTF_8))
    output.flush()
}

private fun readLine(input: BufferedInputStream): String? {
    val sb = StringBuilder()
    while (true) {
        val c = input.read()
        if (c < 0) return if (sb.isEmpty()) null else sb.toString()
        if (c == '\n'.code) return sb.toString()
        if (c != '\r'.code) sb.append(c.toChar())
        if (sb.length > 8192) throw IOException("header line too long")
    }
}

/** v1.5.45: strip anything that could escape the save directory. */
internal fun sanitizeTransferName(raw: String): String {
    val base = raw.substringAfterLast('/').substringAfterLast('\\').trim()
    val cleaned = base.filter { it.code in 0x20..0x7E && it !in "\\/:*?\"<>|" }
    return cleaned.ifEmpty { "file.bin" }.take(200)
}

internal fun fileDisplayInfo(resolver: ContentResolver, uri: Uri): Pair<String, Long>? {
    return try {
        resolver.query(uri, null, null, null, null)?.use { cursor ->
            val nameIdx = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
            val sizeIdx = cursor.getColumnIndex(OpenableColumns.SIZE)
            cursor.moveToFirst()
            val name = if (nameIdx >= 0 && !cursor.isNull(nameIdx)) cursor.getString(nameIdx) else "file.bin"
            val size = if (sizeIdx >= 0 && !cursor.isNull(sizeIdx)) cursor.getLong(sizeIdx) else -1L
            name to size
        }
    } catch (_: Exception) {
        null
    }
}

/**
 * v1.5.45: Android-side RECEIVER server. Binds all interfaces but only
 * accepts loopback + the Wi-Fi Direct /24 (same source policy as the SOCKS5
 * proxy) — files never leave the local link.
 */
internal class FileTransferServer(
    private val user: String,
    private val pass: String,
    private val resolver: ContentResolver,
    private val saveDir: Uri
) {
    private val running = AtomicBoolean(false)
    private var serverSocket: ServerSocket? = null
    private val busy = AtomicBoolean(false)

    fun start() {
        if (!running.compareAndSet(false, true)) return
        thread(name = "FileTransfer-Server", isDaemon = true) {
            try {
                val socket = ServerSocket().apply {
                    reuseAddress = true
                    bind(InetSocketAddress("0.0.0.0", FileTransferProtocol.PORT), 8)
                }
                serverSocket = socket
                Log.i("FileTransfer", "Receiver listening on :${socket.localPort} (Wi-Fi Direct sources only)")
                while (running.get() && !socket.isClosed) {
                    val client = try {
                        socket.accept()
                    } catch (_: Exception) {
                        break
                    }
                    thread(name = "FileTransfer-Recv", isDaemon = true) { handleClient(client) }
                }
            } catch (error: Exception) {
                Log.w("FileTransfer", "receiver server stopped: ${error.message}")
            } finally {
                runCatching { serverSocket?.close() }
                running.set(false)
            }
        }
    }

    fun stop() {
        running.set(false)
        runCatching { serverSocket?.close() }
    }

    private fun allowedSource(ip: String): Boolean =
        ip.startsWith("127.") || ip.startsWith("192.168.49.")

    private fun handleClient(client: Socket) {
        if (!busy.compareAndSet(false, true)) {
            runCatching {
                val out = BufferedOutputStream(client.getOutputStream())
                writeLine(out, "ERR busy")
            }
            runCatching { client.close() }
            return
        }
        var tempFile: File? = null
        try {
            client.soTimeout = 60_000
            val remote = client.inetAddress.hostAddress ?: "?"
            if (!allowedSource(remote)) {
                Log.w("FileTransfer", "Refused file transfer from disallowed source $remote")
                runCatching { client.close() }
                return
            }
            val input = BufferedInputStream(client.getInputStream())
            val output = BufferedOutputStream(client.getOutputStream())

            val first = readLine(input) ?: ""

            // beta1.0.3: PAIR handshake — records the requester's IP in the
            // paired set (mutual pairing is completed on the requester's side
            // when it sees the PAIRED reply). Reuses the SOCKS5 credentials.
            if (first == "PAIR $user $pass") {
                FileTransferPairs.addPaired(remote)
                writeLine(output, "PAIRED")
                Log.i("FileTransfer", "Paired with $remote")
                return
            }
            if (first == "UNPAIR $user $pass") {
                FileTransferPairs.removePaired(remote)
                writeLine(output, "UNPAIRED")
                Log.i("FileTransfer", "Unpaired from $remote")
                return
            }

            val parts = first.split(" ")
            if (parts.size != 3 || parts[0] != "AUTH" || parts[1] != user || parts[2] != pass) {
                writeLine(output, "ERR auth")
                Log.w("FileTransfer", "Auth failed from $remote")
                return
            }
            // beta1.0.3: a transfer requires prior IP pairing on BOTH ends.
            if (!FileTransferPairs.isPaired(remote)) {
                writeLine(output, "ERR pair")
                Log.w("FileTransfer", "Transfer refused from $remote: not paired")
                return
            }
            writeLine(output, "OK")

            val offer = readLine(input)?.split(" ") ?: emptyList()
            if (offer.size != 4 || offer[0] != "OFFER") {
                writeLine(output, "REJECT bad-offer")
                return
            }
            val name = sanitizeTransferName(String(Base64.decode(offer[1], Base64.NO_WRAP)))
            val size = offer[2].toLongOrNull() ?: -1L
            val expectedSha = offer[3]
            if (size < 0 || size > FileTransferProtocol.MAX_FILE_BYTES) {
                writeLine(output, "REJECT size")
                Log.w("FileTransfer", "Rejected $name: size $size outside trial limit")
                return
            }

            FileTransferBus.state = FileTransferBus.state.copy(
                active = true, direction = "receive", fileName = name,
                totalBytes = size, doneBytes = 0, status = "receiving", error = null
            )

            // Stage in app cache first: a verified file is only published to the
            // user-chosen SAF directory after the SHA-256 check passes, and a
            // cancelled/failed transfer leaves nothing behind.
            tempFile = File(appContext().cacheDir, "$name.part")
            val digest = MessageDigest.getInstance("SHA-256")
            var received = 0L
            tempFile.outputStream().buffered(FileTransferProtocol.CHUNK).use { out ->
                val buf = ByteArray(FileTransferProtocol.CHUNK)
                while (received < size) {
                    val want = minOf(buf.size.toLong(), size - received).toInt()
                    val read = input.read(buf, 0, want)
                    if (read < 0) throw IOException("connection closed mid-file")
                    out.write(buf, 0, read)
                    digest.update(buf, 0, read)
                    received += read
                    FileTransferBus.state = FileTransferBus.state.copy(doneBytes = received)
                }
                out.flush()
            }
            val actualSha = digest.digest().joinToString("") { "%02x".format(it) }
            if (!actualSha.equals(expectedSha, ignoreCase = true)) {
                writeLine(output, "FAIL sha256")
                FileTransferBus.state = FileTransferBus.state.copy(
                    active = false, status = "failed",
                    error = "SHA-256 mismatch; partial file discarded"
                )
                Log.w("FileTransfer", "SHA-256 mismatch for $name")
                return
            }

            val published = publishToSaveDir(tempFile, name)
            if (published == null) {
                writeLine(output, "FAIL storage")
                FileTransferBus.state = FileTransferBus.state.copy(
                    active = false, status = "failed",
                    error = "Could not save to the selected directory"
                )
                return
            }
            writeLine(output, "DONE")
            FileTransferBus.state = FileTransferBus.state.copy(
                active = false, doneBytes = size, status = "done: $published", error = null
            )
            Log.i("FileTransfer", "Received $name ($size bytes) -> $published")
        } catch (error: Exception) {
            runCatching {
                client.getOutputStream()?.let { writeLine(BufferedOutputStream(it), "FAIL ${error.message ?: "error"}") }
            }
            FileTransferBus.state = FileTransferBus.state.copy(
                active = false, status = "cancelled/failed", error = error.message
            )
            Log.w("FileTransfer", "receive failed: ${error.message}")
        } finally {
            tempFile?.let { runCatching { it.delete() } }
            busy.set(false)
            runCatching { client.close() }
        }
    }

    /** Copies the verified cache file into the SAF directory; returns the display name. */
    private fun publishToSaveDir(temp: File, name: String): String? {
        return try {
            val doc = DocumentsContract.createDocument(
                resolver, saveDir, "application/octet-stream", name
            ) ?: return null
            resolver.openOutputStream(doc)?.use { out ->
                temp.inputStream().buffered(FileTransferProtocol.CHUNK).use { input ->
                    input.copyTo(out, FileTransferProtocol.CHUNK)
                }
                out.flush()
            } ?: return null
            name
        } catch (error: Exception) {
            Log.w("FileTransfer", "publish failed: ${error.message}")
            null
        }
    }
}

/**
 * v1.5.45: Android-side SENDER. Connects to the Windows receiver (default
 * 192.168.49.2 — the first client on the Wi-Fi Direct group), authenticates
 * with the SOCKS5 credentials and streams one picked document.
 */
internal object FileTransferClient {
    private val cancelled = AtomicBoolean(false)
    @Volatile private var socket: Socket? = null

    fun cancel() {
        cancelled.set(true)
        runCatching { socket?.close() }
    }

    /**
     * beta1.0.3: one-tap mutual IP pairing with the peer (host <-> client or
     * client <-> client). The peer records our IP; we record the peer's from
     * the successful reply. Returns null on success, else a human-readable
     * reason. Runs on the calling thread (short request, 5s timeouts).
     */
    fun pair(targetIp: String, user: String, pass: String): String? {
        return runCatching {
            Socket().use { client ->
                client.connect(InetSocketAddress(targetIp, FileTransferProtocol.PORT), 5_000)
                client.soTimeout = 5_000
                val output = BufferedOutputStream(client.getOutputStream())
                val input = BufferedInputStream(client.getInputStream())
                writeLine(output, "PAIR $user $pass")
                when (readLine(input)) {
                    "PAIRED" -> {
                        FileTransferPairs.addPaired(targetIp)
                        null
                    }
                    "ERR busy" -> "对端正忙，请稍后再试"
                    else -> "对端未确认配对（请检查两端版本与凭据）"
                }
            }
        }.getOrElse {
            when {
                it.message?.contains("timeout", true) == true -> "连接超时：请确认对方 IP 正确且与主机在同一 Wi-Fi Direct 网络"
                else -> "无法连接 ${targetIp}:${FileTransferProtocol.PORT}（${it.message ?: "未知错误"}）"
            }
        }
    }

    fun unpair(targetIp: String, user: String, pass: String) {
        runCatching {
            Socket().use { client ->
                client.connect(InetSocketAddress(targetIp, FileTransferProtocol.PORT), 5_000)
                client.soTimeout = 5_000
                val output = BufferedOutputStream(client.getOutputStream())
                val input = BufferedInputStream(client.getInputStream())
                writeLine(output, "UNPAIR $user $pass")
                readLine(input)
            }
        }
        FileTransferPairs.removePaired(targetIp)
    }

    fun send(targetIp: String, user: String, pass: String, resolver: ContentResolver, fileUri: Uri) {
        cancelled.set(false)
        val info = fileDisplayInfo(resolver, fileUri)
        if (info == null) {
            FileTransferBus.state = FileTransferBus.state.copy(active = false, error = "Cannot read the picked file")
            return
        }
        val (rawName, size) = info
        val name = sanitizeTransferName(rawName)
        if (size < 0) {
            FileTransferBus.state = FileTransferBus.state.copy(active = false, error = "Unknown file size")
            return
        }
        if (size > FileTransferProtocol.MAX_FILE_BYTES) {
            FileTransferBus.state = FileTransferBus.state.copy(
                active = false, error = "File is larger than the 1 GB trial limit"
            )
            return
        }
        FileTransferBus.state = FileTransferBus.state.copy(
            active = true, direction = "send", fileName = name,
            totalBytes = size, doneBytes = 0, status = "connecting", error = null
        )
        thread(name = "FileTransfer-Send", isDaemon = true) {
            try {
                val sha = resolver.openInputStream(fileUri)?.use { sha256Hex(it) }
                    ?: throw IOException("Cannot open the picked file")
                val client = Socket().apply {
                    connect(InetSocketAddress(targetIp, FileTransferProtocol.PORT), 8_000)
                    soTimeout = 60_000
                }
                socket = client
                val input = BufferedInputStream(client.getInputStream())
                val output = BufferedOutputStream(client.getOutputStream())
                writeLine(output, "AUTH $user $pass")
                when (readLine(input)) {
                    "OK" -> {}
                    "ERR pair" -> throw IOException("对方尚未与本机配对：请先在两端分别点击「配对」")
                    else -> throw IOException("认证被对端拒绝（请核对 SOCKS5 凭据）")
                }
                val nameB64 = Base64.encodeToString(name.toByteArray(Charsets.UTF_8), Base64.NO_WRAP)
                writeLine(output, "OFFER $nameB64 $size $sha")
                val reply = readLine(input)
                if (reply != "ACCEPT") throw IOException("Receiver refused: ${reply ?: "no reply"}")

                FileTransferBus.state = FileTransferBus.state.copy(status = "sending")
                var sent = 0L
                resolver.openInputStream(fileUri)?.use { stream ->
                    val buf = ByteArray(FileTransferProtocol.CHUNK)
                    while (sent < size) {
                        if (cancelled.get()) throw IOException("Cancelled by user")
                        val want = minOf(buf.size.toLong(), size - sent).toInt()
                        val read = stream.read(buf, 0, want)
                        if (read < 0) throw IOException("File changed size while sending")
                        output.write(buf, 0, read)
                        sent += read
                        if (sent % (8L * 1024 * 1024) < FileTransferProtocol.CHUNK) {
                            output.flush()
                            FileTransferBus.state = FileTransferBus.state.copy(doneBytes = sent)
                        }
                    }
                    output.flush()
                } ?: throw IOException("Cannot re-open the picked file")
                FileTransferBus.state = FileTransferBus.state.copy(doneBytes = sent)
                val done = readLine(input)
                if (done == "DONE") {
                    FileTransferBus.state = FileTransferBus.state.copy(
                        active = false, status = "done", error = null
                    )
                } else {
                    throw IOException("Receiver reported: ${done ?: "no reply"}")
                }
            } catch (error: Exception) {
                FileTransferBus.state = FileTransferBus.state.copy(
                    active = false, status = "cancelled/failed", error = error.message
                )
                Log.w("FileTransfer", "send failed: ${error.message}")
            } finally {
                runCatching { socket?.close() }
                socket = null
            }
        }
    }
}

/** The application context is registered by HostService on create. */
internal object FileTransferApp {
    @Volatile var context: android.content.Context? = null
    fun appContext(): android.content.Context =
        context ?: error("FileTransferApp context not initialized")
}

private fun appContext(): android.content.Context = FileTransferApp.appContext()
