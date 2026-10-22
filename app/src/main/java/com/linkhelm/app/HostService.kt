package com.linkhelm.app

import android.annotation.SuppressLint
import android.app.*
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.ServiceInfo
import android.net.Uri
import android.net.wifi.WifiManager
import android.net.wifi.p2p.WifiP2pConfig
import android.net.wifi.p2p.WifiP2pGroup
import android.net.wifi.p2p.WifiP2pManager
import android.os.*
import androidx.core.app.NotificationCompat
import android.util.Log
import android.os.SystemClock
import android.app.AlarmManager
import android.app.PendingIntent
import java.net.Inet4Address
import java.net.NetworkInterface
import java.security.SecureRandom
import java.util.concurrent.atomic.AtomicLong
import kotlin.concurrent.thread

/**
 * Minimal hardening: the Wi-Fi Direct passphrase is generated with
 * SecureRandom on first launch and persisted. Existing installations keep their
 * configured password so paired devices are not suddenly disconnected.
 */
object HostCredentials {
    private const val PREFS = "linkhelm_prefs"
    private const val KEY_PASSWORD = "wifi_password"
    private const val LETTERS = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ"
    private const val DIGITS = "23456789"
    // v1.4.5: 10 random chars (~57 bits) — enough entropy for a hotspot that is
    // only visible while sharing, and much easier to type on a TV/console.
    private const val PASSWORD_LENGTH = 10

    // v1.5.0: WPA2 passphrase bounds — the VALIDITY contract a user-supplied
    // password must meet (and the range random ones fall in).
    private const val PASSWORD_MIN_LENGTH = 8
    private const val PASSWORD_MAX_LENGTH = 63

    /** v1.5.0: composition contract shared by save-validation and the random
     *  refresh button: letters AND digits, WPA2 length bounds. */
    fun isValidPassword(password: String): Boolean =
        password.length in PASSWORD_MIN_LENGTH..PASSWORD_MAX_LENGTH &&
            password.any { it.isLetter() } &&
            password.any { it.isDigit() }

    /** v1.5.0: generate one compliant random password (does NOT persist). */
    fun generatePassword(): String {
        val random = SecureRandom()
        // Guarantee composition: half letters, half digits, then shuffle —
        // always both character classes present, order unpredictable.
        val chars = CharArray(PASSWORD_LENGTH)
        for (i in 0 until PASSWORD_LENGTH / 2) chars[i] = LETTERS[random.nextInt(LETTERS.length)]
        for (i in PASSWORD_LENGTH / 2 until PASSWORD_LENGTH) chars[i] = DIGITS[random.nextInt(DIGITS.length)]
        for (i in PASSWORD_LENGTH - 1 downTo 1) {
            val j = random.nextInt(i + 1)
            val tmp = chars[i]; chars[i] = chars[j]; chars[j] = tmp
        }
        return String(chars)
    }

    fun ensurePassword(context: Context): String {
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val existing = prefs.getString(KEY_PASSWORD, null)
        // v1.4.5: also regenerate when a pre-1.4.5 install still holds a longer
        // (16-char) password — the user explicitly asked for 10-char passwords.
        // v1.4.7: user contract is "letters AND digits, never letters-only".
        // v1.5.0 BUGFIX: user-configured passwords are now HONORED. The old
        // check demanded EXACTLY 10 generated-alphabet chars, so any password
        // the user typed in Settings (different length or ordinary letters/
        // digits) was silently replaced by a fresh random one on the next
        // settings open / share start — reported as "settings cannot change name and password".
        // Contract kept: non-blank, WPA2 bounds (8..63), letters AND digits.
        if (!existing.isNullOrBlank() && isValidPassword(existing)) return existing
        val generated = generatePassword()
        prefs.edit().putString(KEY_PASSWORD, generated).apply()
        return generated
    }
}

class HostService : Service() {

    private var proxyServer: ProxyServer? = null
    private var pendingServer: ProxyServer? = null
    // v1.5.45 (beta1.0.1): file-transfer receiver (trial) — lives and dies with
    // the sharing session, reuses the SOCKS5 credentials.
    private var fileTransferServer: FileTransferServer? = null
    // v1.4.0: independent HTTP proxy module for iPhone / no-VPN clients (port 8081).
    // Failure here is non-fatal — the SOCKS5 path stays fully independent.
    @Volatile
    private var httpProxyServer: HttpProxyServer? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null

    private lateinit var p2pManager: WifiP2pManager
    private lateinit var p2pChannel: WifiP2pManager.Channel
    private var preferredBand = 1 // 1 for 2.4GHz, 2 for 5GHz

    /** Guards against late/stale async callbacks: any callback whose epoch does not
     *  match the current one (e.g. after STOP) must be ignored. */
    private val stateEpoch = AtomicLong(0)
    private val stateHandler = Handler(Looper.getMainLooper())

    private var p2pRecoveryReceiver: BroadcastReceiver? = null

    companion object {
        var isServiceRunning = false

        // v1.5.0: live handle to the running service so the UI turbo toggle
        // can resize the RUNNING worker pool immediately ("not the next share
        // start/stop, only the manual toggle"). Null when sharing is off — then the pref
        // alone governs the next bindProxy, as before.
        @Volatile
        private var liveInstance: HostService? = null

        /** v1.5.0: apply the turbo toggle to the running pool, if sharing. */
        fun applyTurboModeLive(enabled: Boolean) {
            liveInstance?.applyTurbo(enabled)
        }

        /**
         * v1.5.45 (beta1.0.1): (re)start the file-transfer receiver with the
         * current save directory + SOCKS5 credentials, so picking a directory
         * in the UI takes effect without restarting sharing. No-op (besides
         * stopping) when no directory is set.
         */
        fun restartFileTransferLive() {
            liveInstance?.restartFileTransfer()
        }

        /**
         * v1.5.41: apply the SOCKS5 compatibility-mode toggle (Karing/Streisand) to the
         * running proxy, if sharing. Null liveInstance → the pref alone governs
         * the next bindProxy.
         */
        fun applySocksCompatLive(enabled: Boolean) {
            liveInstance?.applyCompat(enabled)
        }

        /**
         * v1.5.43: apply the SOCKS5 account/password (8-13 alphanumeric) to the running
         * proxy, if sharing — takes effect immediately. Returns false when validation failed and nothing changed.
         */
        fun applySocksAuthLive(user: String, pass: String): Boolean {
            val instance = liveInstance ?: return false
            return instance.applyAuth(user, pass)
        }

        // v1.5.46 (beta1.0.2): 15min -> 5min. START_STICKY + the 1s
        // task-removed alarm handle most kills; this alarm is the last-resort
        // resurrection when an OEM silently kills the process, and 15 minutes
        // of dead air was too long ("connects but the phone side died").
        private const val WATCHDOG_INTERVAL_MS = 5 * 60 * 1000L
        private const val WATCHDOG_REQUEST_CODE = 1001
        /**
         * v1.5.33: (re)arm the AlarmManager watchdog tick. The alarm is
         * registered with the SYSTEM, so it keeps firing even after the
         * process is killed by aggressive OEM battery savers — the receiver
         * then notices the missing service and brings it back. Safe to call
         * repeatedly; each call replaces the previous tick.
         */
        fun scheduleWatchdog(context: Context) {
            val am = context.getSystemService(Context.ALARM_SERVICE) as? AlarmManager ?: return
            val pi = PendingIntent.getBroadcast(
                context,
                WATCHDOG_REQUEST_CODE,
                Intent(context, HostWatchdogReceiver::class.java),
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
            )
            val triggerAt = SystemClock.elapsedRealtime() + WATCHDOG_INTERVAL_MS
            try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S && !am.canScheduleExactAlarms()) {
                    am.setAndAllowWhileIdle(
                        AlarmManager.ELAPSED_REALTIME_WAKEUP, triggerAt, pi
                    )
                } else {
                    am.setExactAndAllowWhileIdle(
                        AlarmManager.ELAPSED_REALTIME_WAKEUP, triggerAt, pi
                    )
                }
            } catch (_: SecurityException) {
                // Exact alarms revoked between the check and the call —
                // inexact scheduling still beats having no watchdog at all.
                try {
                    am.setAndAllowWhileIdle(
                        AlarmManager.ELAPSED_REALTIME_WAKEUP, triggerAt, pi
                    )
                } catch (_: Exception) {
                }
            }
        }

        /**
         * v1.5.34: cancel the watchdog tick on a clean stop. Without this the
         * alarm keeps firing after the user turns sharing off; the receiver
         * checks the running flag so it would no-op, but cancelling keeps the
         * device quiet and avoids pointless wake-ups.
         */
        fun cancelWatchdog(context: Context) {
            val am = context.getSystemService(Context.ALARM_SERVICE) as? AlarmManager ?: return
            val pi = PendingIntent.getBroadcast(
                context,
                WATCHDOG_REQUEST_CODE,
                Intent(context, HostWatchdogReceiver::class.java),
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
            )
            try {
                am.cancel(pi)
            } catch (_: Exception) {
            }
        }


        private const val MAX_IP_WAIT_ATTEMPTS = 10
        private const val IP_RETRY_INTERVAL_MS = 1_000L
        private val BACKOFF_INTERVALS_MS = longArrayOf(2_000, 4_000, 8_000, 16_000, 30_000, 30_000, 30_000, 30_000)
        private const val BIND_TIMEOUT_MS = 5_000L
        private const val SELF_TEST_TIMEOUT_MS = 3_000

        /** v1.5.31: Wi-Fi Direct SSIDs must keep the spec-mandated DIRECT- prefix;
         * the friendly part is now the app brand instead of the old project name. */
        const val DEFAULT_SSID = "DIRECT-LinkHelm"

        // v1.5.1: UDP self-test candidate pool — IDENTICAL order to the
        // Windows client (main.py UDP_CHECK_DNS_POOL), so both ends report
        // the same resolver on success and the "DNS mismatch between the two ends" confusion
        // disappears. 119 first (user-confirmed phone upstream).
        private val UDP_SELF_TEST_DNS_POOL =
            listOf("119.29.29.29", "223.5.5.5", "114.114.114.114")
        // v1.3.1: standard Android Wi-Fi Direct group-owner subnet. Used as the
        // last-resort display address and as the basis for the client allow-list.
        private const val CANONICAL_GO_ADDRESS = "192.168.49.1"
        private const val P2P_SUBNET_PREFIX = "192.168.49."
        // v1.4.0: independent HTTP proxy port (iPhone proxy-free / no-VPN clients).
        const val HTTP_PROXY_PORT = 8081

        const val STATUS_IDLE = "IDLE"
        const val STATUS_GROUP_CREATING = "GROUP_CREATING"
        const val STATUS_GROUP_READY = "GROUP_READY"
        const val STATUS_WAITING_FOR_IP = "WAITING_FOR_IP"
        const val STATUS_BINDING = "BINDING"
        const val STATUS_SOCKS_READY = "SOCKS_READY"

        /**
         * Immutable endpoint snapshot published atomically. Compose in MainActivity
         * observes this directly, so the UI always reflects the current proxy state.
         */
        data class EndpointState(
            val status: String = STATUS_IDLE,
            val ip: String? = null,
            val port: Int = -1,
            val iface: String? = null,
            val reason: String? = null,
            // v1.4.0: port of the HTTP proxy module (iPhone proxy-free / no-VPN clients).
            // -1 while not started; non-fatal module, SOCKS port remains authoritative.
            val httpPort: Int = -1,
            // v1.4.3: UDP connectivity self-check result (non-fatal diagnostic info).
            // "..." (check mark) means the full UDP ASSOCIATE path works; "..." (cross) carries the reason; null means not yet probed.
            val udpStatus: String? = null,
            // v1.4.4: downstream device roster (refreshed every 3s by the Client-Monitor thread).
            val clients: List<ClientSnapshot> = emptyList()
        )

        val observableEndpoint = androidx.compose.runtime.mutableStateOf(EndpointState())

        val endpointState: EndpointState get() = observableEndpoint.value

        internal fun publishEndpoint(
            status: String,
            ip: String? = null,
            port: Int = -1,
            iface: String? = null,
            reason: String? = null,
            httpPort: Int = -1
        ) {
            observableEndpoint.value = EndpointState(
                status = status,
                ip = ip,
                port = port,
                iface = iface,
                reason = reason,
                httpPort = httpPort
            )
        }

        internal fun resetEndpoint() {
            observableEndpoint.value = EndpointState()
        }

        /** v1.4.3: publish the UDP self-check result (copy current state, change udpStatus only). */
        internal fun publishUdpStatus(value: String?) {
            observableEndpoint.value = observableEndpoint.value.copy(udpStatus = value)
        }

        /** v1.4.4: publish the downstream device roster (copy current state, change clients only). */
        internal fun publishClients(list: List<ClientSnapshot>) {
            observableEndpoint.value = observableEndpoint.value.copy(clients = list)
        }
    }

    override fun onCreate() {
        super.onCreate()
        FileTransferApp.context = applicationContext
        p2pManager = getSystemService(Context.WIFI_P2P_SERVICE) as WifiP2pManager
        p2pChannel = p2pManager.initialize(applicationContext, mainLooper, null)
        registerP2pRecoveryReceiver()
        // v1.5.0: register as the live instance for the UI turbo toggle.
        liveInstance = this
    }

    /** v1.5.0: live turbo resize on the running proxy (no share restart). */
    private fun applyTurbo(enabled: Boolean) {
        proxyServer?.applyTurboMode(enabled)
    }

    /** v1.5.45 (beta1.0.1): rebuild the file-transfer receiver from current prefs. */
    private fun restartFileTransfer() {
        val prefs = getSharedPreferences("linkhelm_prefs", MODE_PRIVATE)
        val dir = prefs.getString(FileTransferProtocol.PREFS_KEY_DIR, null)
        fileTransferServer?.stop()
        fileTransferServer = null
        if (dir == null) return
        val user = prefs.getString("socks_user", ProxyServer.DEFAULT_SOCKS_USER) ?: ProxyServer.DEFAULT_SOCKS_USER
        val pass = prefs.getString("socks_pass", ProxyServer.DEFAULT_SOCKS_PASS) ?: ProxyServer.DEFAULT_SOCKS_PASS
        fileTransferServer = FileTransferServer(user, pass, contentResolver, Uri.parse(dir)).also { it.start() }
        Log.i("HostService", "File-transfer receiver (re)started on :${FileTransferProtocol.PORT}")
    }

    /** v1.5.41: live SOCKS5 compatibility-mode toggle on the running proxy (no share restart). */
    private fun applyCompat(enabled: Boolean) {
        proxyServer?.applyCompatMode(enabled)
    }

    /** v1.5.43: live SOCKS5 credential update on the running proxy (no share restart). */
    private fun applyAuth(user: String, pass: String): Boolean {
        val server = proxyServer ?: return false
        return server.applySocksCredentials(user, pass)
    }

    /**
     * Event-driven P2P recovery. When a CONNECTION_CHANGED broadcast arrives and the
     * endpoint is not SOCKS_READY yet, re-evaluate the group instead of waiting for the
     * next backoff tick. Epoch checks discard stale callbacks after STOP.
     */
    @SuppressLint("MissingPermission")
    private fun registerP2pRecoveryReceiver() {
        if (p2pRecoveryReceiver != null) return
        val filter = IntentFilter(WifiP2pManager.WIFI_P2P_CONNECTION_CHANGED_ACTION)
        val receiver = object : BroadcastReceiver() {
            override fun onReceive(context: Context?, intent: Intent?) {
                if (!isServiceRunning) return
                val status = endpointState.status
                if (status == STATUS_SOCKS_READY || status == STATUS_BINDING || status == STATUS_GROUP_CREATING) return
                Log.i("HostService", "P2P connection changed while status=$status — re-evaluating")
                stateHandler.removeCallbacksAndMessages(null)
                stateEpoch.incrementAndGet()
                startProxyForGroup()
            }
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            registerReceiver(receiver, filter, RECEIVER_NOT_EXPORTED)
        } else {
            @Suppress("UnspecifiedRegisterReceiverFlag")
            registerReceiver(receiver, filter)
        }
        p2pRecoveryReceiver = receiver
    }

    private fun unregisterP2pRecoveryReceiver() {
        p2pRecoveryReceiver?.let { runCatching { unregisterReceiver(it) } }
        p2pRecoveryReceiver = null
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val prefs = getSharedPreferences("linkhelm_prefs", Context.MODE_PRIVATE)

        if (intent?.action == "STOP") {
            stateEpoch.incrementAndGet()
            isServiceRunning = false
            prefs.edit().putBoolean("host_is_running_flag", false).apply()
            stopGroupAndService()
            return START_NOT_STICKY
        }

        if (intent?.action == "REDETECT") {
            if (isServiceRunning && endpointState.status != STATUS_SOCKS_READY &&
                endpointState.status != STATUS_BINDING
            ) {
                Log.i("HostService", "REDETECT requested; re-evaluating P2P endpoint")
                startProxyForGroup()
            }
            return START_STICKY
        }

        // v1.4.4: host UI kicked a downstream device — close its sockets and
        // block reconnects until the next stop/start cycle.
        if (intent?.action == "KICK_CLIENT") {
            val ip = intent.getStringExtra("ip")
            if (ip != null && isServiceRunning) {
                val closed = proxyServer?.kickClient(ip) ?: 0
                Log.i("HostService", "KICK_CLIENT $ip → closed $closed live sockets, blocked")
            }
            return START_STICKY
        }

        val notification = createNotification("Host is active")

        if (isServiceRunning) {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
                startForeground(2, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
            } else {
                startForeground(2, notification)
            }
            acquireLocks()
            return START_STICKY
        }

        isServiceRunning = true
        prefs.edit().putBoolean("host_is_running_flag", true).apply()

        if (intent != null) {
            preferredBand = intent.getIntExtra("WIFI_BAND", 1)
            prefs.edit().putInt("last_wifi_band", preferredBand).apply()
        } else {
            preferredBand = prefs.getInt("last_wifi_band", 1)
        }

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            startForeground(2, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(2, notification)
        }
        acquireLocks()

        // v1.5.33: arm the anti-battery-saver watchdog for as long as the
        // sharing flag is set (see HostWatchdogReceiver).
        scheduleWatchdog(this)

        publishEndpoint(STATUS_GROUP_CREATING)
        setupWifiDirectGroup()

        return START_STICKY
    }

    @SuppressLint("MissingPermission")
    private fun setupWifiDirectGroup() {
        // v1.3 fix: do not unconditionally tear down an existing group. If we are
        // already the group owner and the group looks usable, reuse it. This avoids
        // disconnecting an in-progress Windows client and the "Busy" errors that come
        // from rapid remove/create cycles.
        // v1.5.31 fix: "reuse" is only valid when the live group's credentials still
        // match the saved settings. stopGroupAndService() tears the group down
        // asynchronously, so a quick stop→restart (settings save / random refresh)
        // used to find the OLD group still alive and silently reuse it — new
        // SSID/password never took effect. Now a credential mismatch forces a
        // remove + recreate with the fresh values.
        p2pManager.requestGroupInfo(p2pChannel) { group ->
            if (group != null && group.isGroupOwner) {
                val prefs = getSharedPreferences("linkhelm_prefs", MODE_PRIVATE)
                val wantSsid = prefs.getString("wifi_ssid", DEFAULT_SSID) ?: DEFAULT_SSID
                val wantPass = prefs.getString("wifi_password", null)
                val sameSsid = group.networkName == wantSsid
                // getPassphrase() needs API 28; the custom-config group path itself
                // is Q+ only, so on older systems SSID equality is the whole check.
                val samePass = wantPass == null ||
                    Build.VERSION.SDK_INT < Build.VERSION_CODES.P ||
                    group.passphrase == wantPass
                if (sameSsid && samePass) {
                    Log.i("HostService", "Reusing existing P2P group ${group.networkName}")
                    startProxyForGroup()
                } else {
                    Log.i("HostService", "Saved settings differ from live group (${group.networkName}); recreating group")
                    recreateGroup()
                }
                return@requestGroupInfo
            }
            recreateGroup()
        }
    }

    private fun recreateGroup() {
        p2pManager.removeGroup(p2pChannel, object : WifiP2pManager.ActionListener {
            override fun onSuccess() {
                createGroupWithPassword()
            }
            override fun onFailure(reason: Int) {
                // removeGroup can fail while a previous teardown is still settling
                // (Busy) — retry the create after a short delay instead of stacking
                // a create on a half-live group.
                Log.w("HostService", "removeGroup failed ($reason); retrying create in 1.5s")
                stateHandler.postDelayed({ createGroupWithPassword() }, 1500)
            }
        })
    }

    @SuppressLint("MissingPermission")
    private fun createGroupWithPassword() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            try {
                val prefs = getSharedPreferences("linkhelm_prefs", MODE_PRIVATE)
                val ssid = prefs.getString("wifi_ssid", DEFAULT_SSID) ?: DEFAULT_SSID
                val password = HostCredentials.ensurePassword(this)

                val frequency = if (preferredBand == 1) 2437 else 5180

                val config = WifiP2pConfig.Builder()
                    .setNetworkName(ssid)
                    .setPassphrase(password)
                    .setGroupOperatingFrequency(frequency)
                    .build()

                p2pManager.createGroup(p2pChannel, config, object : WifiP2pManager.ActionListener {
                    override fun onSuccess() {
                        // createGroup onSuccess only means the request was accepted.
                        // The real READY state comes from the proxy bind + self-test below.
                        startProxyForGroup()
                    }
                    override fun onFailure(reason: Int) {
                        Log.e("HostService", "Config Group failed ($reason), trying legacy...")
                        createLegacyGroup()
                    }
                })
                return
            } catch (e: Exception) {
                Log.e("HostService", "Builder error: ${e.message}")
            }
        }
        createLegacyGroup()
    }

    @SuppressLint("MissingPermission")
    private fun createLegacyGroup() {
        p2pManager.createGroup(p2pChannel, object : WifiP2pManager.ActionListener {
            override fun onSuccess() {
                startProxyForGroup()
            }
            override fun onFailure(reason: Int) {
                publishEndpoint("FAILED:GROUP_CREATE_FAILED")
                updateNotification("✗ GROUP_CREATE_FAILED ($reason)")
            }
        })
    }

    /**
     * Start the proxy only when the Wi-Fi Direct group is formed AND the P2P
     * interface has a real IPv4. The address is read from the interface reported by
     * WifiP2pGroup.getInterface(), not from peer APIs that may be null.
     */
    private fun startProxyForGroup() {
        val epoch = stateEpoch.incrementAndGet()
        publishEndpoint(STATUS_GROUP_READY)
        waitForP2pAddressAndBind(epoch, attempt = 0)
    }

    private fun waitForP2pAddressAndBind(epoch: Long, attempt: Int) {
        if (epoch != stateEpoch.get() || !isServiceRunning) return
        p2pManager.requestGroupInfo(p2pChannel) { group ->
            if (epoch != stateEpoch.get() || !isServiceRunning) return@requestGroupInfo
            if (group == null || !group.isGroupOwner) {
                if (attempt < MAX_IP_WAIT_ATTEMPTS) {
                    stateHandler.postDelayed({ waitForP2pAddressAndBind(epoch, attempt + 1) }, IP_RETRY_INTERVAL_MS)
                } else if (attempt < MAX_IP_WAIT_ATTEMPTS + BACKOFF_INTERVALS_MS.size) {
                    val backoffMs = BACKOFF_INTERVALS_MS[attempt - MAX_IP_WAIT_ATTEMPTS]
                    publishEndpoint(STATUS_WAITING_FOR_IP, reason = "GROUP_NOT_READY_YET")
                    stateHandler.postDelayed({ waitForP2pAddressAndBind(epoch, attempt + 1) }, backoffMs)
                } else {
                    failProxy(epoch, "GROUP_NOT_READY", "group=${group != null}")
                }
                return@requestGroupInfo
            }
            val ifaceName: String? = runCatching { group.`interface` }.getOrNull()
            // v1.5.43 performance: the P2P callbacks run on the MAIN thread and
            // NetworkInterface enumeration (findBoundP2pIpv4) is a potentially
            // slow system call — resolving it there caused the reported
            // "the stutter when starting sharing". The whole address resolution now runs on a
            // worker; bindProxy itself is thread-safe (it only publishes state).
            thread(name = "HostService-AddrResolve", isDaemon = true) {
                val localIp = findBoundP2pIpv4(ifaceName)
                if (epoch != stateEpoch.get() || !isServiceRunning) return@thread
                if (localIp == null) {
                    if (attempt < MAX_IP_WAIT_ATTEMPTS) {
                        Log.i("HostService", "P2P interface $ifaceName has no IPv4 yet (attempt ${attempt + 1}/$MAX_IP_WAIT_ATTEMPTS)")
                        publishEndpoint(STATUS_WAITING_FOR_IP, iface = ifaceName)
                        stateHandler.postDelayed({ waitForP2pAddressAndBind(epoch, attempt + 1) }, IP_RETRY_INTERVAL_MS)
                    } else if (attempt < MAX_IP_WAIT_ATTEMPTS + BACKOFF_INTERVALS_MS.size) {
                        val backoffMs = BACKOFF_INTERVALS_MS[attempt - MAX_IP_WAIT_ATTEMPTS]
                        publishEndpoint(STATUS_WAITING_FOR_IP, iface = ifaceName, reason = "P2P_IP_PENDING")
                        stateHandler.postDelayed({ waitForP2pAddressAndBind(epoch, attempt + 1) }, backoffMs)
                    } else {
                        // v1.3.1: the group IS formed (we are group owner) — never fail
                        // just because the interface address could not be resolved.
                        // Bind the wildcard address now; the display IP falls back to
                        // the any-interface scan, then the canonical GO address.
                        Log.w("HostService", "P2P IPv4 unresolved on '$ifaceName' after retries; binding wildcard anyway")
                        bindProxy(epoch, ifaceName, null)
                    }
                    return@thread
                }
                bindProxy(epoch, ifaceName, localIp)
            }
        }
    }

    /**
     * v1.3.1: bind to 0.0.0.0 (all interfaces) instead of a single P2P address.
     *
     * Field reports ("PingSucceeded=True / TcpTestSucceeded=False on 192.168.49.1:8080")
     * showed the proxy port was unreachable on the GO address when interface-name
     * resolution returned a different/secondary address. Binding to the wildcard
     * address guarantees the port is reachable on 192.168.49.1 regardless of which
     * address the interface enumeration reports. Security is preserved by
     * allowedClientRanges: only the P2P /24 is accepted.
     *
     * The displayed IP is resolved independently (interface reported by the group,
     * then any-interface scan for the standard 192.168.49.x GO subnet, then the
     * canonical GO address fallback).
     */
    private fun bindProxy(epoch: Long, ifaceName: String?, ip: String?) {
        if (epoch != stateEpoch.get() || !isServiceRunning) return
        val displayIp = ip ?: findP2pIpv4OnAnyInterface() ?: CANONICAL_GO_ADDRESS
        publishEndpoint(STATUS_BINDING, ip = displayIp, iface = ifaceName)
        // v1.4.8: Turbo mode (user toggle, persisted) — raises the SOCKS5 worker
        // pool ceiling for many-device hosts. Off by default; the UI suggests
        // enabling it once more than 3 real downstream devices show up. Takes
        // effect on the next share session (pool is built here).
        // v1.4.82: normal 500 -> 480, turbo 1000 -> 800 (plan "normal 480, turbo 800");
        // per-IP dynamic quotas live in ProxyServer itself.
        val turboMode = getSharedPreferences("linkhelm_prefs", MODE_PRIVATE)
            .getBoolean("turbo_mode", false)
        // v1.5.43: SOCKS5 credentials (RFC 1929) — built-in fixed values by default, editable in settings
        // (8-13 alphanumeric, persisted in prefs; no random/auto-refresh path at all).
        val authPrefs = getSharedPreferences("linkhelm_prefs", MODE_PRIVATE)
        val socksUser = authPrefs.getString("socks_user", ProxyServer.DEFAULT_SOCKS_USER)
            ?: ProxyServer.DEFAULT_SOCKS_USER
        val socksPass = authPrefs.getString("socks_pass", ProxyServer.DEFAULT_SOCKS_PASS)
            ?: ProxyServer.DEFAULT_SOCKS_PASS
        val config: ProxyServer.Config = ProxyServer.Config(
            bindAddress = "0.0.0.0",
            // v1.5.45 (beta1.0.1): the thread ceiling is no longer set here.
            // Resource profiles (threads + session caps + per-device quotas +
            // UDP idle) are applied via applyTurboMode/applyCompatMode below;
            // the Config defaults are the STANDARD profile.
            // v1.3.2: loopback MUST be allowed — the bind-time SOCKS self-test
            // connects from 127.0.0.1 and was rejected by the P2P-only allow-list
            // (field symptom: SOCKS_HANDSHAKE_FAILED + "pending assignment"). Loopback is only
            // reachable from the phone itself, so this widens nothing externally;
            // remote clients are still restricted to the P2P /24.
            allowedClientRanges = listOf("127.0.0.0/8", cidrFor(displayIp)),
            // v1.5.34: standby lane B on 8082 stays — the Windows side no
            // longer runs the lane router, but it DOES fail over to :8082
            // when :8080 is unreachable (probe-driven tunnel rebuild). If
            // 8082 cannot bind (rare), ProxyServer degrades to single-port
            // mode by itself and everything keeps working.
            secondaryPort = 8082,
            // v1.5.32: briefly wait for a quota slot on burst instead of
            // refusing — refusals used to count as lane failures on the
            // Windows router and short phone-side stalls used to degrade
            // BOTH lanes (the "both lanes unavailable" blackout).
            quotaWaitMs = 1_500L,
            socksUser = socksUser,
            socksPass = socksPass
        )
        val server: ProxyServer = ProxyServer(config, AndroidProxyLogger)
        pendingServer = server
        // v1.5.45 (beta1.0.1): restore the persisted Turbo toggle as a full
        // resource profile (bounded session caps), not just a thread number.
        if (turboMode) server.applyTurboMode(true)
        server.start()
        Log.i("HostService", "Proxy binding 0.0.0.0:${config.port}, display=$displayIp on iface ${ifaceName ?: "?"} (epoch=$epoch)")
        thread(name = "HostService-ProxyWatch", isDaemon = true) {
            val bound = server.awaitStarted(BIND_TIMEOUT_MS)
            if (epoch != stateEpoch.get() || !isServiceRunning) {
                server.stop()
                return@thread
            }
            if (!bound) {
                server.stop()
                pendingServer = null
                failProxy(epoch, "PROXY_BIND_FAILED", "addr=0.0.0.0:${config.port} iface=$ifaceName")
                return@thread
            }
            // Self-test against loopback: with a wildcard bind the loopback is always
            // routed to the same listening socket, so this validates the listener and
            // the SOCKS5 handshake independently of interface address resolution.
            // v1.3.2: one retry — a freshly bound accept queue can transiently
            // refuse on some devices; only fail after the retry also errors.
            var handshakeError = socksSelfTest("127.0.0.1", config.port, config.socksUser, config.socksPass)
            if (handshakeError != null && epoch == stateEpoch.get() && isServiceRunning) {
                Thread.sleep(200)
                handshakeError = socksSelfTest("127.0.0.1", config.port, config.socksUser, config.socksPass)
            }
            if (epoch != stateEpoch.get() || !isServiceRunning) {
                server.stop()
                return@thread
            }
            if (handshakeError != null) {
                server.stop()
                pendingServer = null
                failProxy(epoch, "SOCKS_HANDSHAKE_FAILED", handshakeError)
                return@thread
            }
            pendingServer = null
            proxyServer = server
            // v1.5.41: restore the SOCKS5 compatibility mode (Karing/Streisand) persisted
            // toggle on every fresh share session.
            val socksCompat = getSharedPreferences("linkhelm_prefs", MODE_PRIVATE)
                .getBoolean("socks_compat_mode", false)
            if (socksCompat) server.applyCompatMode(true)
            // v1.5.45 (beta1.0.1): file-transfer receiver (trial). Only starts
            // when the user has picked a save directory; port 8765, same
            // credentials as SOCKS5, Wi-Fi Direct sources only.
            fileTransferServer?.stop()
            val ftDir = getSharedPreferences("linkhelm_prefs", MODE_PRIVATE)
                .getString(FileTransferProtocol.PREFS_KEY_DIR, null)
            fileTransferServer = if (ftDir != null) {
                FileTransferServer(socksUser, socksPass, contentResolver, Uri.parse(ftDir))
                    .also { it.start() }
            } else {
                null
            }
            // v1.4.0: independent HTTP proxy module (iPhone proxy-free / no-VPN clients).
            // Non-fatal: its failure never downgrades the SOCKS5 session.
            val httpPort = startHttpProxyLocked(epoch, displayIp, server)
            publishEndpoint(STATUS_SOCKS_READY, ip = displayIp, port = config.port, iface = ifaceName, httpPort = httpPort)
            Log.i("HostService", "SOCKS_READY on 0.0.0.0:${config.port} (display=$displayIp, iface=${ifaceName ?: "?"}, httpProxy=$httpPort)")
            updateNotification("✓ Sharing Active ($displayIp:${config.port})")

            // v1.4.4: downstream-device roster — poll the SOCKS server's roster
            // and publish to the UI so the host can see (and kick) connected
            // clients. v1.4.7: the UI roster hides the phone's own traffic
            // (bind-time SOCKS self-test from 127.x, and connections the phone
            // itself makes via its 192.168.49.1 GO address) — they are not
            // downstream devices. The data-plane snapshot stays complete;
            // filtering is display-only (control plane only, data plane untouched).
            thread(name = "Client-Monitor", isDaemon = true) {
                while (epoch == stateEpoch.get() && isServiceRunning && proxyServer === server) {
                    val roster = server.clientSnapshot().filter { snap ->
                        !snap.ip.startsWith("127.") && snap.ip != CANONICAL_GO_ADDRESS
                    }
                    publishClients(roster)
                    try {
                        Thread.sleep(3_000)
                    } catch (_: InterruptedException) {
                        break
                    }
                }
            }

            // v1.4.3: UDP connectivity self-check (non-fatal, background). Sends one real query to a
            // public DNS via the local SOCKS5 UDP ASSOCIATE: success = local UDP relay + egress UDP path all OK.
            // v1.4.4: respect the user toggle (can be turned off on the share page); when off, state it explicitly instead of probing silently.
            // v1.4.81 bugfix: a single DNS attempt with a 5s timeout was too brittle — one packet
            // dropped by cellular UDP QoS showed a red cross. Now it retries with the next candidate; only all-fail counts as failure.
            // v1.5.1: the candidate pool now matches the Windows side exactly (119 -> 223 -> 114, same order),
            // and the actually successful DNS is shown truthfully — previously the phone always preferred 223.5.5.5
            // while the desktop preferred 119.29.29.29, so both ends reported "success" with different DNS
            // ("UDP DNS mismatch yet matched"), contradicting each other during troubleshooting.
            thread(name = "UDP-SelfTest", isDaemon = true) {
                val autoProbe = getSharedPreferences("linkhelm_prefs", MODE_PRIVATE)
                    .getBoolean("udp_auto_probe", true)
                if (epoch != stateEpoch.get() || !isServiceRunning) return@thread
                if (!autoProbe) {
                    publishUdpStatus(L10n.tr("Auto probe disabled", "自动检测已关闭"))
                    return@thread
                }
                publishUdpStatus(L10n.tr("Probing... (DNS queries sent via the UDP relay in candidate-pool order)", "检测中…（经 UDP 中继按候选池顺序发送 DNS 查询）"))
                val attempts = mutableListOf<String>()
                var success: String? = null
                for (dns in UDP_SELF_TEST_DNS_POOL) {
                    if (epoch != stateEpoch.get() || !isServiceRunning) return@thread
                    val error = UdpProbe.socks5UdpDnsProbe(
                        "127.0.0.1", config.port, dnsHost = dns,
                        socksUser = config.socksUser, socksPass = config.socksPass
                    )
                    if (error == null) {
                        success = dns
                        break
                    }
                    attempts.add("$dns: $error")
                    Log.i("HostService", "UDP self-test try failed ($dns): $error; trying next candidate")
                }
                if (epoch != stateEpoch.get() || !isServiceRunning) return@thread
                val status = if (success != null) {
                    L10n.tr("OK (via UDP relay to $success:53, connectivity diagnostics only)", "✓ 正常（经 UDP 中继到 $success:53，仅用于连通性诊断）")
                } else {
                    L10n.tr("Failed: ", "✗ 失败：") + attempts.joinToString("; ")
                }
                publishUdpStatus(status)
                Log.i("HostService", "UDP self-test: $status")
            }
        }
    }

    /**
     * Starts the independent HTTP proxy on 8081 for clients that use their native
     * Wi-Fi manual-proxy setting (iOS has no SOCKS option). Returns the listening
     * port, or -1 if the module could not start (SOCKS session is unaffected).
     */
    private fun startHttpProxyLocked(epoch: Long, displayIp: String, socksServer: ProxyServer): Int {
        if (epoch != stateEpoch.get() || !isServiceRunning) return -1
        return try {
            val httpProxy = HttpProxyServer(
                port = HTTP_PROXY_PORT,
                bindAddress = "0.0.0.0",
                allowedClientRanges = listOf("127.0.0.0/8", cidrFor(displayIp)),
                // v1.4.4: kicked devices are denied here too. The set instance is
                // shared, so kicks issued after this call are also enforced.
                blockedClients = socksServer.blockedClientsView()
            )
            httpProxy.start()
            if (!httpProxy.awaitStarted(2_000L) || httpProxy.listeningPort <= 0) {
                Log.w("HostService", "HTTP proxy module failed to start (non-fatal); SOCKS path unaffected")
                httpProxy.stop()
                return -1
            }
            httpProxyServer = httpProxy
            Log.i("HostService", "HTTP proxy ready on 0.0.0.0:${httpProxy.listeningPort} (iPhone proxy-free)")
            httpProxy.listeningPort
        } catch (error: Throwable) {
            Log.w("HostService", "HTTP proxy module error (non-fatal): ${error.message}")
            -1
        }
    }

    private fun failProxy(epoch: Long, code: String, detail: String) {
        if (epoch != stateEpoch.get()) return
        Log.e("HostService", "Proxy start failed: $code ($detail)")
        publishEndpoint("FAILED:$code")
        proxyServer?.stop()
        pendingServer?.stop()
        httpProxyServer?.stop()
        httpProxyServer = null
        proxyServer = null
        pendingServer = null
        updateNotification("✗ $code")
    }

    private fun stopProxyLocked() {
        proxyServer?.stop()
        pendingServer?.stop()
        httpProxyServer?.stop()
        httpProxyServer = null
        proxyServer = null
        pendingServer = null
    }

    private fun cidrFor(ip: String): String {
        val parts = ip.split(".")
        return if (parts.size == 4) "${parts[0]}.${parts[1]}.${parts[2]}.0/24" else "192.168.49.0/24"
    }

    private fun findBoundP2pIpv4(interfaceName: String?): String? {
        if (interfaceName.isNullOrBlank()) return null
        return runCatching {
            val networkIf = NetworkInterface.getByName(interfaceName) ?: return@runCatching null
            val addresses = networkIf.inetAddresses
            while (addresses.hasMoreElements()) {
                val a = addresses.nextElement()
                if (a is Inet4Address && !a.isAnyLocalAddress &&
                    !a.isLoopbackAddress && !a.isLinkLocalAddress
                ) {
                    return@runCatching a.hostAddress
                }
            }
            null
        }.getOrNull()
    }

    /**
     * v1.3.1 fallback: scan every network interface for an IPv4 in the standard
     * Wi-Fi Direct GO subnet (192.168.49.0/24). Used for display when the group
     * interface name cannot be resolved via NetworkInterface.getByName.
     */
    private fun findP2pIpv4OnAnyInterface(): String? {
        return runCatching {
            val interfaces = NetworkInterface.getNetworkInterfaces()
            while (interfaces.hasMoreElements()) {
                val addresses = interfaces.nextElement().inetAddresses
                while (addresses.hasMoreElements()) {
                    val a = addresses.nextElement()
                    if (a is Inet4Address && !a.isLoopbackAddress && !a.isLinkLocalAddress &&
                        a.hostAddress.startsWith(P2P_SUBNET_PREFIX)
                    ) {
                        return@runCatching a.hostAddress
                    }
                }
            }
            null
        }.getOrNull()
    }

    private fun socksSelfTest(host: String, port: Int, socksUser: String, socksPass: String): String? {
        return try {
            java.net.Socket().use { socket ->
                socket.connect(java.net.InetSocketAddress(host, port), SELF_TEST_TIMEOUT_MS)
                socket.soTimeout = SELF_TEST_TIMEOUT_MS
                // v1.5.43: host now requires RFC 1929 — offer no-auth AND user/pass,
                // then answer the subnegotiation when the server selects 0x02.
                socket.getOutputStream().write(byteArrayOf(0x05, 0x02, 0x00, 0x02))
                socket.getOutputStream().flush()
                val reply = ByteArray(2)
                var read = 0
                val input = socket.getInputStream()
                while (read < 2) {
                    val n = input.read(reply, read, 2 - read)
                    if (n < 0) break
                    read += n
                }
                if (read < 2) return "short reply ($read bytes)"
                if (reply[0] != 0x05.toByte()) return "bad version 0x%02x".format(reply[0])
                when (reply[1].toInt() and 0xFF) {
                    0x02 -> {
                        val auth = Socks5Parser.buildUserPassRequest(socksUser, socksPass)
                        socket.getOutputStream().write(auth)
                        socket.getOutputStream().flush()
                        val authReply = ByteArray(2)
                        var got = 0
                        while (got < 2) {
                            val n = input.read(authReply, got, 2 - got)
                            if (n < 0) break
                            got += n
                        }
                        if (got < 2) return "short auth reply ($got bytes)"
                        if (authReply[1] != 0x00.toByte()) return "auth failed (bad credentials?)"
                    }
                    0x00 -> return "server accepted no-auth (auth not enforced?)"
                    else -> return "method refused 0x%02x".format(reply[1])
                }
                null
            }
        } catch (e: Exception) {
            "${e.javaClass.simpleName}: ${e.message}"
        }
    }

    override fun onTaskRemoved(rootIntent: Intent?) {
        Log.d("HostService", "App swiped — scheduling restart...")
        val prefs = getSharedPreferences("linkhelm_prefs", Context.MODE_PRIVATE)
        val savedBand = prefs.getInt("last_wifi_band", preferredBand)

        val restartIntent = Intent(applicationContext, HostService::class.java).apply {
            putExtra("WIFI_BAND", savedBand)
        }

        val pendingIntent = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            PendingIntent.getForegroundService(
                applicationContext, 1, restartIntent,
                PendingIntent.FLAG_ONE_SHOT or PendingIntent.FLAG_IMMUTABLE
            )
        } else {
            PendingIntent.getService(
                applicationContext, 1, restartIntent,
                PendingIntent.FLAG_ONE_SHOT or PendingIntent.FLAG_IMMUTABLE
            )
        }

        val alarmManager = getSystemService(Context.ALARM_SERVICE) as AlarmManager
        val triggerTime = SystemClock.elapsedRealtime() + 1000L

        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                if (alarmManager.canScheduleExactAlarms()) {
                    alarmManager.setExactAndAllowWhileIdle(
                        AlarmManager.ELAPSED_REALTIME_WAKEUP,
                        triggerTime,
                        pendingIntent
                    )
                } else {
                    alarmManager.setAndAllowWhileIdle(
                        AlarmManager.ELAPSED_REALTIME_WAKEUP,
                        triggerTime,
                        pendingIntent
                    )
                }
            } else if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                alarmManager.setExactAndAllowWhileIdle(
                    AlarmManager.ELAPSED_REALTIME_WAKEUP,
                    triggerTime,
                    pendingIntent
                )
            } else {
                alarmManager.set(
                    AlarmManager.ELAPSED_REALTIME_WAKEUP,
                    triggerTime,
                    pendingIntent
                )
            }
        } catch (e: SecurityException) {
            Log.e("HostService", "Exact alarm permission missing, using inexact fallback", e)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                alarmManager.setAndAllowWhileIdle(
                    AlarmManager.ELAPSED_REALTIME_WAKEUP,
                    triggerTime,
                    pendingIntent
                )
            } else {
                alarmManager.set(
                    AlarmManager.ELAPSED_REALTIME_WAKEUP,
                    triggerTime,
                    pendingIntent
                )
            }
        }

        super.onTaskRemoved(rootIntent)
    }

    private fun createNotification(content: String): Notification {
        val channelId = "host_service_channel"
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(channelId, L10n.tr("LinkHelm Sharing Service", "LinkHelm 分享服务"), NotificationManager.IMPORTANCE_LOW)
            getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
        }

        val stopIntent = Intent(this, HostService::class.java).apply { action = "STOP" }
        val stopPendingIntent = PendingIntent.getService(this, 0, stopIntent, PendingIntent.FLAG_IMMUTABLE)

        return NotificationCompat.Builder(this, channelId)
            .setContentTitle(L10n.tr("LinkHelm Host", "LinkHelm 分享服务"))
            .setContentText(content)
            .setSmallIcon(android.R.drawable.ic_menu_share)
            .setOngoing(true)
            .addAction(android.R.drawable.ic_menu_close_clear_cancel, "Stop Sharing", stopPendingIntent)
            .build()
    }

    private fun updateNotification(content: String) {
        val notification = createNotification(content)
        getSystemService(NotificationManager::class.java).notify(2, notification)
    }

    private fun acquireLocks() {
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "LinkHelm::HostWakeLock")
        wakeLock?.acquire()

        val wm = applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
        // v1.4.2: LOW_LATENCY mode (API 29+) keeps the Wi-Fi chip out of power-save
        // between packets — the main source of the "i/o timeout" blips clients saw.
        // FULL_HIGH_PERF stays as the pre-Q fallback.
        val mode = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q)
            WifiManager.WIFI_MODE_FULL_LOW_LATENCY
        else
            WifiManager.WIFI_MODE_FULL_HIGH_PERF
        wifiLock = wm.createWifiLock(mode, "LinkHelm::WifiLock")
        wifiLock?.acquire()
    }

    private fun stopGroupAndService() {
        stateEpoch.incrementAndGet()
        stateHandler.removeCallbacksAndMessages(null)
        stopProxyLocked()
        resetEndpoint()
        p2pManager.removeGroup(p2pChannel, null)
        stopForeground(true)
        unregisterP2pRecoveryReceiver()
        stopSelf()
    }

    override fun onDestroy() {
        val prefs = getSharedPreferences("linkhelm_prefs", Context.MODE_PRIVATE)
        prefs.edit().putBoolean("host_is_running_flag", false).apply()
        stateEpoch.incrementAndGet()
        stateHandler.removeCallbacksAndMessages(null)
        stopProxyLocked()
        // v1.5.45 (beta1.0.1): the file-transfer receiver dies with sharing.
        fileTransferServer?.stop()
        fileTransferServer = null
        resetEndpoint()
        unregisterP2pRecoveryReceiver()
        // v1.5.34: stop the watchdog alarm on a clean shutdown.
        cancelWatchdog(this)
        wakeLock?.let { if (it.isHeld) it.release() }
        wifiLock?.let { if (it.isHeld) it.release() }
        // v1.5.0: drop the live handle — the toggle then only writes the pref,
        // which governs the next bindProxy as before.
        liveInstance = null
        isServiceRunning = false
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null
}
