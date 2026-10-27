package com.linkhelm.app

import android.content.ContentResolver
import android.content.Context
import android.net.Uri
import android.os.PowerManager
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
import java.io.FileOutputStream
import java.io.IOException
import java.io.InputStream
import java.io.RandomAccessFile
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.security.MessageDigest
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.concurrent.thread

/**
 * v1.5.45 (beta1.0.1): file transfer — single file, <= 30 GB (beta1.0.62,
 * was 1 GB), Wi-Fi Direct only.
 *
 * Wire protocol (newline-terminated ASCII header, then raw bytes), port 8765:
 *   -> "AUTH <user> <pass>\n"            <- "OK\n" | "ERR auth\n"
 *   -> "OFFER <nameB64> <size> <sha256>\n" <- "ACCEPT\n" | "RESUME <offset>\n"
 *                                             | "REJECT <reason>\n"   (beta1.0.58)
 *   -> <raw file bytes>                  <- "DONE\n" | "FAIL <reason>\n"
 * beta1.0.58: RESUMABLE — the receiver stages a .part plus a .meta sidecar
 * (name/size/sha256/part_size, refreshed every few MB). A dropped connection
 * — peer exit included — answers "RESUME <offset>" on the next attempt of the
 * SAME file and the sender seeks there; the receiver's offset is the single
 * source of truth, so both sides stay in step. stop() (app exit) deletes the
 * staging pair, so a fresh start never resumes a stale session.
 * Auth reuses the SOCKS5 credentials (RFC 1929 account) — there is no second
 * secret in the channel.
 *
 * The channel is deliberately independent of the SOCKS5 exit: files never
 * touch the phone's uplink, and the transfer cannot change system routes.
 */
internal object FileTransferProtocol {
    const val PORT = 8765
    const val MAX_FILE_BYTES = 30_000_000_000L
    const val CHUNK = 128 * 1024
    const val PREFS_KEY_DIR = "ft_dir_uri"
    const val PREFS_KEY_PAIRED = "ft_paired_ips"

    /**
     * beta1.0.4: accept either "192.168.49.2" or "192.168.49.2:8765". The peer's
     * displayed address may already include the port; never append the default a
     * second time (that produced "192.168.49.2:8765:8765" and a failed connect).
     */
    internal fun parsePeerAddress(raw: String): Pair<String, Int> {
        val s = raw.trim()
        val lastColon = s.lastIndexOf(':')
        if (lastColon > 0) {
            val host = s.substring(0, lastColon)
            val port = s.substring(lastColon + 1).toIntOrNull()
            if (port != null && port in 1..65535 && host.isNotEmpty() && !host.contains(':')) {
                return host to port
            }
        }
        return s to PORT
    }
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

// beta1.0.61: a PARTIAL wake lock held for the duration of a transfer. The
// phone suspending the socket when the screen turns off (Doze / app standby)
// was the main cause of the reported "偶尔也会断开" mid-transfer. The lock
// keeps the CPU alive; it does NOT keep the screen on and touches no
// WLAN/Wi-Fi/adapter state.
private val transferWakeLock: PowerManager.WakeLock by lazy {
    val pm = appContext().getSystemService(Context.POWER_SERVICE) as PowerManager
    pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "LinkHelm:FileTransfer")
        .apply { setReferenceCounted(false) }
}

private fun acquireTransferWakeLock() {
    runCatching {
        if (!transferWakeLock.isHeld) {
            transferWakeLock.acquire(4 * 60 * 60 * 1000L)   // 4 h safety cap
        }
    }
}

private fun releaseTransferWakeLock() {
    runCatching {
        if (transferWakeLock.isHeld) transferWakeLock.release()
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
    // beta1.0.61: keep CJK names intact. The old 0x20..0x7E filter ate every
    // Chinese character ("微信图片_2024.jpg" -> ".jpg", a fully Chinese name
    // -> "file.bin"). Only path separators, control characters and the
    // Windows-invalid set are removed now.
    val base = raw.substringAfterLast('/').substringAfterLast('\\').trim()
    val cleaned = base.filter { it.code >= 0x20 && it !in "\\/:*?\"<>|" }
    return cleaned.ifEmpty { "file" }.take(200).trimEnd('.', ' ')
}

// beta1.0.61: real format from magic bytes, so a name that lost its
// extension (or carries a generic .bin) is saved as what it really is.
private val GENERIC_EXTS = setOf("", ".bin", ".file", ".dat", ".raw")

internal fun sniffExtension(head: ByteArray): String = when {
    head.size >= 3 && head[0] == 0xFF.toByte() && head[1] == 0xD8.toByte() &&
        head[2] == 0xFF.toByte() -> ".jpg"
    head.size >= 8 && head[0] == 0x89.toByte() && head[1] == 'P'.code.toByte() &&
        head[2] == 'N'.code.toByte() && head[3] == 'G'.code.toByte() -> ".png"
    head.size >= 6 && String(head, 0, 6, Charsets.US_ASCII) == "GIF87a" -> ".gif"
    head.size >= 6 && String(head, 0, 6, Charsets.US_ASCII) == "GIF89a" -> ".gif"
    head.size >= 2 && head[0] == 'B'.code.toByte() && head[1] == 'M'.code.toByte() -> ".bmp"
    head.size >= 4 && head[0] == 0x00.toByte() && head[1] == 0x00.toByte() &&
        head[2] == 0x01.toByte() && head[3] == 0x00.toByte() -> ".ico"
    head.size >= 4 && String(head, 0, 4, Charsets.US_ASCII) == "%PDF" -> ".pdf"
    head.size >= 2 && head[0] == 'P'.code.toByte() && head[1] == 'K'.code.toByte() -> ".zip"
    head.size >= 7 && String(head, 0, 7, Charsets.US_ASCII) == "Rar!\u001a\u0007" -> ".rar"
    head.size >= 6 && String(head, 0, 6, Charsets.US_ASCII) == "7z\u00bc\u00af\u0027\u001c" -> ".7z"
    head.size >= 2 && head[0] == 0x1F.toByte() && head[1] == 0x8B.toByte() -> ".gz"
    head.size >= 12 && head[0] == 'R'.code.toByte() && head[1] == 'I'.code.toByte() &&
        head[2] == 'F'.code.toByte() && head[3] == 'F'.code.toByte() &&
        String(head, 8, 4, Charsets.US_ASCII) == "WEBP" -> ".webp"
    head.size >= 3 && String(head, 0, 3, Charsets.US_ASCII) == "ID3" -> ".mp3"
    head.size >= 2 && head[0] == 0xFF.toByte() &&
        (head[1] == 0xFB.toByte() || head[1] == 0xF3.toByte() || head[1] == 0xF2.toByte()) -> ".mp3"
    head.size >= 4 && String(head, 0, 4, Charsets.US_ASCII) == "fLaC" -> ".flac"
    head.size >= 4 && String(head, 0, 4, Charsets.US_ASCII) == "OggS" -> ".ogg"
    head.size >= 12 && String(head, 4, 4, Charsets.US_ASCII) == "ftyp" -> {
        val brand = String(head, 8, 4, Charsets.US_ASCII)
        if (brand.startsWith("qt ")) ".mov" else ".mp4"
    }
    head.size >= 4 && head[0] == 0x1A.toByte() && head[1] == 0x45.toByte() &&
        head[2] == 0xDF.toByte() && head[3] == 0xA3.toByte() -> ".mkv"
    head.size >= 4 && String(head, 0, 4, Charsets.US_ASCII) == "\u00d0\u00cf\u0011\u00e0" -> ".doc"
    else -> ""
}

/** Read the first bytes of a content URI for format detection ("" on failure). */
internal fun sniffExtension(resolver: ContentResolver, uri: Uri): String = try {
    resolver.openInputStream(uri)?.use { stream ->
        val buf = ByteArray(32)
        val read = stream.read(buf)
        if (read > 0) sniffExtension(buf.copyOf(read)) else ""
    } ?: ""
} catch (_: Exception) { "" }

/** Read the first bytes of a file for format detection ("" on failure). */
internal fun sniffExtension(file: File): String = try {
    file.inputStream().use { stream ->
        val buf = ByteArray(32)
        val read = stream.read(buf)
        if (read > 0) sniffExtension(buf.copyOf(read)) else ""
    }
} catch (_: Exception) { "" }

/**
 * Give a name without a usable extension its REAL format (magic bytes).
 * "IMG_2024" + jpeg content -> "IMG_2024.jpg"; "received.bin" + jpeg
 * content -> "received.jpg". Names that already carry a real extension
 * are returned untouched.
 */
internal fun withRealExtension(name: String, sniffed: String): String {
    val ext = name.substringAfterLast('.', "").lowercase()
    if (ext !in GENERIC_EXTS || sniffed.isEmpty()) return name
    val stem = if (ext.isEmpty()) name else name.dropLast(ext.length + 1)
    return stem + sniffed
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
        // beta1.0.58: stopping the receiver (app exit / share stop) removes
        // every staging buffer — a fresh start never resumes a stale session.
        runCatching {
            appContext().cacheDir.listFiles { file ->
                file.name.endsWith(".part") || file.name.endsWith(".part.meta")
            }?.forEach { it.delete() }
        }
    }

    /**
     * beta1.0.61: accept loopback + ANY private LAN range. Wi-Fi Direct is
     * 192.168.49.x, the Windows Mobile Hotspot is 192.168.137.x, routers are
     * usually 192.168.1.x or 10.x — refusing everything outside .49 was a real
     * "无法连接/偶尔断开" cause. The SOCKS5 credentials (AUTH) and the
     * explicit one-tap pairing still gate every transfer, so this coarse
     * source filter adds no exposure.
     * Read-only: no WLAN/Wi-Fi/adapter state is touched anywhere here.
     */
    private fun allowedSource(ip: String): Boolean =
        ip.startsWith("127.") || ip.startsWith("192.168.") ||
            ip.startsWith("10.") ||
            (ip.startsWith("172.") && ip.split(".").getOrNull(1)
                ?.toIntOrNull()?.let { it in 16..31 } == true)

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
        var metaFile: File? = null
        var keepPart = false
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
                Log.w("FileTransfer", "Rejected $name: size $size outside the 30 GB limit")
                return
            }

            FileTransferBus.state = FileTransferBus.state.copy(
                active = true, direction = "receive", fileName = name,
                totalBytes = size, doneBytes = 0, status = "receiving", error = null
            )
            // beta1.0.61: a real transfer starts here — hold the CPU awake so
            // the screen turning off cannot stall (and kill) the receive.
            acquireTransferWakeLock()

            // Stage in app cache first: a verified file is only published to the
            // user-chosen SAF directory after the SHA-256 check passes.
            // beta1.0.58: RESUMABLE — the .part plus a .meta sidecar survive a
            // dropped connection (peer exit included); the next attempt of the
            // SAME file continues at the staged offset. stop() wipes them.
            tempFile = File(appContext().cacheDir, "$name.part")
            metaFile = File(tempFile!!.path + ".meta")
            val sidecar = metaFile!!
            var offset = 0L
            if (tempFile!!.exists() && sidecar.exists()) {
                runCatching {
                    val meta = org.json.JSONObject(sidecar.readText())
                    val partSize = tempFile!!.length()
                    val checkpoint = meta.optLong("part_size", 0L)
                    if (meta.optString("name") == name &&
                        meta.optLong("size") == size &&
                        meta.optString("sha").equals(expectedSha, true) &&
                        checkpoint >= 1 && checkpoint < size &&
                        partSize >= checkpoint
                    ) {
                        // The sidecar is the authority: an abrupt drop flushes
                        // everything buffered, so the .part may reach past the
                        // last checkpoint — trim the uncheckpointed tail.
                        offset = checkpoint
                        if (partSize > offset) {
                            RandomAccessFile(tempFile, "rw").use { it.setLength(offset) }
                        }
                    }
                }
            }
            if (offset > 0) {
                writeLine(output, "RESUME $offset")
                Log.i("FileTransfer", "Resuming $name at $offset/$size")
            } else {
                runCatching { tempFile?.delete() }
                runCatching { sidecar.delete() }
                writeLine(output, "ACCEPT")
            }

            val digest = MessageDigest.getInstance("SHA-256")
            var received = offset
            keepPart = true // a mid-file failure from here on KEEPS progress
            FileOutputStream(tempFile, offset > 0).buffered(FileTransferProtocol.CHUNK).use { out ->
                if (offset > 0) {
                    // the final digest must still cover the whole file
                    tempFile!!.inputStream().buffered(FileTransferProtocol.CHUNK).use { existing ->
                        val buf = ByteArray(FileTransferProtocol.CHUNK)
                        var read = existing.read(buf)
                        while (read >= 0) {
                            digest.update(buf, 0, read)
                            read = existing.read(buf)
                        }
                    }
                }
                val buf = ByteArray(FileTransferProtocol.CHUNK)
                var sinceMeta = 0
                while (received < size) {
                    val want = minOf(buf.size.toLong(), size - received).toInt()
                    val read = input.read(buf, 0, want)
                    if (read < 0) throw IOException("connection closed mid-file")
                    out.write(buf, 0, read)
                    digest.update(buf, 0, read)
                    received += read
                    sinceMeta += read
                    FileTransferBus.state = FileTransferBus.state.copy(doneBytes = received)
                    if (sinceMeta >= 8L * 1024 * 1024) {
                        // checkpoint: flush, then refresh the sidecar so an
                        // interrupted transfer resumes from here, not zero
                        out.flush()
                        sidecar.writeText(
                            org.json.JSONObject()
                                .put("name", name).put("size", size)
                                .put("sha", expectedSha).put("part_size", received)
                                .toString()
                        )
                        sinceMeta = 0
                    }
                }
                out.flush()
            }
            val actualSha = digest.digest().joinToString("") { "%02x".format(it) }
            if (!actualSha.equals(expectedSha, ignoreCase = true)) {
                writeLine(output, "FAIL sha256")
                keepPart = false
                FileTransferBus.state = FileTransferBus.state.copy(
                    active = false, status = "failed",
                    error = "SHA-256 mismatch; partial file discarded"
                )
                Log.w("FileTransfer", "SHA-256 mismatch for $name")
                return
            }

            // beta1.0.61: the sender may have sent a name without a real
            // extension (or with a generic .bin) — publish under the format
            // the bytes actually say, so a photo never lands as .bin.
            val realName = withRealExtension(name, sniffExtension(tempFile!!))
            val published = publishToSaveDir(tempFile!!, realName)
            if (published == null) {
                writeLine(output, "FAIL storage")
                FileTransferBus.state = FileTransferBus.state.copy(
                    active = false, status = "failed",
                    error = "无法保存到所选目录（写入被拒）——请重新选择保存目录并确认授权"
                )
                return
            }
            // success: the staging pair is no longer needed
            runCatching { tempFile?.delete() }
            runCatching { sidecar.delete() }
            writeLine(output, "DONE")
            FileTransferBus.state = FileTransferBus.state.copy(
                active = false, doneBytes = size, status = "done: $published", error = null
            )
            Log.i("FileTransfer", "Received $name ($size bytes) -> $published")
        } catch (error: Exception) {
            runCatching {
                client.getOutputStream()?.let { writeLine(BufferedOutputStream(it), "FAIL ${error.message ?: "error"}") }
            }
            val resumeHint =
                if (keepPart && error.message?.contains("mid-file", true) == true)
                    "（已保留断点，重新发送同一文件即可续传）" else ""
            FileTransferBus.state = FileTransferBus.state.copy(
                active = false, status = "cancelled/failed",
                error = (error.message ?: "传输中断") + resumeHint
            )
            Log.w("FileTransfer", "receive failed: ${error.message}")
        } finally {
            // beta1.0.58: keep the .part (resumable) unless the transfer
            // completed or failed in a way that invalidates the staging file.
            if (!keepPart) {
                tempFile?.let { runCatching { it.delete() } }
                metaFile?.let { runCatching { it.delete() } }
            }
            busy.set(false)
            runCatching { client.close() }
            // beta1.0.61: release the transfer wake lock on EVERY exit path.
            releaseTransferWakeLock()
        }
    }

    /** Copies the verified cache file into the SAF directory; returns the display name.
     *  beta1.0.95: canonical document URI (buildDocumentUriUsingTree) — the
     *  raw tree:// URI passed straight to createDocument throws on several
     *  providers ("手机无法创建文件" field report). */
    private fun publishToSaveDir(temp: File, name: String): String? {
        return try {
            val rootDoc = DocumentsContract.buildDocumentUriUsingTree(
                saveDir, DocumentsContract.getTreeDocumentId(saveDir)
            )
            val doc = DocumentsContract.createDocument(
                resolver, rootDoc, "application/octet-stream", name
            ) ?: return null
            resolver.openOutputStream(doc)?.use { out ->
                temp.inputStream().buffered(FileTransferProtocol.CHUNK).use { input ->
                    input.copyTo(out, FileTransferProtocol.CHUNK)
                }
                out.flush()
            } ?: return null
            name
        } catch (error: Exception) {
            Log.w("FileTransfer", "publish failed: ${error.javaClass.simpleName}: ${error.message}")
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
        val (host, port) = FileTransferProtocol.parsePeerAddress(targetIp)
        return runCatching {
            Socket().use { client ->
                client.connect(InetSocketAddress(host, port), 5_000)
                client.soTimeout = 5_000
                val output = BufferedOutputStream(client.getOutputStream())
                val input = BufferedInputStream(client.getInputStream())
                writeLine(output, "PAIR $user $pass")
                when (readLine(input)) {
                    "PAIRED" -> {
                        FileTransferPairs.addPaired(host)
                        null
                    }
                    "ERR busy" -> "对端正忙，请稍后再试"
                    else -> "对端未确认配对（请检查两端版本与凭据）"
                }
            }
        }.getOrElse {
            when {
                it is java.net.ConnectException ->
                    "无法连接 ${host}:${port}（连接被拒绝）——请确认电脑端已打开「文件传输」窗口"
                it.message?.contains("timeout", true) == true -> "连接超时：请确认对方 IP 正确且与主机在同一 Wi-Fi Direct 网络"
                else -> "无法连接 ${host}:${port}（${it.message ?: "未知错误"}）"
            }
        }
    }

    fun unpair(targetIp: String, user: String, pass: String) {
        val (host, port) = FileTransferProtocol.parsePeerAddress(targetIp)
        runCatching {
            Socket().use { client ->
                client.connect(InetSocketAddress(host, port), 5_000)
                client.soTimeout = 5_000
                val output = BufferedOutputStream(client.getOutputStream())
                val input = BufferedInputStream(client.getInputStream())
                writeLine(output, "UNPAIR $user $pass")
                readLine(input)
            }
        }
        FileTransferPairs.removePaired(host)
    }

    fun send(targetIp: String, user: String, pass: String, resolver: ContentResolver, fileUri: Uri) {
        cancelled.set(false)
        val info = fileDisplayInfo(resolver, fileUri)
        if (info == null) {
            FileTransferBus.state = FileTransferBus.state.copy(active = false, error = "Cannot read the picked file")
            return
        }
        val (rawName, size) = info
        // beta1.0.61: a name without a real extension gets the format from
        // the file's own magic bytes, so the PC never sees a bare .bin.
        val name = withRealExtension(sanitizeTransferName(rawName),
                                     sniffExtension(resolver, fileUri))
        if (size < 0) {
            FileTransferBus.state = FileTransferBus.state.copy(active = false, error = "Unknown file size")
            return
        }
        if (size > FileTransferProtocol.MAX_FILE_BYTES) {
            FileTransferBus.state = FileTransferBus.state.copy(
                active = false, error = "File is larger than the 30 GB limit"
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
                val (host, port) = FileTransferProtocol.parsePeerAddress(targetIp)
                val client = try {
                    Socket().apply {
                        connect(InetSocketAddress(host, port), 8_000)
                        soTimeout = 60_000
                    }
                } catch (e: java.net.ConnectException) {
                    // beta1.0.59: ECONNREFUSED almost always means the PC
                    // side has no listener armed (dialog closed, or the
                    // Wi-Fi Direct lease changed) — say so instead of the
                    // raw socket error.
                    throw IOException(
                        "无法连接 $host:$port（连接被拒绝）——请确认电脑端已打开「文件传输」" +
                            "窗口，且地址填写的是电脑当前显示的「本机地址」"
                    )
                } catch (e: java.net.SocketTimeoutException) {
                    throw IOException(
                        "无法连接 $host:$port（超时）——请确认两端在同一 Wi-Fi Direct 网络，" +
                            "且地址填写的是电脑当前显示的「本机地址」"
                    )
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
                // beta1.0.61: the transfer is real from here on — hold the CPU
                // awake so a screen-off/Doze pause cannot stall the socket.
                acquireTransferWakeLock()
                val nameB64 = Base64.encodeToString(name.toByteArray(Charsets.UTF_8), Base64.NO_WRAP)
                writeLine(output, "OFFER $nameB64 $size $sha")
                val reply = readLine(input)
                // beta1.0.58: the receiver may answer RESUME <offset> when it
                // already holds a partial copy of this exact file (same name,
                // size and sha256). The receiver's offset is the single source
                // of truth, so the two sides can never fall out of step.
                var resumeFrom = 0L
                if (reply == "ACCEPT") {
                    resumeFrom = 0L
                } else if (reply != null && reply.startsWith("RESUME ")) {
                    resumeFrom = reply.removePrefix("RESUME ").trim().toLongOrNull() ?: -1L
                    if (resumeFrom < 0 || resumeFrom >= size) {
                        throw IOException("Receiver asked to resume at an invalid offset: $reply")
                    }
                } else {
                    throw IOException("Receiver refused: ${reply ?: "no reply"}")
                }

                FileTransferBus.state = FileTransferBus.state.copy(status = "sending")
                var sent = resumeFrom
                if (resumeFrom > 0) {
                    FileTransferBus.state = FileTransferBus.state.copy(
                        doneBytes = sent,
                        status = "resuming ${sent / 1_000_000} MB / ${size / 1_000_000} MB"
                    )
                }
                resolver.openInputStream(fileUri)?.use { stream ->
                    if (resumeFrom > 0) {
                        var skipped = 0L
                        while (skipped < resumeFrom) {
                            val step = stream.skip(resumeFrom - skipped)
                            if (step <= 0) throw IOException("Cannot seek to the resume offset")
                            skipped += step
                        }
                    }
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
                // beta1.0.61: never leave the CPU asleep holding a live
                // transfer — and never leak the lock either.
                releaseTransferWakeLock()
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

/**
 * beta1.0.93: process-wide FT receiver SINGLETON.
 *
 * Previously the receiver only started when the sharing service reached
 * SOCKS_READY — a phone that was merely CONNECTED to the peer (client mode,
 * sharing off, e.g. joined the PC's hotspot) had NO listener on :8765, so
 * every PC -> phone transfer was refused. The UI card has always claimed
 * the transfer is "independent of sharing"; now the lifecycle finally is:
 * ONE receiver per process, (re)started whenever a save dir exists — from
 * MainActivity.onCreate, after every directory re-pick, and on share start
 * (config sync only). It is NOT torn down when sharing stops; it dies with
 * the process.
 */
internal object FileTransferLive {
    @Volatile private var server: FileTransferServer? = null
    @Volatile private var runningDir: String? = null
    @Volatile private var runningCreds: Pair<String, String>? = null
    /** beta1.0.95: last sync failure (null = none) — surfaced in the UI so a
     *  dead receiver is visible instead of silently unreachable. */
    @Volatile var lastSyncError: String? = null

    /** True when a receiver is currently bound (for UI diagnostics). */
    fun isRunning(): Boolean = server != null

    @Synchronized
    fun sync(context: Context) {
        val app = context.applicationContext
        // register the app context FIRST: the receiver (and its wake lock)
        // needs it even when HostService has never run.
        FileTransferApp.context = app
        val prefs = app.getSharedPreferences("linkhelm_prefs", Context.MODE_PRIVATE)
        val dir = prefs.getString(FileTransferProtocol.PREFS_KEY_DIR, null)
        if (dir == null) {
            stop()
            return
        }
        val user = prefs.getString("socks_user", ProxyServer.DEFAULT_SOCKS_USER)
            ?: ProxyServer.DEFAULT_SOCKS_USER
        val pass = prefs.getString("socks_pass", ProxyServer.DEFAULT_SOCKS_PASS)
            ?: ProxyServer.DEFAULT_SOCKS_PASS
        // already running with the SAME dir + creds -> nothing to do
        if (server != null && dir == runningDir && runningCreds == user to pass) return
        stop()
        try {
            server = FileTransferServer(user, pass, app.contentResolver, Uri.parse(dir))
                .also { it.start() }
            runningDir = dir
            runningCreds = user to pass
            lastSyncError = null
            Log.i("FileTransfer", "Live receiver ensured on :${FileTransferProtocol.PORT} (independent of sharing)")
        } catch (e: Exception) {
            // e.g. bind failed (port taken by a zombie), bad URI, ...
            server = null
            lastSyncError = "${e.javaClass.simpleName}: ${e.message ?: "未知错误"}"
            Log.w("FileTransfer", "receiver sync failed: $lastSyncError")
        }
    }

    @Synchronized
    fun stop() {
        server?.stop()
        server = null
        runningDir = null
        runningCreds = null
    }
}

/**
 * beta1.0.95: validate a freshly picked SAF directory BEFORE trusting it —
 * CANONICAL URI FORM. 1.0.94 still failed with "无法在该目录创建文件" on the
 * field phone: the picker hands back a tree:// URI, and passing it straight
 * to DocumentsContract.createDocument throws on several Android versions /
 * providers (getDocumentId expects a DOCUMENT uri, not a TREE uri). The
 * canonical pattern (official SAF docs) is to first build the root document
 * uri via buildDocumentUriUsingTree(getTreeDocumentId(...)). Same fix is
 * applied to publishToSaveDir — that path had the identical latent bug.
 * Error text now carries the exception class so field reports identify the
 * cause. Returns null on success, else a human-readable reason.
 */
internal fun probeSaveDir(resolver: ContentResolver, dirUri: Uri): String? {
    val rootDoc = runCatching {
        DocumentsContract.buildDocumentUriUsingTree(
            dirUri, DocumentsContract.getTreeDocumentId(dirUri)
        )
    }.getOrElse { return "目录 URI 无法解析（${it.javaClass.simpleName}）" }

    val doc = try {
        DocumentsContract.createDocument(
            resolver, rootDoc, "application/octet-stream", "linkhelm_probe"
        )
    } catch (e: Exception) {
        return "无法在该目录创建文件（${e.javaClass.simpleName}: ${e.message ?: ""}）"
    } ?: return "目录无法访问（未获得写入授权）"

    try {
        resolver.openOutputStream(doc)?.use { it.write(1) }
            ?: return "目录不允许写入"
    } catch (e: Exception) {
        runCatching { DocumentsContract.deleteDocument(resolver, doc) }
        return "写入测试失败（${e.javaClass.simpleName}: ${e.message ?: ""}）"
    }
    // delete is best-effort: a provider that refuses to delete the marker is
    // annoying but NOT a broken grant (1.0.93 wrongly failed every dir here).
    runCatching { DocumentsContract.deleteDocument(resolver, doc) }
    return null
}
