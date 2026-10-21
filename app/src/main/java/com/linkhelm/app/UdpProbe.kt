package com.linkhelm.app

import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.Socket
import java.net.SocketTimeoutException
import java.util.Random

/**
 * v1.4.3 — UDP connectivity detection (pure java.net, JVM-testable).
 *
 * Two probe paths, covering both segments of the UDP chain:
 *  1. [dnsQueryViaUdp]  Direct UDP: send one DNS A query and wait for the response.
 *     Use: self-check after the VPN client connects (traffic goes tun2socks -> SOCKS5 UDP relay -> internet;
 *     a reply proves the full client -> host -> internet UDP path works).
 *  2. [socks5UdpDnsProbe] SOCKS5 UDP ASSOCIATE: complete the TCP handshake + UDP ASSOCIATE with the
 *     given SOCKS5 server, then send a DNS query through the relay. A reply proves the full
 *     client -> host UDP relay -> internet path (exactly what games/voice use).
 *
 * All functions return null on success, or an error description string with the failure reason (non-fatal).
 */
object UdpProbe {

    const val DEFAULT_DNS_HOST = "223.5.5.5" // Ali public DNS, stable UDP 53 reachability domestically
    const val DEFAULT_DNS_PORT = 53

    /** Builds a minimal DNS A query (example.com); the id is caller-supplied to match the response. */
    fun buildDnsQuery(id: Int, name: String = "example.com"): ByteArray {
        val out = mutableListOf<Byte>()
        fun b(v: Int) = out.add(v.toByte())
        b(id shr 8); b(id and 0xFF)      // ID
        b(0x01); b(0x00)                  // flags: RD=1
        b(0x00); b(0x01)                  // QDCOUNT=1
        b(0x00); b(0x00); b(0x00); b(0x00); b(0x00); b(0x00)
        for (label in name.split(".")) {
            b(label.length)
            for (ch in label.toByteArray()) b(ch.toInt() and 0xFF)
        }
        b(0x00)                           // root label
        b(0x00); b(0x01)                  // QTYPE=A
        b(0x00); b(0x01)                  // QCLASS=IN
        return out.toByteArray()
    }

    /** Response validation: sufficient length, DNS header, ID match, QR=1 (response bit). */
    fun isDnsResponse(buf: ByteArray, len: Int, id: Int): Boolean {
        if (len < 12) return false
        val respId = ((buf[0].toInt() and 0xFF) shl 8) or (buf[1].toInt() and 0xFF)
        if (respId != id) return false
        return (buf[2].toInt() and 0x80) != 0
    }

    /**
     * Path 1: direct UDP DNS query.
     * When called on a client with the VPN active, the datagram goes out via tun2socks -> the host SOCKS5 UDP relay;
     * receiving a response means the full-chain UDP path is connected.
     */
    fun dnsQueryViaUdp(
        dnsHost: String = DEFAULT_DNS_HOST,
        dnsPort: Int = DEFAULT_DNS_PORT,
        timeoutMs: Int = 5_000
    ): String? {
        val id = Random().nextInt(0xFFFF)
        val query = buildDnsQuery(id)
        return try {
            DatagramSocket().use { socket ->
                socket.soTimeout = timeoutMs
                val addr = InetAddress.getByName(dnsHost)
                socket.send(DatagramPacket(query, query.size, addr, dnsPort))
                val buf = ByteArray(1024)
                val resp = DatagramPacket(buf, buf.size)
                socket.receive(resp)
                if (isDnsResponse(buf, resp.length, id)) null
                else L10n.tr("Received UDP data but not a valid DNS response", "收到 UDP 数据但不是有效 DNS 响应")
            }
        } catch (e: SocketTimeoutException) {
            L10n.tr("UDP timeout (no response within ${timeoutMs} ms)", "UDP 超时（${timeoutMs}ms 内无响应）")
        } catch (e: Exception) {
            "${e.javaClass.simpleName}: ${e.message}"
        }
    }

    /**
     * Path 2: SOCKS5 UDP ASSOCIATE probe.
     * Completes the TCP handshake and UDP ASSOCIATE with (socksHost, socksPort), then sends a DNS query to
     * (dnsHost, dnsPort) through the relay and waits for the response.
     */
    fun socks5UdpDnsProbe(
        socksHost: String,
        socksPort: Int,
        dnsHost: String = DEFAULT_DNS_HOST,
        dnsPort: Int = DEFAULT_DNS_PORT,
        timeoutMs: Int = 5_000,
        // v1.5.43: the host requires RFC 1929 credentials — provide authentication when non-empty.
        socksUser: String? = null,
        socksPass: String? = null
    ): String? {
        var control: Socket? = null
        var relay: DatagramSocket? = null
        try {
            // --- TCP control channel: greeting + UDP ASSOCIATE ---
            control = Socket()
            control.connect(InetSocketAddress(socksHost, socksPort), timeoutMs)
            control.soTimeout = timeoutMs
            val output = control.getOutputStream()
            val input = control.getInputStream()

            // v1.5.43: offer both no-auth and user/pass; respond according to the server choice.
            output.write(byteArrayOf(0x05, 0x02, 0x00, 0x02))
            output.flush()
            val methodReply = ByteArray(2)
            if (!readFully(input, methodReply)) return L10n.tr("Incomplete SOCKS5 greeting response", "SOCKS5 问候响应不完整")
            if (methodReply[0] != 0x05.toByte()) return L10n.tr("SOCKS5 version error 0x%02x".format(methodReply[0]), "SOCKS5 版本错误 0x%02x".format(methodReply[0]))
            when (methodReply[1].toInt() and 0xFF) {
                0x02 -> {
                    val u = socksUser
                    val p = socksPass
                    if (u.isNullOrEmpty() || p.isNullOrEmpty()) return L10n.tr("Server requires credentials but none were provided", "服务器要求账号密码但未提供")
                    output.write(Socks5Parser.buildUserPassRequest(u, p))
                    output.flush()
                    val authReply = ByteArray(2)
                    if (!readFully(input, authReply)) return L10n.tr("Incomplete authentication response", "认证响应不完整")
                    if (authReply[1] != 0x00.toByte()) return L10n.tr("Authentication failed (wrong account or password)", "认证失败（账号或密码错误）")
                }
                0x00 -> { /* no-auth accepted (old server) */ }
                else -> return L10n.tr("Method rejected 0x%02x".format(methodReply[1]), "方法被拒绝 0x%02x".format(methodReply[1]))
            }

            // UDP ASSOCIATE request: ATYP=1, 0.0.0.0:0
            output.write(byteArrayOf(0x05, 0x03, 0x00, 0x01, 0, 0, 0, 0, 0, 0))
            output.flush()
            val head = ByteArray(4)
            if (!readFully(input, head)) return L10n.tr("Incomplete UDP ASSOCIATE response header", "UDP ASSOCIATE 响应头不完整")
            if (head[0] != 0x05.toByte()) return L10n.tr("UDP ASSOCIATE version error", "UDP ASSOCIATE 版本错误")
            if (head[1] != 0x00.toByte()) return L10n.tr("UDP ASSOCIATE rejected (reply=0x%02x)".format(head[1]), "UDP ASSOCIATE 被拒绝（reply=0x%02x）".format(head[1]))
            val bnd = when (head[3].toInt()) {
                0x01 -> {
                    val a = ByteArray(4)
                    if (!readFully(input, a)) return L10n.tr("Incomplete UDP ASSOCIATE address", "UDP ASSOCIATE 地址不完整")
                    a
                }
                0x03 -> {
                    val len = input.read()
                    if (len <= 0) return L10n.tr("Abnormal UDP ASSOCIATE domain length", "UDP ASSOCIATE 域名长度异常")
                    val a = ByteArray(len)
                    if (!readFully(input, a)) return L10n.tr("Incomplete UDP ASSOCIATE domain", "UDP ASSOCIATE 域名不完整")
                    a
                }
                0x04 -> {
                    val a = ByteArray(16)
                    if (!readFully(input, a)) return L10n.tr("Incomplete UDP ASSOCIATE IPv6 address", "UDP ASSOCIATE IPv6 地址不完整")
                    a
                }
                else -> return L10n.tr("Abnormal UDP ASSOCIATE ATYP 0x%02x".format(head[3]), "UDP ASSOCIATE ATYP 异常 0x%02x".format(head[3]))
            }
            val portBuf = ByteArray(2)
            if (!readFully(input, portBuf)) return L10n.tr("Incomplete UDP ASSOCIATE port", "UDP ASSOCIATE 端口不完整")
            val relayPort = ((portBuf[0].toInt() and 0xFF) shl 8) or (portBuf[1].toInt() and 0xFF)
            if (relayPort <= 0) return L10n.tr("Invalid UDP relay port", "UDP 中继端口无效")

            // by convention, reconnect to the SOCKS server address when BND.ADDR=0.0.0.0
            val relayHost = if (bnd.all { it == 0.toByte() }) socksHost
            else InetAddress.getByAddress(bnd).hostAddress ?: socksHost

            // --- UDP data plane: RSV FRAG ATYP ADDR PORT + DNS query ---
            relay = DatagramSocket()
            relay.soTimeout = timeoutMs
            val dnsAddr = InetAddress.getByName(dnsHost)
            val id = Random().nextInt(0xFFFF)
            val query = buildDnsQuery(id)
            val datagram = ByteArray(10 + 16 + query.size)
            var p = 0
            datagram[p++] = 0; datagram[p++] = 0          // RSV
            datagram[p++] = 0                              // FRAG
            datagram[p++] = 0x01                           // ATYP=IPv4
            val targetIp = dnsAddr.address
            System.arraycopy(targetIp, 0, datagram, p, 4); p += 4
            datagram[p++] = ((dnsPort shr 8) and 0xFF).toByte()
            datagram[p++] = (dnsPort and 0xFF).toByte()
            System.arraycopy(query, 0, datagram, p, query.size); p += query.size

            relay.send(DatagramPacket(datagram, p, InetAddress.getByName(relayHost), relayPort))
            val buf = ByteArray(2048)
            val resp = DatagramPacket(buf, buf.size)
            relay.receive(resp)
            if (resp.length < 10 + 12) return L10n.tr("UDP relay response too short", "UDP 中继响应过短")
            // strip the 10-byte SOCKS UDP header, then validate the DNS response
            val payload = buf.copyOfRange(10, resp.length)
            return if (isDnsResponse(payload, payload.size, id)) null
            else L10n.tr("Received UDP via the relay but not a valid DNS response", "经中继收到 UDP 但不是有效 DNS 响应")
        } catch (e: SocketTimeoutException) {
            return L10n.tr("UDP timeout (no response within ${timeoutMs} ms)", "UDP 超时（${timeoutMs}ms 内无响应）")
        } catch (e: Exception) {
            return "${e.javaClass.simpleName}: ${e.message}"
        } finally {
            runCatching { relay?.close() }
            runCatching { control?.close() } // close the control connection -> the host side reclaims the association
        }
    }

    private fun readFully(input: java.io.InputStream, buf: ByteArray): Boolean {
        var read = 0
        while (read < buf.size) {
            val n = input.read(buf, read, buf.size - read)
            if (n < 0) return false
            read += n
        }
        return true
    }
}
