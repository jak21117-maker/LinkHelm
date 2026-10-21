package com.linkhelm.app

import android.app.AlarmManager
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.VpnService
import android.net.wifi.WifiManager
import android.os.*
import androidx.core.app.NotificationCompat
import engine.Engine
import kotlin.concurrent.thread

class MyVpnServiceTun2Socks : VpnService() {

    private var vpnInterface: ParcelFileDescriptor? = null
    private var isRunning = false
    private var proxyIp = ""
    private var proxyPort = 0
    // v1.5.43: SOCKS5 credentials (host enforces RFC 1929).
    private var proxyUser = ProxyServer.DEFAULT_SOCKS_USER
    private var proxyPass = ProxyServer.DEFAULT_SOCKS_PASS
    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null

    companion object {
        const val ACTION_STOP = "com.linkhelm.app.STOP"
        var isServiceRunning = false // ADD THIS
        // v1.4.6: sustained keepalive misses before the UI notes link degradation.
        // v1.4.7: 3 strikes × ~10s ≈ 30s of sustained loss before the
        // "link flapping" notification (was 6 x 3s = 18s at the old cadence).
        private const val LINK_KEEPALIVE_FAST_STRIKES = 3
    }

    override fun onCreate() {
        super.onCreate()
        acquireLocks()
    }

    private fun acquireLocks() {
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "LinkHelm::ClientWakeLock")
        wakeLock?.acquire()

        val wm = applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
        wifiLock = wm.createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "LinkHelm::ClientWifiLock")
        wifiLock?.acquire()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val prefs = getSharedPreferences("linkhelm_prefs", Context.MODE_PRIVATE)

        if (intent?.action == ACTION_STOP) {
            isServiceRunning = false
            prefs.edit().putBoolean("vpn_is_running_flag", false).apply()
            DebugUtils.log("Stop Action Received via Intent")
            shutdownService()
            return START_NOT_STICKY
        }

        if (isServiceRunning) {
            startForegroundNotification()
            return START_STICKY
        }

        isServiceRunning = true
        prefs.edit().putBoolean("vpn_is_running_flag", true).apply()

        if (intent != null) {
            proxyIp = intent.getStringExtra("PROXY_IP") ?: "192.168.49.1"
            proxyPort = intent.getIntExtra("PROXY_PORT", 8080)
            // v1.5.43: SOCKS5 credentials (host enforces RFC 1929) — passed with the connection and persisted.
            proxyUser = intent.getStringExtra("PROXY_USER")
                ?: prefs.getString("last_socks_user", ProxyServer.DEFAULT_SOCKS_USER)
                ?: ProxyServer.DEFAULT_SOCKS_USER
            proxyPass = intent.getStringExtra("PROXY_PASS")
                ?: prefs.getString("last_socks_pass", ProxyServer.DEFAULT_SOCKS_PASS)
                ?: ProxyServer.DEFAULT_SOCKS_PASS
            prefs.edit()
                .putString("last_proxy_ip", proxyIp)
                .putInt("last_proxy_port", proxyPort)
                .putString("last_socks_user", proxyUser)
                .putString("last_socks_pass", proxyPass)
                .apply()
        } else {
            proxyIp = prefs.getString("last_proxy_ip", "192.168.49.1") ?: "192.168.49.1"
            proxyPort = prefs.getInt("last_proxy_port", 8080)
            proxyUser = prefs.getString("last_socks_user", ProxyServer.DEFAULT_SOCKS_USER)
                ?: ProxyServer.DEFAULT_SOCKS_USER
            proxyPass = prefs.getString("last_socks_pass", ProxyServer.DEFAULT_SOCKS_PASS)
                ?: ProxyServer.DEFAULT_SOCKS_PASS
            DebugUtils.log("Service restarted by system. Restored config: $proxyIp:$proxyPort")
        }

        startForegroundNotification()

        thread(name = "ProxyTest") {
            if (DebugUtils.testProxyConnection(proxyIp, proxyPort)) {
                startVpnWithTun2Socks()
            } else {
                updateNotification("Error: Cannot reach proxy at $proxyIp:$proxyPort")
                stopSelf()
            }
        }

        return START_STICKY
    }

    private fun resetConnections() {
        try {
            Engine.stop()
            Thread.sleep(500)
            vpnInterface?.close()
            vpnInterface = null
            DebugUtils.log("✓ Connections reset")
        } catch (e: Exception) {
            DebugUtils.error("Error resetting connections", e)
        }
    }

    private fun shutdownService() {
        isRunning = false

        // 1. Close the interface first to drop the system VPN route
        try {
            vpnInterface?.close()
            vpnInterface = null
        } catch (e: Exception) {}

        // 2. Stop the native engine
        try {
            Engine.stop()
        } catch (e: Exception) {}

        // 3. Remove foreground status and stop
        stopForeground(true)
        stopSelf()

        DebugUtils.log("Service shutdown sequence complete")
    }

    private fun startVpnWithTun2Socks() {
        try {
            vpnInterface?.close()
            vpnInterface = null
            Thread.sleep(500)

            DebugUtils.log("Setting up fresh VPN interface...")

            DebugUtils.log("Setting up VPN interface...")

            val builder = Builder()
                .setMtu(1350) // LOWER MTU: 1350 is the "sweet spot" for gaming over proxies
                .addAddress("10.0.0.2", 24)
                .addRoute("0.0.0.0", 0) // Route EVERYTHING
                // .addRoute("::", 0) // Uncomment if you want to block IPv6 leaks
                .addDisallowedApplication(packageName)
                // v1.4.6: in-country fast resolvers — 8.8.8.8/1.1.1.1 relayed
                // over the phone's cellular link made every lookup slow and
                // lossy, amplifying the "sometimes fast, sometimes slow" feel.
                .addDnsServer("223.5.5.5")
                .addDnsServer("119.29.29.29")
                .setSession("LinkHelm")
                .setBlocking(true) // Crucial for gaming stability

            vpnInterface = builder.establish()

            if (vpnInterface == null) {
                DebugUtils.error("Failed to establish VPN interface")
                updateNotification(L10n.tr("Error: failed to create the connection channel", "错误：连接通道创建失败"))
                stopSelf()
                return
            }

            val fd = vpnInterface!!.fd
            DebugUtils.log("VPN interface established with fd: $fd")

            isRunning = true
            updateNotification(L10n.tr("LinkHelm connecting - via tun2socks", "LinkHelm 连接中 - 经 tun2socks 转发"))

            // Start tun2socks in a separate thread
            thread(name = "tun2socks-engine", isDaemon = false) {
                runTun2Socks(fd)
            }

            // v1.4.6: link warmer — a minimal SOCKS5 greeting keeps the Wi-Fi
            // Direct link out of power-save, so tun2socks' on-demand dials no
            // longer hit a dozing radio (root cause of the i/o-timeout storms).
            // This service's own traffic bypasses the VPN (addDisallowedApplication),
            // so the probe reaches the host directly. Failures are silent unless
            // sustained; the VPN engine itself is never torn down from here.
            // v1.4.7: pacing reworked for multi-device hosts. 3s per device was
            // far too chatty — with several downstreams greeting simultaneously
            // the accept queue and radio wake-ups collided and links dropped
            // ("handshake too frequent... disconnect on time conflict"). Now: 10s cadence + a 1-2s random
            // jitter per round, so with many connected devices each client
            // greets in its own window instead of in lockstep. Sustained-loss
            // notification stays at ~30s (3 strikes × 10s).
            thread(name = "link-keepalive", isDaemon = true) {
                val jitter = java.security.SecureRandom()
                // Stagger the FIRST probe too, so N devices starting together do
                // not all greet at t=0.
                Thread.sleep(jitter.nextLong(1_000, 2_500))
                var failures = 0
                var degraded = false
                while (isRunning) {
                    try {
                        java.net.Socket().use { probe ->
                            probe.connect(java.net.InetSocketAddress(proxyIp, proxyPort), 2000)
                            probe.getOutputStream().write(byteArrayOf(5, 1, 0))
                            probe.getOutputStream().flush()
                            val resp = ByteArray(2)
                            val n = probe.getInputStream().read(resp)
                            if (n == 2 && resp[0] == 5.toByte()) {
                                if (degraded) {
                                    degraded = false
                                    DebugUtils.log("✓ Link warm again after $failures misses")
                                    updateNotification(L10n.tr("LinkHelm connected $proxyIp:$proxyPort", "✓ LinkHelm 已连接 $proxyIp:$proxyPort"))
                                }
                                failures = 0
                            } else {
                                failures++
                            }
                        }
                    } catch (e: Exception) {
                        failures++
                    }
                    if (failures >= LINK_KEEPALIVE_FAST_STRIKES && !degraded) {
                        degraded = true
                        DebugUtils.log("Link unreachable for ~${failures * 10}s; VPN dials will fail until it recovers")
                        updateNotification(L10n.tr("Link flapping; it will recover automatically...", "链路抖动中，恢复后自动好转…"))
                    }
                    try {
                        // v1.4.7: 10s base + 1-2s jitter — per-device handshake
                        // windows never coincide across multiple downstreams.
                        Thread.sleep(10_000 + jitter.nextLong(1_000, 2_000))
                    } catch (_: InterruptedException) {
                        break
                    }
                }
            }

        } catch (e: Exception) {
            DebugUtils.error("Failed to start VPN", e)
            updateNotification("Error: ${e.message}")
            stopSelf()
        }
    }

    private fun runTun2Socks(fd: Int) {
        try {
            // v1.5.43: host enforces SOCKS5 credentials — embedded in the URL (standard
            // socks5://user:pass@host:port form). Credentials are alphanumeric only, no escaping needed.
            val socksProxy = "socks5://$proxyUser:$proxyPass@$proxyIp:$proxyPort"

            DebugUtils.log("Configuring tun2socks engine...")

            // 1. Create a Key object for configuration
            val key = engine.Key()

            // 2. Set the parameters using the Key object
            // Note: The device must be "fd://<number>" for Android
            key.setDevice("fd://$fd")
            key.setProxy(socksProxy)
            key.setMTU(1420L)
            key.setLogLevel("info")

            // Optional: Some versions allow setting DNS here,
            // but often it's handled by the VPN Builder routes
            // key.setDNS("8.8.8.8,8.8.4.4")

            // 3. Register the configuration and start the engine
            Engine.insert(key)
            Engine.start()

            DebugUtils.log("✓ tun2socks started successfully!")
            updateNotification(L10n.tr("LinkHelm connected $proxyIp:$proxyPort", "✓ LinkHelm 已连接 $proxyIp:$proxyPort"))

            // Keep the thread alive while VPN is running
            while (isRunning) {
                Thread.sleep(500)
            }

        } catch (e: Exception) {
            DebugUtils.error("tun2socks error", e)
            updateNotification("Error: ${e.message}")
        } finally {
            DebugUtils.log("tun2socks engine stopped")
        }
    }

    private fun startForegroundNotification() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                "vpn_channel",
                L10n.tr("LinkHelm Service", "LinkHelm 服务"),
                NotificationManager.IMPORTANCE_LOW
            )
            getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
        }

        val notification = NotificationCompat.Builder(this, "vpn_channel")
            .setContentTitle("LinkHelm")
            .setContentText("Initializing tun2socks...")
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            startForeground(1, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(1, notification)
        }
    }

    private fun updateNotification(message: String) {
        val notification = NotificationCompat.Builder(this, "vpn_channel")
            .setContentTitle("LinkHelm")
            .setContentText(message)
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()

        getSystemService(NotificationManager::class.java).notify(1, notification)
    }

    override fun onTaskRemoved(rootIntent: Intent?) {
        DebugUtils.log("App swiped — scheduling VPN restart...")
        val prefs = getSharedPreferences("linkhelm_prefs", Context.MODE_PRIVATE)
        val savedIp = prefs.getString("last_proxy_ip", proxyIp) ?: proxyIp
        val savedPort = prefs.getInt("last_proxy_port", proxyPort)

        val restartIntent = Intent(applicationContext, MyVpnServiceTun2Socks::class.java).apply {
            putExtra("PROXY_IP", savedIp)
            putExtra("PROXY_PORT", savedPort)
        }

        val pendingIntent = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            PendingIntent.getForegroundService(
                applicationContext, 2, restartIntent,
                PendingIntent.FLAG_ONE_SHOT or PendingIntent.FLAG_IMMUTABLE
            )
        } else {
            PendingIntent.getService(
                applicationContext, 2, restartIntent,
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
            DebugUtils.error("Exact alarm permission missing in VPN service, using inexact fallback", e)
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

    override fun onDestroy() {
        DebugUtils.log("CRITICAL: Executing Stop Sequence...")
        isServiceRunning = false
        val prefs = getSharedPreferences("linkhelm_prefs", Context.MODE_PRIVATE)
        prefs.edit().putBoolean("vpn_is_running_flag", false).apply()

        wakeLock?.let { if (it.isHeld) it.release() }
        wifiLock?.let { if (it.isHeld) it.release() }

        try {
            vpnInterface?.close()
            vpnInterface = null
            DebugUtils.log("✓ VPN Interface closed (Routing removed)")
        } catch (e: Exception) {
            DebugUtils.error("Error closing interface", e)
        }

        try {
            Engine.stop()
            DebugUtils.log("✓ tun2socks engine stopped")
        } catch (e: Exception) {
            DebugUtils.error("Error stopping engine", e)
        }

        stopForeground(true)
        val notificationManager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        notificationManager.cancel(1)

        DebugUtils.log("VPN Service fully destroyed")

        super.onDestroy()
    }


    override fun onRevoke() {
        DebugUtils.log("VPN Permission revoked by system")
        stopSelf()
        super.onRevoke()
    }
}