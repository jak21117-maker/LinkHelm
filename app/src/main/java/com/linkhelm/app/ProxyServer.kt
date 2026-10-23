package com.linkhelm.app

import android.util.Log
import java.io.EOFException
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.net.SocketException
import java.net.SocketTimeoutException
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CountDownLatch
import java.util.concurrent.RejectedExecutionException
import java.util.concurrent.SynchronousQueue
import java.util.concurrent.ThreadFactory
import java.util.concurrent.ThreadPoolExecutor
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.atomic.AtomicReference
import kotlin.concurrent.thread

internal enum class ProxyLogLevel { DEBUG, WARN, ERROR }

internal fun interface ProxyLogger {
    fun log(level: ProxyLogLevel, message: String, error: Throwable?)
}

internal object AndroidProxyLogger : ProxyLogger {
    override fun log(level: ProxyLogLevel, message: String, error: Throwable?) {
        when (level) {
            ProxyLogLevel.DEBUG -> Log.d("ProxyServer", message)
            ProxyLogLevel.WARN -> Log.w("ProxyServer", message, error)
            ProxyLogLevel.ERROR -> Log.e("ProxyServer", message, error)
        }
    }
}

/**
 * Parses a dotted-quad CIDR range such as "192.168.49.0/24".
 * Returns (networkAddress, prefixLength) or null when malformed.
 */
internal fun parseCidr(cidr: String): Pair<Long, Int>? {
    val parts = cidr.split("/")
    if (parts.size != 2) return null
    val prefix = parts[1].toIntOrNull() ?: return null
    if (prefix < 0 || prefix > 32) return null
    val octets = parts[0].split(".")
    if (octets.size != 4) return null
    var addr = 0L
    for (octet in octets) {
        val value = octet.toIntOrNull() ?: return null
        if (value < 0 || value > 255) return null
        addr = (addr shl 8) or value.toLong()
    }
    return addr to prefix
}

internal fun ipMatchesCidr(ip: String, cidr: Pair<Long, Int>): Boolean {
    val octets = ip.split(".")
    if (octets.size != 4) return false
    var addr = 0L
    for (octet in octets) {
        val value = octet.toIntOrNull() ?: return false
        if (value < 0 || value > 255) return false
        addr = (addr shl 8) or value.toLong()
    }
    val prefix = cidr.second
    if (prefix == 0) return true
    val mask = (-1L shl (32 - prefix)) and 0xFFFFFFFFL
    return (addr and mask) == (cidr.first and mask)
}

/**
 * v1.4.8 (R3 timing fix): monotonic interval clock for idle/activity accounting.
 * Wall-clock (System.currentTimeMillis) jumps on NTP sync or manual time change,
 * which instantly aged out or froze every idle timer at once. Interval math only
 * — UI-facing timestamps deliberately keep wall-clock. Mirrors the Word plan's
 * `SystemClock.elapsedRealtime()` guidance and Python `time.monotonic()`.
 */
internal fun elapsedRealtimeMs(): Long = try {
    android.os.SystemClock.elapsedRealtime()
} catch (_: Throwable) {
    // JVM unit tests: android.jar is a stub. System.nanoTime is monotonic and
    // equally valid for interval math; both never jump with wall-clock.
    System.nanoTime() / 1_000_000L
}

/**
 * v1.4.4 — one row of the host-side "connected devices" roster: a downstream device
 * seen by the SOCKS5 server, how many connections it holds right now, when it
 * was last seen, and whether the host has blocked (kicked) it.
 */
data class ClientSnapshot(
    val ip: String,
    val activeConnections: Int,
    val lastSeenMs: Long,
    val blocked: Boolean
)

class ProxyServer internal constructor(
    config: Config,
    private val logger: ProxyLogger
) {
    constructor() : this(Config(), AndroidProxyLogger)

    internal constructor(config: Config) : this(config, ProxyLogger { _, _, _ -> })

    // v1.5.0: var with private set — the turbo-mode toggle resizes the
    // RUNNING pool live (applyTurboMode), so every reader (active-connection
    // ceiling, congestion detector, reject logging) follows without a share
    // restart. Reference swap is atomic; single-writer (UI main thread).
    // v1.5.44: @Volatile — applySocksCredentials swaps credentials live from
    // the settings page; without it handshake threads could keep seeing a
    // stale Config ("changed password has no effect").
    @Volatile
    internal var config: Config = config
        private set

    internal data class Config(
        val port: Int = 8080,
        // Minimal hardening: by default still bind to all interfaces for
        // backwards compatibility, but HostService must pass the P2P group owner address.
        val bindAddress: String = "0.0.0.0",
        // CIDR ranges allowed to connect (e.g. "192.168.49.0/24"). Empty list = allow all
        // (kept for unit tests / legacy behaviour). HostService restricts this to the
        // Wi-Fi Direct /24 so devices on other networks cannot use the open proxy.
        val allowedClientRanges: List<String> = emptyList(),
        val coreThreads: Int = 16,
        // beta1.0.51: ONE shared session budget — TCP and UDP are NO LONGER
        // rationed against each other (user contract: "TCP 和 UDP 不设比例").
        // Totals: STANDARD 800 / TURBO 1400; per device floats 350-400.
        val maxThreads: Int = 896,
        val maxConnectionsPerClient: Int = 375,
        // Global session budget = TCP sessions + UDP-association control
        // connections. Admission ceiling sized below the thread pool
        // (threads stay the reserve for accept/UDP-cleanup).
        val maxTcpSessions: Int = 800,
        val maxTcpSessionsPerClient: Int = 375,
        // UDP ceilings equal the TCP budget (the old ~sessions/3 3:1 ration
        // is gone). The per-device UDP ceiling follows the same dynamic
        // per-device quota as TCP (see udpPerDeviceCap).
        val maxUdpAssociations: Int = 800,
        val maxUdpAssociationsPerClient: Int = 400,
        // v1.5.45: accept backlog = admit queue limit.
        val maxAdmitQueue: Int = 256,
        val workerKeepAliveMs: Long = 60_000L,
        val connectTimeoutMs: Int = 15_000,
        val handshakeTimeoutMs: Int = 15_000,
        // v1.5.4: 60s -> 30s. This is ONLY the liveness poll slice (soTimeout
        // on the client/target sockets): each timeout is a harmless wakeup
        // that checks whether the peer is still there — established sessions
        // are NEVER reaped for idleness (v1.4.9 contract below). Halving it
        // means a vanished client's sockets/quota are released in <=30s
        // instead of <=60s, which keeps the per-device 350-400 session quota
        // from piling up during PC-side retry storms (v1.5.38 lesson).
        // connectTimeoutMs deliberately UNCHANGED (15s) — shortening it would
        // turn legitimately slow upstream dials into false failures.
        val socketPollIntervalMs: Int = 30_000,
        // beta1.0.3: established-but-idle TCP sessions are reclaimed after 3h
        // (field table: default 3h, experimental 6h in TURBO; every real
        // send/receive refreshes the timer). Active sessions are NEVER
        // reaped; this only bounds socket/FD waste from long-dead lobbies.
        val tcpIdleTimeoutMs: Long = 10_800_000L,
        val udpTargetTimeoutMs: Int = 30_000,
        // beta1.0.3: UDP target-endpoint idle reclamation. Field guidance:
        // start measuring at 300s; interactive/video UDP flows tolerate 600-900s.
        val udpIdleTimeoutMs: Long = 300_000L,
        // v1.4.9: how long the ≥3-device fair-share tier is HELD after the
        // last ≥3 observation, so a device briefly dropping its last connection
        // does not flip the ceiling for everyone (report section 4.1 debounce).
        val quotaDebounceMs: Long = 30_000L,
        // beta1.0.51: NEW-connection per-device ceiling while the shared
        // session budget is congested (≥95% for 15s enters, <95% for 5min
        // exits). User contract: 拥堵时 300 — and never below the
        // full-pressure floor of 200 (PRESSURE_MIN_QUOTA_PER_DEVICE). Normal
        // usage never touches this; it only applies when the shared budget
        // is nearly saturated.
        val congestionQuotaPerClient: Int = 300,
        // v1.5.2 (LinkHelm dual-lane): optional SECOND listening socket for
        // the Windows lane router's standby lane. 0 = disabled (legacy single
        // port). Both accept loops feed the SAME executor and the SAME
        // global/per-IP admission budget — the standby port is never a second
        // quota. Its accept loop is degradable: a failure there only closes
        // that socket, never the primary service. Semantics: -1 = disabled
        // (legacy single port, default), 0 = OS-assigned standby port (tests),
        // 1..65535 = explicit standby port (HostService passes 8082).
        val secondaryPort: Int = -1,
        val udpCleanupIntervalMs: Long = 120_000L,
        // v1.5.32: briefly wait for a slot on burst instead of refusing outright.
        // 0 = refuse immediately (tests / legacy behaviour). The wait runs on a
        // worker thread; the accept loop never blocks.
        val quotaWaitMs: Long = 0L,
        // v1.5.43: SOCKS5 credentials (RFC 1929) — the handshake must provide and pass them.
        // Built-in fixed values by default; editable in settings (8-13 alphanumeric), effective immediately.
        val socksUser: String = DEFAULT_SOCKS_USER,
        val socksPass: String = DEFAULT_SOCKS_PASS
    ) {
        init {
            require(port in 0..65535)
            require(bindAddress.isNotBlank())
            allowedClientRanges.forEach { require(parseCidr(it) != null) { "invalid CIDR range: $it" } }
            require(SOCKS_CREDENTIAL_REGEX.matches(socksUser)) { "socksUser must be 8-13 alnum" }
            require(SOCKS_CREDENTIAL_REGEX.matches(socksPass)) { "socksPass must be 8-13 alnum" }
            require(coreThreads > 0)
            require(maxThreads >= coreThreads)
            require(maxConnectionsPerClient in 1..maxThreads)
            require(maxTcpSessions in 1..maxThreads)
            require(maxTcpSessionsPerClient in 1..maxTcpSessions)
            require(maxUdpAssociations > 0)
            require(maxUdpAssociationsPerClient > 0)
            require(maxAdmitQueue > 0)
            require(workerKeepAliveMs > 0)
            require(connectTimeoutMs > 0)
            require(handshakeTimeoutMs > 0)
            require(socketPollIntervalMs > 0)
            require(tcpIdleTimeoutMs >= socketPollIntervalMs)
            require(udpTargetTimeoutMs > 0)
            require(udpIdleTimeoutMs > 0)
            require(quotaDebounceMs >= 0)
            // Only positivity is required: the overlay applies via
            // minOf(base, congestionQuotaPerClient), so it can never RAISE a
            // smaller configured base (small test configs rely on this).
            require(congestionQuotaPerClient > 0)
            require(secondaryPort >= -1 && secondaryPort <= 65535 && (secondaryPort !in 1..65535 || secondaryPort != port))
            require(udpCleanupIntervalMs > 0)
            require(quotaWaitMs >= 0)
        }
    }

    internal data class Metrics(
        val activeConnections: Int,
        val totalConnections: Int,
        val rejectedConnections: Int,
        val activeTcpSessions: Int,
        val activeUdpAssociations: Int,
        // beta1.0.51: TCP + UDP together — the number the shared session
        // budget and the PROTECT detector actually see.
        val activeSessions: Int,
        val activeByClient: Map<String, Int>,
        val executorActiveThreads: Int,
        val executorPoolSize: Int
    )

    private val running = AtomicBoolean(false)
    private val startLatch = CountDownLatch(1)
    private val connectionSequence = AtomicLong(0)
    private val udpAssociationSequence = AtomicLong(0)
    private val workerSequence = AtomicInteger(0)
    private val pipeSequence = AtomicInteger(0)

    private val activeConnections = AtomicInteger(0)
    private val totalConnections = AtomicInteger(0)
    private val rejectedConnections = AtomicInteger(0)
    private val activeByClient = ConcurrentHashMap<String, AtomicInteger>()

    // v1.5.45 (beta1.0.1): UDP associations get their OWN admission budget
    // (global + per-device). One UDP association costs a relay socket, a
    // receive thread and one response thread per target — previously
    // unbounded, which is how "compat mode" turned into a thread explosion.
    private val activeUdpGlobal = AtomicInteger(0)
    private val activeUdpByClient = ConcurrentHashMap<String, AtomicInteger>()

    // beta1.0.51: the fixed per-device tiers are REPLACED by ONE shared
    // session budget (TCP + UDP together, no ratio): a fair-share formula
    // (global budget spread across live devices, floored at 200 — 资源占满时
    // 最少两百) plus a slowly stepped dynamic per-device quota floating
    // 350-400 in BOTH modes. See perClientQuotaFor and the quota sampler in
    // start().

    /**
     * beta1.0.51: ONE shared per-device NEW-session quota for BOTH modes
     * (TCP + UDP associations together — no ratio). Floats 350-400, stepped
     * by ±5 every 30s from measured TOTAL session utilization — deliberately
     * slow and non-random (no jitter), so devices never see flapping ceilings.
     * Small explicit test configs (per-device cap < PER_DEVICE_MIN) bypass
     * this and stay authoritative at their configured value.
     */
    @Volatile internal var dynamicPerDeviceQuota: Int = PER_DEVICE_START

    // beta1.0.51: extreme-pressure (PROTECT) sustain timers. PROTECT engages
    // only after sessions stay ≥95% of the shared budget for 15s (filters
    // short CPU spikes), and relaxes only after <95% holds for 5min (user
    // contract: 低于 95% 持续 5min 退出) — no flapping at the edge.
    private val highPressureSinceMs = AtomicLong(0L)
    private val lowPressureSinceMs = AtomicLong(0L)

    // beta1.0.51: the ONE shared session budget (user contract: 总数 800 /
    // 1400, TCP 和 UDP 不设比例). Every admitted session — a TCP CONNECT or
    // a UDP association (its control connection AND the association slot) —
    // holds exactly one tick here. The global ceiling and the PROTECT
    // detector read THIS counter, so the two types can never starve each
    // other; the per-type counters remain for metrics/reporting only.
    private val activeSessions = AtomicInteger(0)

    // v1.4.9: rejections counted by cause (regression report §4.2) instead of
    // one anonymous number — surfaces whether pressure comes from the global
    // worker cap, the per-IP quota, or executor saturation.
    private val rejectedGlobalLimit = AtomicLong(0)
    private val rejectedPerClientLimit = AtomicLong(0)
    private val rejectedPoolSaturated = AtomicLong(0)

    // beta1.0.51: the ONE network-side pressure tuning (user contract:
    // "only under extreme congestion; never in normal operation"). When the
    // shared session budget stays ≥95% for 15s, NEW-session per-device
    // admission is tightened to config.congestionQuotaPerClient (300 —
    // 拥堵时 300, never below the 200 floor); it relaxes after <95% holds
    // for 5min. Idle path cost: one atomic read per accepted socket.
    // Established sessions are NEVER touched, handshake/self-test cadence
    // unchanged — normal usage is unaffected.
    internal val congestionActive = AtomicBoolean(false)

    companion object {
        // beta1.0.51: thread ceilings track the session profiles below
        // (sessions + accept/UDP-cleanup reserve). STANDARD 800 sessions /
        // TURBO 1400 (user contract: 普通模式总数 800、强力模式 1400).
        const val NORMAL_MAX_THREADS = 896
        const val TURBO_MAX_THREADS = 1536

        // beta1.0.51: ONE shared per-device session budget, floating 350-400
        // (user contract: 单设备 350-400 浮动), stepped ±5 per 30s sampler.
        // Applies to BOTH modes and to TCP+UDP together (no ratio).
        const val PER_DEVICE_MIN = 350
        const val PER_DEVICE_MAX = 400
        const val PER_DEVICE_START = 375
        const val PER_DEVICE_STEP = 5
        const val QUOTA_SAMPLE_INTERVAL_MS = 30_000L

        // beta1.0.51: PROTECT (资源占满) sustain windows — sessions ≥95% of
        // the shared budget for 15s to enter, <95% for 5min to leave (user
        // contract: 低于百分之九十五 5min 退出).
        const val PROTECT_ENTER_SUSTAIN_MS = 15_000L
        const val PROTECT_EXIT_SUSTAIN_MS = 300_000L

        // beta1.0.51: congestion-time per-device quota (user contract:
        // 拥堵时 300) and the full-pressure floor (资源占满时最少两百).
        const val CONGESTION_QUOTA_PER_DEVICE = 300
        const val PRESSURE_MIN_QUOTA_PER_DEVICE = 200

        // beta1.0.51: ONE shared session budget per profile — TCP and UDP
        // draw from the same totals (user contract: TCP 和 UDP 不设比例):
        //  - STANDARD: 800 sessions, 350-400 per device (PER_DEVICE_*), UDP
        //    idle 300s.
        //  - TURBO: 1400 sessions — the multi-device capacity tier; UDP idle
        //    relaxed to 600s.
        // COMPAT is gone: the relaxed UDP source-endpoint tracking that used
        // to be its micro-kernel is now ALWAYS ON (see UdpAssociation), and
        // compatMode remains a no-op switch kept for API compatibility.
        internal enum class SessionProfile(
            val maxThreads: Int,
            val maxTcpSessions: Int,
            val maxTcpSessionsPerClient: Int,
            val maxUdpAssociations: Int,
            val maxUdpAssociationsPerClient: Int,
            val maxAdmitQueue: Int,
            val udpIdleTimeoutMs: Long,
            val udpCleanupIntervalMs: Long,
            val tcpIdleTimeoutMs: Long
        ) {
            STANDARD(896, 800, 375, 800, 400, 256, 300_000L, 60_000L, 10_800_000L),
            TURBO(1536, 1400, 375, 1400, 400, 256, 600_000L, 90_000L, 21_600_000L)
        }

        // v1.5.43: SOCKS5 credentials (RFC 1929) defaults. System-initial and fixed,
        // never randomized — both ends (APK/EXE) hardcode the same values, interoperable out of the box;
        // users may manually change them in settings to 8-13 alphanumeric characters (manual only, no refresh entry).
        const val DEFAULT_SOCKS_USER = "linkhelm01"
        const val DEFAULT_SOCKS_PASS = "k7h2m9x4qa"

        /** v1.5.43: custom credential validation — 8-13 chars, letters and digits only. */
        val SOCKS_CREDENTIAL_REGEX = Regex("^[A-Za-z0-9]{8,13}$")
    }

    /**
     * v1.5.45 (beta1.0.1): apply a full resource profile in one atomic config
     * swap (threads, session caps, per-device quotas, queue, UDP idle/cleanup)
     * and resize the RUNNING pool. Effective immediately — established
     * sessions are never touched; only NEW admission follows the new caps.
     */
    internal fun applySessionProfile(profile: SessionProfile) {
        config = config.copy(
            maxThreads = profile.maxThreads,
            maxTcpSessions = profile.maxTcpSessions,
            maxTcpSessionsPerClient = profile.maxTcpSessionsPerClient,
            maxUdpAssociations = profile.maxUdpAssociations,
            maxUdpAssociationsPerClient = profile.maxUdpAssociationsPerClient,
            maxAdmitQueue = profile.maxAdmitQueue,
            udpIdleTimeoutMs = profile.udpIdleTimeoutMs,
            udpCleanupIntervalMs = profile.udpCleanupIntervalMs,
            tcpIdleTimeoutMs = profile.tcpIdleTimeoutMs
        )
        clientPool?.let { pool ->
            pool.corePoolSize = minOf(config.coreThreads, profile.maxThreads)
            pool.maximumPoolSize = profile.maxThreads
        }
        log(
            ProxyLogLevel.WARN,
            "Session profile ${profile.name}: worker limit ${profile.maxThreads}, " +
                "SHARED session budget ${profile.maxTcpSessions} (TCP+UDP together, no ratio; " +
                "per-device floats ${PER_DEVICE_MIN}-${PER_DEVICE_MAX}), " +
                "queue ${profile.maxAdmitQueue}, UDP idle ${profile.udpIdleTimeoutMs / 1000}s"
        )
    }

    /**
     * beta1.0.3: the profile actually in force. Compatibility is a
     * micro-kernel now and does NOT select a capacity profile.
     */
    internal fun activeProfile(): SessionProfile =
        if (config.maxThreads >= TURBO_MAX_THREADS) SessionProfile.TURBO else SessionProfile.STANDARD

    /**
     * beta1.0.51: live turbo-mode resize — swap the whole TURBO profile on
     * the RUNNING pool. 1400 shared sessions target MULTI-device total
     * capacity; it is not a single-device accelerator. The per-device
     * ceiling is the shared dynamic quota (see dynamicPerDeviceQuota and the
     * 30s sampler). Takes effect IMMEDIATELY; no share stop/start.
     */
    internal fun applyTurboMode(enabled: Boolean) {
        val target = if (enabled) TURBO_MAX_THREADS else NORMAL_MAX_THREADS
        if (config.maxThreads == target) return
        applySessionProfile(if (enabled) SessionProfile.TURBO else SessionProfile.STANDARD)
    }

    // beta1.0.51: the compatibility switch is now a NO-OP kept for API
    // compatibility. beta1.0.3 turned it into a micro-kernel that only gated
    // relaxed UDP source-endpoint tracking — but nobody ever called
    // applyCompatMode, so third-party clients that rotate source ports
    // (Karing/Streisand pools, power-saving socket rebuilds) had their
    // packets dropped and their replies misrouted ("UDP check OK, game gets
    // nothing"). As of beta1.0.51 that tracking is ALWAYS ON (see
    // UdpAssociation.relayPacket / acceptClientEndpoint), so toggling this
    // flag no longer changes any behaviour — the always-on minimum
    // compatibility (correct UDP BND.ADDR replies + lenient reserved bits +
    // per-target precise reply routing) covers every client.
    @Volatile internal var compatMode: Boolean = false
        private set

    internal fun applyCompatMode(enabled: Boolean) {
        if (compatMode == enabled) return
        compatMode = enabled
        log(
            ProxyLogLevel.WARN,
            "Compatibility switch ${if (enabled) "enabled" else "disabled"} " +
                "(no behavioural effect since beta1.0.51: multi-port UDP source tracking and " +
                "per-target precise replies are always on)"
        )
    }

    /**
     * v1.5.43: update the SOCKS5 credentials (8-13 alphanumeric), effective immediately — every new
     * handshake reads the live value from config; established sessions are unaffected. Returns false without changes on validation failure.
     * Manual changes only through this entry; there is no random/auto-refresh path.
     */
    internal fun applySocksCredentials(user: String, pass: String): Boolean {
        if (!SOCKS_CREDENTIAL_REGEX.matches(user) || !SOCKS_CREDENTIAL_REGEX.matches(pass)) {
            log(ProxyLogLevel.WARN, "Invalid SOCKS5 account/password format (8-13 alphanumeric required); not changed.")
            return false
        }
        if (config.socksUser == user && config.socksPass == pass) return true
        config = config.copy(socksUser = user, socksPass = pass)
        log(ProxyLogLevel.WARN, "SOCKS5 credentials updated (immediate; new connections must use the new credentials).")
        return true
    }

    // v1.4.9: hot-path metrics logging is throttled — per-connection
    // logMetrics() built a full snapshot (Map copy + executor stats) on every
    // accept/close, which under short-connection bursts showed up as GC/sched
    // pressure exactly when the accept loop needed to be fastest.
    private val lastMetricsLogAt = AtomicLong(0L)

    private fun isLoopbackIp(ip: String): Boolean =
        ip.startsWith("127.") || ip == "::1" || ip == "localhost"
    // v1.4.4: roster of every downstream IP seen this session (never removed on
    // disconnect so the host UI can show devices that come and go) + kick blocklist.
    private val seenClients = ConcurrentHashMap<String, AtomicLong>()
    private val blockedClients: MutableSet<String> = ConcurrentHashMap.newKeySet()
    private val activeClientSockets = ConcurrentHashMap.newKeySet<Socket>()
    private val activeTargetSockets = ConcurrentHashMap.newKeySet<Socket>()
    private val activeTcpSessions = ConcurrentHashMap.newKeySet<TcpSession>()
    private val udpAssociations = ConcurrentHashMap<Long, UdpAssociation>()

    @Volatile private var serverSocket: ServerSocket? = null

    // v1.5.0: internal (private set) — tests verify applyTurboMode resizes
    // the LIVE executor's maximumPoolSize, not just the config copy.
    @Volatile internal var clientPool: ThreadPoolExecutor? = null
        private set

    // v1.5.2 (LinkHelm dual-lane): standby listening socket. Degradable —
    // its accept loop closes ONLY itself on failure; the primary service and
    // the shared executor keep running (single-quota contract preserved).
    @Volatile private var secondaryServerSocket: ServerSocket? = null

    /** v1.5.2: bound standby port, or -1 (degraded single-port mode). Test-visible. */
    internal var secondaryListeningPort: Int = -1
        private set
    @Volatile private var serverThread: Thread? = null
    @Volatile private var cleanupThread: Thread? = null
    @Volatile private var quotaSamplerThread: Thread? = null
    @Volatile private var startFailure: Throwable? = null
    @Volatile private var listeningPort = -1

    fun start() {
        if (!running.compareAndSet(false, true)) {
            log(ProxyLogLevel.WARN, "Server already running")
            return
        }

        val executor = ThreadPoolExecutor(
            config.coreThreads,
            config.maxThreads,
            config.workerKeepAliveMs,
            TimeUnit.MILLISECONDS,
            SynchronousQueue(true),
            ThreadFactory { runnable ->
                Thread(runnable, "Proxy-Worker-${workerSequence.incrementAndGet()}").apply {
                    priority = Thread.NORM_PRIORITY + 1
                }
            },
            ThreadPoolExecutor.AbortPolicy()
        ).apply {
            allowCoreThreadTimeOut(true)
        }
        clientPool = executor

        serverThread = thread(name = "ProxyServer-Main", isDaemon = true) {
            runAcceptLoop(executor)
        }
        // v1.5.2 (LinkHelm dual-lane): optional standby listener. Failure to
        // bind or accept here degrades to single-port mode; it NEVER tears
        // down the primary loop or the shared executor.
        if (config.secondaryPort >= 0) {
            thread(name = "ProxyServer-Standby", isDaemon = true) {
                runSecondaryAcceptLoop(executor)
            }
        }
        cleanupThread = thread(name = "Proxy-UDP-Cleanup", isDaemon = true) {
            runUdpCleanup()
        }
        // beta1.0.51: 30s quota sampler — slowly steps the SHARED per-device
        // quota (±5, bounded 350-400) from measured TOTAL session utilization
        // (TCP + UDP). Runs in BOTH modes now (user contract: 单设备 350-400
        // 浮动 applies to 普通模式 too). Deliberately NOT random jitter:
        // devices never see flapping ceilings.
        quotaSamplerThread = thread(name = "Proxy-Quota-Sampler", isDaemon = true) {
            while (running.get()) {
                try {
                    Thread.sleep(QUOTA_SAMPLE_INTERVAL_MS)
                } catch (_: InterruptedException) {
                    break
                }
                if (!running.get()) break
                // Small explicit test configs keep their own static ceiling.
                if (config.maxTcpSessionsPerClient < PER_DEVICE_MIN) continue
                val sessions = activeSessions.get()
                val cap = config.maxTcpSessions
                if (cap <= 0) continue
                val utilPct = sessions * 100 / cap
                val old = dynamicPerDeviceQuota
                val next = when {
                    utilPct >= 85 -> maxOf(PER_DEVICE_MIN, old - PER_DEVICE_STEP)
                    utilPct <= 70 -> minOf(PER_DEVICE_MAX, old + PER_DEVICE_STEP)
                    else -> old
                }
                if (next != old) {
                    dynamicPerDeviceQuota = next
                    log(
                        ProxyLogLevel.WARN,
                        "Per-device quota stepped $old -> $next (sessions $sessions/$cap, 30s sampler)"
                    )
                }
            }
        }
    }

    fun stop() {
        val wasRunning = running.getAndSet(false)
        if (wasRunning) log(ProxyLogLevel.DEBUG, "Stopping server...")

        tryClose(serverSocket)
        tryClose(secondaryServerSocket)
        secondaryServerSocket = null
        cleanupThread?.interrupt()
        quotaSamplerThread?.interrupt()

        activeTcpSessions.toList().forEach { it.close() }
        udpAssociations.values.toList().forEach { it.close() }
        activeClientSockets.toList().forEach { tryClose(it) }
        activeTargetSockets.toList().forEach { tryClose(it) }

        clientPool?.shutdownNow()
        logMetrics("Server stop requested")
    }

    internal fun awaitStarted(timeoutMs: Long = 5_000L): Boolean =
        startLatch.await(timeoutMs, TimeUnit.MILLISECONDS) && startFailure == null && listeningPort >= 0

    internal fun awaitStopped(timeoutMs: Long = 5_000L): Boolean {
        val deadline = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(timeoutMs)
        serverThread?.join(remainingMillis(deadline))
        cleanupThread?.join(remainingMillis(deadline))
        quotaSamplerThread?.join(remainingMillis(deadline))
        val executor = clientPool
        if (executor != null) executor.awaitTermination(remainingMillis(deadline), TimeUnit.MILLISECONDS)
        return serverThread?.isAlive != true && cleanupThread?.isAlive != true &&
            quotaSamplerThread?.isAlive != true && executor?.isTerminated != false
    }

    internal fun localPort(): Int = listeningPort

    /** v1.4.4: current downstream-device roster for the host UI. */
    fun clientSnapshot(): List<ClientSnapshot> {
        val ips = (seenClients.keys + activeByClient.keys).toSortedSet()
        return ips.map { ip ->
            ClientSnapshot(
                ip = ip,
                activeConnections = activeByClient[ip]?.get() ?: 0,
                lastSeenMs = seenClients[ip]?.get() ?: 0L,
                blocked = ip in blockedClients
            )
        }
    }

    /**
     * v1.4.4: kick a downstream device — immediately close every socket and UDP
     * association it holds, and block it from reconnecting until the host shares
     * again (stop/start resets the blocklist). Returns how many live sockets closed.
     */
    fun kickClient(ip: String): Int {
        blockedClients.add(ip)
        seenClients.putIfAbsent(ip, AtomicLong(System.currentTimeMillis()))?.set(System.currentTimeMillis())
        var closed = 0
        activeClientSockets.toList().forEach { socket ->
            if (socket.inetAddress?.hostAddress == ip) {
                tryClose(socket)
                closed++
            }
        }
        udpAssociations.values.toList().forEach { association ->
            if (association.clientIp == ip) association.close()
        }
        log(ProxyLogLevel.WARN, "Kicked client $ip (closed $closed sockets, blocked until restart)")
        return closed
    }

    /** v1.4.4: lift a kick; the device may connect again immediately. */
    fun unblockClient(ip: String): Boolean = blockedClients.remove(ip)

    /** v1.4.4: LIVE read-only view for the HTTP proxy module to enforce the same blocklist. */
    fun blockedClientsView(): Set<String> = java.util.Collections.unmodifiableSet(blockedClients)

    internal fun metrics(): Metrics {
        val executor = clientPool
        return Metrics(
            activeConnections = activeConnections.get(),
            totalConnections = totalConnections.get(),
            rejectedConnections = rejectedConnections.get(),
            activeTcpSessions = activeTcpSessions.size,
            activeUdpAssociations = udpAssociations.size,
            activeSessions = activeSessions.get(),
            activeByClient = activeByClient.mapValues { it.value.get() },
            executorActiveThreads = executor?.activeCount ?: 0,
            executorPoolSize = executor?.poolSize ?: 0
        )
    }

    private fun runAcceptLoop(executor: ThreadPoolExecutor) {
        try {
            val socket = ServerSocket().apply {
                reuseAddress = true
                receiveBufferSize = 256 * 1024
                // Minimal hardening: bind to the configured address (HostService passes the
                // Wi-Fi Direct group owner IP) instead of always exposing 0.0.0.0.
                bind(InetSocketAddress(config.bindAddress, config.port), config.maxAdmitQueue)
            }
            serverSocket = socket
            listeningPort = socket.localPort
            startLatch.countDown()

            if (!running.get()) {
                tryClose(socket)
                return
            }

            log(
                ProxyLogLevel.DEBUG,
                "Proxy server started on port $listeningPort " +
                    "(workers=${config.coreThreads}-${config.maxThreads}, perClient=${config.maxConnectionsPerClient})"
            )

            while (running.get() && !socket.isClosed) {
                try {
                    val client = socket.accept()
                    try {
                        configureAcceptedSocket(client)
                    } catch (error: Exception) {
                        tryClose(client)
                        throw error
                    }
                    totalConnections.incrementAndGet()
                    dispatchClient(executor, client)
                } catch (error: SocketException) {
                    if (running.get()) log(ProxyLogLevel.ERROR, "Accept failed", error)
                } catch (error: IOException) {
                    if (running.get()) log(ProxyLogLevel.ERROR, "Accept failed", error)
                }
            }
        } catch (error: Throwable) {
            startFailure = error
            running.set(false)
            startLatch.countDown()
            executor.shutdownNow()
            cleanupThread?.interrupt()
            log(ProxyLogLevel.ERROR, "Proxy server failed to start", error)
        } finally {
            tryClose(serverSocket)
            if (running.get()) {
                log(ProxyLogLevel.ERROR, "Accept loop exited while the proxy was still marked running")
                stop()
            }
        }
    }

    /**
     * v1.5.2 (LinkHelm dual-lane): standby accept loop on config.secondaryPort.
     * Accepts into the SAME executor and admission budget as the primary —
     * the standby port is a second door into one house, never a second house.
     * Any failure here (bind conflict, accept error) closes ONLY this socket
     * and degrades to single-port mode; stop() and the primary loop are the
     * only paths that may take the whole service down.
     */
    private fun runSecondaryAcceptLoop(executor: ThreadPoolExecutor) {
        val socket: ServerSocket = try {
            ServerSocket().apply {
                // No reuseAddress on the standby: a LISTEN socket has no
                // TIME_WAIT of its own, and on Windows SO_REUSEADDR would let
                // this bind succeed even over an active port (hijack), which
                // would silently defeat the degrade-on-conflict contract.
                receiveBufferSize = 256 * 1024
                bind(InetSocketAddress(config.bindAddress, config.secondaryPort), config.maxAdmitQueue)
            }
        } catch (error: Exception) {
            log(
                ProxyLogLevel.WARN,
                "Secondary channel :${config.secondaryPort} bind failed; degraded to single channel (primary unaffected)",
                error
            )
            return
        }
        if (!running.get()) {
            tryClose(socket)
            return
        }
        secondaryServerSocket = socket
        secondaryListeningPort = socket.localPort
        log(ProxyLogLevel.DEBUG, "Secondary channel ready :$secondaryListeningPort (shares the thread pool and connection quota with the primary)")

        while (running.get() && !socket.isClosed) {
            val client = try {
                socket.accept()
            } catch (error: IOException) {
                if (running.get() && !socket.isClosed) {
                    log(ProxyLogLevel.WARN, "Secondary channel accept error; closing the secondary entry (primary unaffected)", error)
                }
                break
            }
            try {
                configureAcceptedSocket(client)
            } catch (error: Exception) {
                tryClose(client)
                log(ProxyLogLevel.WARN, "Secondary channel socket configuration failed; connection closed", error)
                continue
            }
            totalConnections.incrementAndGet()
            dispatchClient(executor, client, laneTag = "B")
        }
        tryClose(socket)
        if (secondaryServerSocket === socket) {
            secondaryServerSocket = null
            secondaryListeningPort = -1
        }
        log(ProxyLogLevel.DEBUG, "Secondary channel exited (single-channel mode continues serving)")
    }

    private fun configureAcceptedSocket(client: Socket) {
        client.tcpNoDelay = true
        client.keepAlive = true
        client.soTimeout = config.handshakeTimeoutMs
    }

    private fun dispatchClient(executor: ThreadPoolExecutor, client: Socket, laneTag: String = "A") {
        val clientIp = client.inetAddress.hostAddress ?: "unknown"
        seenClients.computeIfAbsent(clientIp) { AtomicLong(0) }.set(System.currentTimeMillis())
        // v1.4.4: host-issued kick — silently drop before any handshake work.
        if (clientIp in blockedClients) {
            rejectClient(client, clientIp, "blocked by host (kicked)")
            return
        }
        // Minimal hardening: reject clients outside the configured source ranges before the
        // SOCKS5 handshake. Empty ranges = allow all (legacy/test behaviour).
        if (config.allowedClientRanges.isNotEmpty()) {
            val allowed = config.allowedClientRanges.any { range ->
                parseCidr(range)?.let { ipMatchesCidr(clientIp, it) } == true
            }
            if (!allowed) {
                rejectClient(client, clientIp, "source not allowed (outside P2P range)")
                return
            }
        }
        val rejectReason = reserveConnection(clientIp)
        if (rejectReason != null) {
            if (config.quotaWaitMs > 0) {
                // v1.5.32: absorb short bursts by waiting on a worker thread
                // for a slot instead of refusing — a saturated pool still
                // refuses immediately via RejectedExecutionException, and
                // the accept loop is never blocked.
                try {
                    executor.execute {
                        val waited = awaitQuotaSlot(clientIp, config.quotaWaitMs)
                        if (waited == null) {
                            admitClient(executor, client, clientIp, laneTag)
                        } else {
                            rejectClient(client, clientIp, waited)
                        }
                    }
                } catch (error: RejectedExecutionException) {
                    rejectedPoolSaturated.incrementAndGet()
                    rejectClient(client, clientIp, "worker pool saturated", error)
                }
            } else {
                rejectClient(client, clientIp, rejectReason)
            }
            return
        }
        admitClient(executor, client, clientIp, laneTag)
    }

    private fun admitClient(executor: ThreadPoolExecutor, client: Socket, clientIp: String, laneTag: String) {
        activeClientSockets.add(client)
        try {
            executor.execute { handleClient(client, clientIp, laneTag) }
            logMetrics("Accepted client $clientIp (lane $laneTag)")
        } catch (error: RejectedExecutionException) {
            activeClientSockets.remove(client)
            releaseConnection(clientIp)
            rejectedPoolSaturated.incrementAndGet()
            rejectClient(client, clientIp, "worker pool saturated", error)
        }
    }

    /**
     * v1.5.32: poll reserveConnection every 100ms for up to [waitMs].
     * Returns null once a slot is reserved, or the final rejection reason.
     * reserveConnection rolls its global counter back on every failure, so
     * retrying never leaks counts.
     */
    private fun awaitQuotaSlot(clientIp: String, waitMs: Long): String? {
        val deadline = System.currentTimeMillis() + waitMs
        while (true) {
            val reason = reserveConnection(clientIp)
            if (reason == null) return null
            if (System.currentTimeMillis() >= deadline) return reason
            try {
                Thread.sleep(100)
            } catch (_: InterruptedException) {
                return reason
            }
        }
    }

    /**
     * beta1.0.51: dynamic per-device NEW-session quota (TCP + UDP share it —
     * no ratio), evaluated inside the reserve critical section so admission
     * decisions are consistent.
     *
     *  - ≤2 devices → the shared dynamic quota (floats 350-400; small
     *    explicit test configs keep their own static ceiling);
     *  - ≥3 devices → fair share of the shared session budget
     *    (maxTcpSessions / N), floored at PRESSURE_MIN_QUOTA_PER_DEVICE (200)
     *    — "资源占满时最少两百" — and the global cap binds first;
     *  - congestion (PROTECT) overlays CONGESTION_QUOTA_PER_DEVICE (300) —
     *    "拥堵时 300".
     *
     * Only NEW sessions are gated; a session that outlives a quota cut is
     * never closed by this mechanism. Loopback self-test IPs never count
     * toward N.
     */
    internal fun perClientQuotaFor(deviceCountIncludingNew: Int): Int {
        // beta1.0.51: big profiles float on the shared dynamic quota; small
        // explicit configs (unit tests) stay authoritative at their own value.
        val cap = if (config.maxTcpSessionsPerClient >= PER_DEVICE_MIN) {
            dynamicPerDeviceQuota
        } else {
            config.maxTcpSessionsPerClient
        }
        val base = if (deviceCountIncludingNew <= 2) {
            cap
        } else {
            minOf(
                cap,
                maxOf(PRESSURE_MIN_QUOTA_PER_DEVICE, config.maxTcpSessions / deviceCountIncludingNew)
            )
        }
        // congestion overlay — tightens NEW-session admission only; existing
        // sessions and the exit path are untouched. For real (big) profiles
        // the result never drops below the full-pressure floor of 200: the
        // overlay is 300 and the fair-share floor is 200.
        return if (congestionActive.get()) minOf(base, CONGESTION_QUOTA_PER_DEVICE) else base
    }

    /** v1.4.9: real downstream devices counted for the quota tier (loopback excluded). */
    internal fun quotaDeviceCount(clientIp: String, isNewSource: Boolean): Int {
        var count = 0
        for (ip in activeByClient.keys) {
            if (!isLoopbackIp(ip)) count++
        }
        if (isNewSource && !isLoopbackIp(clientIp)) count++
        return count
    }

    /** v1.4.9: returns the rejection reason, or null when the connection is admitted. */
    private fun reserveConnection(clientIp: String): String? {
        while (true) {
            // v1.5.45: the global ceiling is the SESSION cap, not the thread
            // count. Threads are sized above the cap; admitting past the cap
            // is what made active connections explode.
            // beta1.0.51: the cap applies to the SHARED budget — TCP sessions
            // and UDP associations hold ticks in the same counter.
            val current = activeSessions.get()
            if (current >= config.maxTcpSessions) {
                rejectedGlobalLimit.incrementAndGet()
                return "global session limit reached"
            }
            if (activeSessions.compareAndSet(current, current + 1)) break
        }
        activeConnections.incrementAndGet()

        // beta1.0.3: extreme-pressure (PROTECT) detector with SUSTAIN windows —
        // enters only after sessions hold ≥95% of the shared budget for 15s
        // (filters short CPU spikes), leaves only after <95% holds for 5min
        // (user contract: 低于 95% 持续 5min 退出). The hysteresis band keeps
        // the mode from flapping near the edge.
        val poolLevel = activeSessions.get()
        val now = elapsedRealtimeMs()
        if (poolLevel * 100 >= config.maxTcpSessions * 95) {
            lowPressureSinceMs.set(0L)
            if (highPressureSinceMs.get() == 0L) highPressureSinceMs.set(now)
            if (!congestionActive.get() &&
                now - highPressureSinceMs.get() >= PROTECT_ENTER_SUSTAIN_MS &&
                congestionActive.compareAndSet(false, true)
            ) {
                log(
                    ProxyLogLevel.WARN,
                    "Extreme pressure PROTECT engaged (sessions $poolLevel/${config.maxTcpSessions} >= 95% for 15s): " +
                        "per-device NEW-session admission tightened to ${config.congestionQuotaPerClient}; established connections are unaffected"
                )
            }
        } else {
            highPressureSinceMs.set(0L)
            if (poolLevel * 100 < config.maxTcpSessions * 95) {
                if (lowPressureSinceMs.get() == 0L) lowPressureSinceMs.set(now)
                if (congestionActive.get() &&
                    now - lowPressureSinceMs.get() >= PROTECT_EXIT_SUSTAIN_MS &&
                    congestionActive.compareAndSet(true, false)
                ) {
                    log(
                        ProxyLogLevel.WARN,
                        "Pressure eased RECOVER (sessions <95% for 5min): normal per-device admission restored"
                    )
                }
            } else {
                lowPressureSinceMs.set(0L)
            }
        }

        var reserved = false
        activeByClient.compute(clientIp) { _, existing ->
            val counter = existing ?: AtomicInteger(0)
            // A brand-new IP is not in activeByClient yet — count it; the
            // loopback self-test never inflates the device count.
            val deviceCount = quotaDeviceCount(clientIp, existing == null)
            // beta1.0.51: the per-device budget is SHARED — this device's TCP
            // sessions and UDP associations count together against one quota
            // (user contract: TCP 和 UDP 不设比例). The cross-map read is a
            // snapshot; a simultaneous TCP+UDP burst for the same IP may
            // over-admit by a session or two — the hard global cap still binds.
            val udpCount = activeUdpByClient[clientIp]?.get() ?: 0
            if (counter.get() + udpCount < perClientQuotaFor(deviceCount)) {
                counter.incrementAndGet()
                reserved = true
            }
            counter
        }
        if (!reserved) {
            activeSessions.decrementAndGet()
            activeConnections.decrementAndGet()
            rejectedPerClientLimit.incrementAndGet()
            return "per-client connection limit reached"
        }
        return null
    }

    private fun releaseConnection(clientIp: String) {
        activeSessions.decrementAndGet()
        activeConnections.decrementAndGet()
        activeByClient.computeIfPresent(clientIp) { _, counter ->
            if (counter.decrementAndGet() <= 0) null else counter
        }
    }

    /**
     * beta1.0.51: per-device UDP ceiling = that device's live SHARED session
     * quota — NO TCP:UDP ratio (user contract: TCP 和 UDP 不设比例), clamped by
     * the profile's per-device UDP cap. Field data: a PC game holds 30-50 UDP
     * endpoints, a phone/tablet game 20-30 — the old sessions/3 ration (46-53)
     * starved exactly those apps while the single-packet probe still passed.
     */
    private fun udpPerDeviceCap(clientIp: String): Int {
        val sessionCap = perClientQuotaFor(quotaDeviceCount(clientIp, false))
        return minOf(config.maxUdpAssociationsPerClient, sessionCap)
    }

    /**
     * v1.5.45 (beta1.0.1): UDP admission budget. Returns the rejection reason,
     * or null when the association may start. Rejections are surfaced to the
     * client as a SOCKS5 general failure (see handleUdpAssociate), never as a
     * silent hang — third-party clients retry blindly on timeouts.
     */
    private fun reserveUdp(clientIp: String): String? {
        // beta1.0.51: UDP associations draw from the SAME shared session
        // budget as TCP (user contract: TCP 和 UDP 不设比例) — the per-type
        // counters remain for metrics/reporting only.
        while (true) {
            val current = activeSessions.get()
            if (current >= config.maxTcpSessions) return "global session limit reached"
            if (activeSessions.compareAndSet(current, current + 1)) break
        }
        activeUdpGlobal.incrementAndGet()
        var reserved = false
        activeUdpByClient.compute(clientIp) { _, existing ->
            val counter = existing ?: AtomicInteger(0)
            // The per-device budget is shared: this device's TCP sessions
            // (activeByClient) count against the same quota.
            val tcpCount = activeByClient[clientIp]?.get() ?: 0
            if (counter.get() + tcpCount < udpPerDeviceCap(clientIp)) {
                counter.incrementAndGet()
                reserved = true
            }
            counter
        }
        if (!reserved) {
            activeSessions.decrementAndGet()
            activeUdpGlobal.decrementAndGet()
            return "per-client UDP session limit reached"
        }
        return null
    }

    private fun releaseUdp(clientIp: String) {
        activeSessions.decrementAndGet()
        activeUdpGlobal.decrementAndGet()
        activeUdpByClient.computeIfPresent(clientIp) { _, counter ->
            if (counter.decrementAndGet() <= 0) null else counter
        }
    }

    private fun rejectClient(client: Socket, clientIp: String, reason: String, error: Throwable? = null) {
        rejectedConnections.incrementAndGet()
        // v1.5.3: answer with the standard SOCKS5 refusal (\x05\xff, NO
        // ACCEPTABLE METHODS) before closing. A bare close looked like a
        // forced RST to tun2socks ("connection was forcibly closed by the
        // remote host") and left it retrying blindly; a protocol-level
        // refusal fails fast and cleanly. Best-effort: the peer may already
        // be gone — never let this throw.
        try {
            client.getOutputStream().write(byteArrayOf(0x05, (0xFF).toByte()))
            client.getOutputStream().flush()
        } catch (_: Exception) {
        }
        tryClose(client)
        val snapshot = metrics()
        log(
            ProxyLogLevel.WARN,
            "Rejected client $clientIp: $reason " +
                "(sessions=${snapshot.activeSessions}/${config.maxTcpSessions} shared TCP+UDP, " +
                "client=${snapshot.activeByClient[clientIp] ?: 0}/${config.maxTcpSessionsPerClient}, " +
                "executor=${snapshot.executorActiveThreads}/${config.maxThreads})",
            error
        )
    }

    private fun handleClient(client: Socket, clientIp: String, laneTag: String = "A") {
        val clientId = "$clientIp:${client.port}#${connectionSequence.incrementAndGet()}(lane $laneTag)"
        try {
            val input = client.getInputStream()
            val output = client.getOutputStream()
            val greeting = Socks5Parser.readGreeting(input)
            // v1.5.43: host enforces SOCKS5 credential authentication (RFC 1929). The client greeting
            // must offer the 0x02 method; offering nothing is rejected (no no-auth fallback — an open
            // unauthenticated port used to be a backdoor for third-party clients).
            if (!greeting.supportsUserPass) {
                output.write(Socks5Parser.buildMethodSelection(Socks5Parser.METHOD_NOT_ACCEPTABLE))
                output.flush()
                log(ProxyLogLevel.WARN, "[$clientId] Client did not offer username/password authentication")
                return
            }

            output.write(Socks5Parser.buildMethodSelection(Socks5Parser.METHOD_USERPASS))
            output.flush()
            val (givenUser, givenPass) = Socks5Parser.readUserPass(input)
            val authOk = givenUser == config.socksUser && givenPass == config.socksPass
            output.write(Socks5Parser.buildUserPassReply(success = authOk))
            output.flush()
            if (!authOk) {
                // v1.5.43: don't leak which side mismatched in the log.
                log(ProxyLogLevel.WARN, "[$clientId] Incorrect SOCKS5 account or password; connection rejected.")
                return
            }
            log(ProxyLogLevel.DEBUG, "[$clientId] SOCKS5 authentication passed.")

            val request = try {
                Socks5Parser.readRequest(input)
            } catch (error: Socks5Parser.ProtocolException) {
                sendReply(output, error.replyCode)
                throw error
            }

            when (request.command) {
                Socks5Parser.COMMAND_CONNECT -> {
                    if (request.targetPort == 0) {
                        sendReply(output, Socks5Parser.REPLY_CONNECTION_REFUSED)
                        return
                    }
                    handleTcpConnect(client, input, output, request, clientId)
                }
                Socks5Parser.COMMAND_UDP_ASSOCIATE ->
                    handleUdpAssociate(client, input, output, clientIp, clientId)
                else -> {
                    sendReply(output, Socks5Parser.REPLY_COMMAND_NOT_SUPPORTED)
                    log(ProxyLogLevel.WARN, "[$clientId] Unsupported command: ${request.command}")
                }
            }
        } catch (error: EOFException) {
            log(ProxyLogLevel.DEBUG, "[$clientId] Client closed during SOCKS negotiation")
        } catch (error: SocketTimeoutException) {
            log(ProxyLogLevel.WARN, "[$clientId] SOCKS negotiation timed out")
        } catch (error: Socks5Parser.ProtocolException) {
            log(ProxyLogLevel.WARN, "[$clientId] Invalid SOCKS request: ${error.message}")
        } catch (error: Exception) {
            if (running.get()) log(ProxyLogLevel.ERROR, "[$clientId] Client error", error)
        } finally {
            activeClientSockets.remove(client)
            tryClose(client)
            releaseConnection(clientIp)
            logMetrics("[$clientId] Connection closed")
        }
    }

    private fun handleTcpConnect(
        client: Socket,
        input: InputStream,
        output: OutputStream,
        request: Socks5Parser.Request,
        clientId: String
    ) {
        val target = Socket().apply {
            tcpNoDelay = true
            keepAlive = true
            soTimeout = config.socketPollIntervalMs
        }
        client.soTimeout = config.socketPollIntervalMs

        var session: TcpSession? = null
        activeTargetSockets.add(target)
        try {
            if (!running.get()) throw SocketException("Proxy server is stopping")
            target.connect(InetSocketAddress(request.targetHost, request.targetPort), config.connectTimeoutMs)
            session = TcpSession(clientId, client, target)
            activeTcpSessions.add(session)

            sendReply(output, Socks5Parser.REPLY_SUCCEEDED)
            log(ProxyLogLevel.DEBUG, "[$clientId] TCP connected to ${request.targetHost}:${request.targetPort}")
            session.run(input, output)
        } catch (error: Exception) {
            if (session == null) sendReply(output, Socks5Parser.REPLY_CONNECTION_REFUSED)
            if (running.get()) {
                log(
                    ProxyLogLevel.ERROR,
                    "[$clientId] TCP connection to ${request.targetHost}:${request.targetPort} failed",
                    error
                )
            }
        } finally {
            session?.close() ?: tryClose(target)
            activeTargetSockets.remove(target)
        }
    }

    private fun handleUdpAssociate(
        client: Socket,
        input: InputStream,
        output: OutputStream,
        clientIp: String,
        clientId: String
    ) {
        var association: UdpAssociation? = null
        // v1.5.45 (beta1.0.1): UDP admission budget — fail fast with a
        // protocol-level refusal instead of creating yet another relay socket
        // + receive thread + per-target response threads.
        val udpReject = reserveUdp(clientIp)
        if (udpReject != null) {
            rejectedConnections.incrementAndGet()
            sendReply(output, Socks5Parser.REPLY_GENERAL_FAILURE)
            log(ProxyLogLevel.WARN, "[$clientId] UDP ASSOCIATE refused: $udpReject")
            return
        }
        try {
            // v1.5.42 (critical bug fix): the relay socket binds to the local address of the
            // control connection, and BND.ADDR replies with the same address. v1.5.41 used
            // "first site-local IPv4 from enumerating all interfaces" — on a phone that picks the
            // cellular rmnet 10.x address: the PC cannot reach it for return UDP (all in-tunnel DNS
            // fails), and the local probe UDP source address no longer equals clientIp so packets are dropped.
            // The control connection localAddress is naturally the "local address reachable by the client"
            // (P2P client -> 192.168.49.1; local self-check -> 127.0.0.1), correct per client.
            //
            // beta1.0.52 (ROOT CAUSE of "UDP dead through the tunnel on Windows"):
            // `DatagramSocket()` ALREADY BINDS to the wildcard in its constructor —
            // calling bind() afterwards always threw "Already bound", the runCatching
            // fell back to DatagramSocket(0) and BND.ADDR was replied as 0.0.0.0.
            // hev-socks5-tunnel then connect()s its UDP socket to 0.0.0.0 — fine on
            // Linux, but Windows refuses it with WSAEADDRNOTAVAIL (10049), so EVERY
            // tunneled UDP session (all DNS!) died while the direct relay probe
            // still answered. Construct the socket UNBOUND (null), bind to the
            // control-local address, and — belt and braces — always advertise the
            // control-local address as BND.ADDR (a wildcard-bound relay socket still
            // receives datagrams addressed to it, so the advertised address is safe
            // even if the specific bind failed).
            val controlLocal = runCatching { client.localAddress }.getOrNull()
            val relaySocket = if (controlLocal is java.net.Inet4Address &&
                !controlLocal.isAnyLocalAddress
            ) {
                runCatching {
                    DatagramSocket(null as java.net.SocketAddress?).apply {
                        reuseAddress = true
                        bind(java.net.InetSocketAddress(controlLocal, 0))
                    }
                }.getOrNull() ?: DatagramSocket(null as java.net.SocketAddress?)
                    .apply { bind(java.net.InetSocketAddress(0)) }
            } else {
                DatagramSocket(null as java.net.SocketAddress?)
                    .apply { bind(java.net.InetSocketAddress(0)) }
            }
            val bndAddr = if (controlLocal is java.net.Inet4Address &&
                !controlLocal.isAnyLocalAddress
            ) controlLocal.address else null
            val associationId = udpAssociationSequence.incrementAndGet()
            association = UdpAssociation(associationId, clientId, relaySocket, clientIp)
            udpAssociations[associationId] = association

            sendReply(output, Socks5Parser.REPLY_SUCCEEDED, relaySocket.localPort, bndAddr)
            log(
                ProxyLogLevel.DEBUG,
                "[$clientId] [compat] UDP ASSOCIATE reply port ${relaySocket.localPort}" +
                    (if (bndAddr != null)
                        ", BND.ADDR=${controlLocal?.hostAddress} (local address facing this client)"
                    else "(bind failed or not IPv4, falling back to 0.0.0.0)")
            )
            log(
                ProxyLogLevel.DEBUG,
                "[$clientId] UDP association #$associationId listening on port ${relaySocket.localPort}"
            )
            association.start()

            client.soTimeout = config.socketPollIntervalMs
            while (running.get() && association.isOpen()) {
                try {
                    if (input.read() < 0) break
                } catch (_: SocketTimeoutException) {
                    // Poll so shutdown and idle cleanup do not depend on control traffic.
                }
            }
        } catch (error: Exception) {
            if (association == null) sendReply(output, Socks5Parser.REPLY_GENERAL_FAILURE)
            if (running.get()) log(ProxyLogLevel.ERROR, "[$clientId] UDP association failed", error)
        } finally {
            if (association == null) {
                // v1.5.45: the UDP slot was reserved but the association never
                // started (socket creation threw) — hand the slot back.
                releaseUdp(clientIp)
            } else {
                association?.close()
            }
        }
    }

    private inner class TcpSession(
        private val clientId: String,
        private val client: Socket,
        private val target: Socket
    ) {
        private val closed = AtomicBoolean(false)
        // v1.4.8 (R3 timing fix): idle accounting uses the monotonic clock
        // (SystemClock.elapsedRealtime) — a wall-clock jump (NTP sync, manual
        // time change, timezone fix) used to instantly age out or freeze every
        // idle timer at once. UI-facing timestamps keep wall-clock on purpose.
        private val lastActivity = AtomicLong(elapsedRealtimeMs())

        fun run(clientInput: InputStream, clientOutput: OutputStream) {
            val reversePipe = thread(
                name = "Proxy-Pipe-${pipeSequence.incrementAndGet()}-$clientId",
                isDaemon = true
            ) {
                try {
                    pipe(target.getInputStream(), clientOutput, "target->client")
                } catch (error: Exception) {
                    if (!closed.get()) {
                        log(ProxyLogLevel.DEBUG, "[$clientId] target->client setup failed: ${error.message}")
                    }
                    close()
                } finally {
                    try {
                        client.shutdownOutput()
                    } catch (_: Exception) {
                    }
                }
            }

            try {
                pipe(clientInput, target.getOutputStream(), "client->target")
                try {
                    target.shutdownOutput()
                } catch (_: Exception) {
                }

                while (reversePipe.isAlive && !closed.get()) {
                    reversePipe.join(config.socketPollIntervalMs.toLong())
                    closeIfIdle()
                }
            } finally {
                close()
                reversePipe.join(config.socketPollIntervalMs.toLong().coerceAtMost(1_000L))
            }
        }

        private fun pipe(input: InputStream, output: OutputStream, direction: String) {
            val buffer = ByteArray(64 * 1024)
            var totalBytes = 0L
            while (!closed.get()) {
                val count = try {
                    input.read(buffer)
                } catch (_: SocketTimeoutException) {
                    closeIfIdle()
                    continue
                } catch (error: IOException) {
                    if (!closed.get()) log(ProxyLogLevel.DEBUG, "[$clientId] $direction closed: ${error.message}")
                    close()
                    return
                }

                if (count < 0) break
                if (count == 0) continue
                try {
                    output.write(buffer, 0, count)
                    output.flush()
                } catch (error: IOException) {
                    if (!closed.get()) log(ProxyLogLevel.DEBUG, "[$clientId] $direction write failed: ${error.message}")
                    close()
                    return
                }
                totalBytes += count
                lastActivity.set(elapsedRealtimeMs())
            }
            if (totalBytes > 0) log(ProxyLogLevel.DEBUG, "[$clientId] $direction transferred ${totalBytes / 1024}KB")
        }

        private fun closeIfIdle() {
            val idleMs = elapsedRealtimeMs() - lastActivity.get()
            if (idleMs >= config.tcpIdleTimeoutMs) {
                log(ProxyLogLevel.DEBUG, "[$clientId] TCP session idle for ${idleMs / 1000}s; closing")
                close()
            }
        }

        fun close() {
            if (!closed.compareAndSet(false, true)) return
            activeTcpSessions.remove(this)
            activeTargetSockets.remove(target)
            tryClose(target)
            tryClose(client)
        }
    }

    private inner class UdpAssociation(
        private val associationId: Long,
        private val clientId: String,
        private val relaySocket: DatagramSocket,
        internal val clientIp: String
    ) {
        private val open = AtomicBoolean(true)
        private val clientEndpoint = AtomicReference<InetSocketAddress?>(null)
        private val targetSockets = ConcurrentHashMap<String, DatagramSocket>()
        // beta1.0.51: ALWAYS remember the source endpoint per target session
        // (concurrency-friendly; NO compatMode gate). sing-box/Xray open
        // several source ports concurrently (connection pool rotation,
        // power-saving socket rebuilds), and even hev re-dials sockets per
        // lwIP PCB — v1.5.41's "follow the latest packet and overwrite the
        // global endpoint" made all concurrent ports except the newest never
        // receive responses, and the pre-1.0.51 default DROPPED foreign ports
        // outright ("UDP check OK but the game gets nothing"). Now each target
        // remembers its own last packet source and replies precisely;
        // clientEndpoint remains the fallback for a target's first reply.
        private val targetEndpoints = ConcurrentHashMap<String, InetSocketAddress>()
        @Volatile var lastActivity = elapsedRealtimeMs()
            private set

        init {
            relaySocket.receiveBufferSize = 128 * 1024
            relaySocket.sendBufferSize = 128 * 1024
        }

        fun isOpen(): Boolean = open.get()

        fun start() {
            thread(name = "Proxy-UDP-$associationId", isDaemon = true) {
                val buffer = ByteArray(65_535)
                try {
                    while (running.get() && open.get()) {
                        val packet = DatagramPacket(buffer, buffer.size)
                        relaySocket.receive(packet)
                        if (!acceptClientEndpoint(packet)) continue

                        try {
                            relayPacket(packet, InetSocketAddress(packet.address, packet.port))
                        } catch (error: Exception) {
                            if (open.get() && running.get()) {
                                log(ProxyLogLevel.WARN, "[$clientId] Dropped UDP packet: ${error.message}")
                            }
                        }
                    }
                } catch (error: SocketException) {
                    if (open.get() && running.get()) log(ProxyLogLevel.ERROR, "[$clientId] UDP relay socket failed", error)
                } catch (error: Exception) {
                    if (open.get() && running.get()) log(ProxyLogLevel.ERROR, "[$clientId] UDP relay failed", error)
                } finally {
                    close()
                }
            }
        }

        private fun relayPacket(packet: DatagramPacket, fromEndpoint: InetSocketAddress) {
            val target = Socks5Parser.parseUdpHeader(packet.data, packet.length)
            if (target == null || target.headerLength >= packet.length) {
                throw IOException("malformed SOCKS5 UDP header")
            }

            val payload = packet.data.copyOfRange(target.headerLength, packet.length)
            val targetAddress = InetAddress.getByName(target.targetHost)
            val key = "${targetAddress.hostAddress}:${target.targetPort}"
            // beta1.0.51: ALWAYS record the target's source endpoint (replies
            // go precisely to the initiator). Was gated on compatMode, which
            // nobody ever enabled — rotated source ports were dropped and
            // replies misrouted. Single-port clients (hev) are unaffected:
            // the same endpoint is simply re-recorded on every packet.
            targetEndpoints[key] = fromEndpoint
            sendToTarget(key, payload, targetAddress, target.targetPort)
            lastActivity = elapsedRealtimeMs()
        }

        private fun sendToTarget(key: String, payload: ByteArray, address: InetAddress, port: Int) {
            var lastError: SocketException? = null
            repeat(2) {
                val socket = targetSockets[key] ?: createTargetSocket(key)
                try {
                    socket.send(DatagramPacket(payload, payload.size, address, port))
                    return
                } catch (error: SocketException) {
                    lastError = error
                    targetSockets.remove(key, socket)
                    socket.close()
                }
            }
            throw lastError ?: SocketException("Unable to create UDP target socket")
        }

        private fun acceptClientEndpoint(packet: DatagramPacket): Boolean {
            val packetIp = packet.address.hostAddress ?: return false
            if (packetIp != clientIp) {
                log(ProxyLogLevel.WARN, "[$clientId] Dropped UDP packet from unexpected client $packetIp")
                return false
            }

            val incoming = InetSocketAddress(packet.address, packet.port)
            val known = clientEndpoint.get()
            if (known == null && clientEndpoint.compareAndSet(null, incoming)) return true
            if (clientEndpoint.get() == incoming) return true

            // beta1.0.51: other source ports of the SAME client IP are ALWAYS
            // accepted (concurrency-friendly). The pre-1.0.51 default dropped
            // them, which broke every client that rotates source ports under
            // one association. The IP check above (plus the allow-list) is the
            // security boundary; replies are routed precisely per target
            // session by targetEndpoints — never a global endpoint overwrite.
            return true

            log(ProxyLogLevel.WARN, "[$clientId] Dropped UDP packet from unexpected endpoint $incoming")
            return false
        }

        private fun createTargetSocket(key: String): DatagramSocket {
            val created = DatagramSocket().apply {
                soTimeout = config.udpTargetTimeoutMs
                receiveBufferSize = 128 * 1024
                sendBufferSize = 128 * 1024
            }
            val existing = targetSockets.putIfAbsent(key, created)
            if (existing != null) {
                created.close()
                return existing
            }
            startResponseListener(key, created)
            return created
        }

        private fun startResponseListener(key: String, socket: DatagramSocket) {
            thread(name = "Proxy-UDP-Response-$associationId-${pipeSequence.incrementAndGet()}", isDaemon = true) {
                val buffer = ByteArray(65_535)
                // v1.4.81 bugfix: a single soTimeout used to KILL this listener
                // ("catch SocketTimeoutException → fall out"). One 30s lull and
                // the response path for that target was gone forever — a late
                // DNS/game reply was silently dropped. Now a timeout just loops;
                // only CONSECUTIVE idle windows retire the socket, and any real
                // packet resets the count.
                var idleWindows = 0
                try {
                    while (running.get() && open.get()) {
                        val packet = DatagramPacket(buffer, buffer.size)
                        try {
                            socket.receive(packet)
                        } catch (_: SocketTimeoutException) {
                            idleWindows++
                            if (idleWindows >= 2) break  // ~60s idle → retire
                            continue
                        }
                        idleWindows = 0
                        // beta1.0.51: replies go to the source endpoint that last sent a packet for
                        // this target session (always-on multi-port concurrency); falls back to the
                        // association's first endpoint when this target has not been recorded yet.
                        val endpoint = targetEndpoints[key] ?: clientEndpoint.get() ?: continue
                        val header = Socks5Parser.buildUdpHeader(packet.address, packet.port)
                        val response = ByteArray(header.size + packet.length)
                        System.arraycopy(header, 0, response, 0, header.size)
                        System.arraycopy(packet.data, packet.offset, response, header.size, packet.length)
                        relaySocket.send(DatagramPacket(response, response.size, endpoint.address, endpoint.port))
                        lastActivity = elapsedRealtimeMs()
                    }
                } catch (error: Exception) {
                    if (open.get() && running.get()) {
                        log(ProxyLogLevel.DEBUG, "[$clientId] UDP target $key closed: ${error.message}")
                    }
                } finally {
                    targetSockets.remove(key, socket)
                    socket.close()
                }
            }
        }

        fun close() {
            if (!open.compareAndSet(true, false)) return
            // v1.5.45: give the UDP admission slot back exactly once, on every
            // exit path (control-connection EOF, idle reaper, stop()).
            releaseUdp(clientIp)
            udpAssociations.remove(associationId, this)
            targetSockets.values.toList().forEach { it.close() }
            targetSockets.clear()
            targetEndpoints.clear()
            relaySocket.close()
        }
    }

    private fun runUdpCleanup() {
        while (running.get()) {
            try {
                Thread.sleep(config.udpCleanupIntervalMs)
            } catch (_: InterruptedException) {
                if (!running.get()) return
            }
            val now = elapsedRealtimeMs()
            udpAssociations.values.toList().forEach { association ->
                if (now - association.lastActivity >= config.udpIdleTimeoutMs) {
                    log(ProxyLogLevel.DEBUG, "Closing idle UDP association")
                    association.close()
                }
            }
        }
    }

    private fun sendReply(
        output: OutputStream,
        replyCode: Int,
        bindPort: Int = 0,
        bindAddress: ByteArray? = null
    ) {
        try {
            output.write(Socks5Parser.buildReply(replyCode, bindPort, bindAddress))
            output.flush()
        } catch (_: Exception) {
        }
    }

    private fun logMetrics(prefix: String) {
        // v1.4.9: throttled to at most one snapshot-log per 15s. Per-connection
        // full snapshots (Map copy + executor stats) were pure hot-path overhead
        // under short-connection bursts; counters below stay lock-free.
        val now = elapsedRealtimeMs()
        val last = lastMetricsLogAt.get()
        if (last != 0L && now - last < 15_000L) return
        if (!lastMetricsLogAt.compareAndSet(last, now)) return
        val snapshot = metrics()
        log(
            ProxyLogLevel.DEBUG,
            "$prefix (active=${snapshot.activeConnections}, clients=${snapshot.activeByClient}, " +
                "tcp=${snapshot.activeTcpSessions}, udp=${snapshot.activeUdpAssociations}, " +
                "workers=${snapshot.executorActiveThreads}/${snapshot.executorPoolSize}, " +
                "rejected=${snapshot.rejectedConnections}" +
                " [global=${rejectedGlobalLimit.get()}, perClient=${rejectedPerClientLimit.get()}, " +
                "pool=${rejectedPoolSaturated.get()}])"
        )
    }

    private fun log(level: ProxyLogLevel, message: String, error: Throwable? = null) {
        logger.log(level, message, error)
    }

    private fun tryClose(socket: Socket?) {
        try {
            socket?.close()
        } catch (_: Exception) {
        }
    }

    private fun tryClose(socket: ServerSocket?) {
        try {
            socket?.close()
        } catch (_: Exception) {
        }
    }

    private fun remainingMillis(deadlineNanos: Long): Long =
        TimeUnit.NANOSECONDS.toMillis((deadlineNanos - System.nanoTime()).coerceAtLeast(0L)).coerceAtLeast(1L)
}
