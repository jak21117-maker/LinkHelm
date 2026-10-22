package com.linkhelm.app

import android.util.Log
import java.io.InputStream
import java.net.InetSocketAddress
import java.net.Socket

/**
 * beta1.0.4 — unified upstream liveness manager.
 *
 * Before 1.0.4 the client opened THREE independent probes that each spun up
 * their own connection and fought for the same Wi-Fi Direct radio:
 *   1. a one-off TCP pre-check (DebugUtils.testProxyConnection),
 *   2. a per-tick SOCKS5 TCP greeting keepalive,
 *   3. a host-side UDP+DNS relay self-test (UdpProbe.socks5UdpDnsProbe).
 * Under concurrency these "three handshakes" collided (accept-queue and radio
 * wake-up storms) and inflated the active-connection / session count — a couple
 * of busy devices could exhaust the phone's limit.
 *
 * This manager collapses them into ONE negotiated handshake per tick and makes
 * the deep probe short-circuit on the FIRST success (per the 1.0.4 spec: one
 * good probe is enough — we no longer demand that all three chains pass). All
 * TCP/IP work stays native to the engine; this object only orchestrates.
 */
internal object UpstreamNetworkManager {

    /** Shared with the host auto-probe so both ends use the identical candidate order. */
    internal val UDP_SELF_TEST_DNS_POOL =
        listOf("119.29.29.29", "223.5.5.5", "114.114.114.114")

    /**
     * Single handshake. Opens ONE TCP connection, performs the SOCKS5 greeting
     * (offering no-auth + RFC1929 user/pass, matching the host's requirement),
     * and treats a successful method-selection reply as "link alive".
     *
     * Wrong credentials must NOT be mistaken for a healthy link — if the server
     * demands auth and ours fails we return false, exactly like the host's own
     * keepalive gate.
     */
    fun singleHandshake(
        proxyIp: String,
        proxyPort: Int,
        user: String,
        pass: String,
        timeoutMs: Int = 3000
    ): Boolean {
        return runCatching {
            Socket().use { s ->
                s.connect(InetSocketAddress(proxyIp, proxyPort), timeoutMs)
                s.soTimeout = timeoutMs
                val out = s.getOutputStream()
                val inp = s.getInputStream()
                // Offer no-auth and user/pass; the host picks.
                out.write(byteArrayOf(0x05, 0x02, 0x00, 0x02))
                out.flush()
                val method = ByteArray(2)
                if (!readFully(inp, method) || method[0] != 0x05.toByte()) return@runCatching false
                when (method[1].toInt() and 0xFF) {
                    0x02 -> {
                        out.write(Socks5Parser.buildUserPassRequest(user, pass))
                        out.flush()
                        val auth = ByteArray(2)
                        if (!readFully(inp, auth) || auth[1] != 0x00.toByte()) return@runCatching false
                    }
                    0x00 -> { /* no-auth accepted (legacy host) */ }
                    else -> return@runCatching false
                }
                true
            }
        }.getOrDefault(false)
    }

    /**
     * Deep probe through the SOCKS5 UDP relay. Tries the candidate DNS pool and
     * returns the FIRST DNS that replies (one good probe is sufficient), or null
     * if the whole pool fails. Reuses UdpProbe's proven relay path; only the
     * "all-must-pass" requirement of older releases is relaxed here.
     */
    fun deepProbe(
        socksHost: String,
        socksPort: Int,
        user: String?,
        pass: String?,
        pool: List<String> = UDP_SELF_TEST_DNS_POOL,
        timeoutMs: Int = 5000
    ): String? {
        for (dns in pool) {
            val err = UdpProbe.socks5UdpDnsProbe(
                socksHost, socksPort, dnsHost = dns,
                socksUser = user, socksPass = pass, timeoutMs = timeoutMs
            )
            if (err == null) return dns
            Log.i("UpstreamNet", "deep probe failed via $dns: $err; next candidate")
        }
        return null
    }

    private fun readFully(inp: InputStream, buf: ByteArray): Boolean {
        var r = 0
        while (r < buf.size) {
            val n = inp.read(buf, r, buf.size - r)
            if (n < 0) return false
            r += n
        }
        return true
    }
}
