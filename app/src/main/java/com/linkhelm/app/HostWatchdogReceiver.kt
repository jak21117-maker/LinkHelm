package com.linkhelm.app

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build
import android.util.Log

/**
 * v1.5.33: watchdog against aggressive OEM battery savers (MIUI/EMUI/etc.)
 * that freeze or kill the foreground sharing service. The alarm chain is
 * registered with AlarmManager, so it SURVIVES process death: when the tick
 * fires and the sharing flag is still set but the service is gone, the
 * process is started again purely to run this receiver and the service is
 * brought back to the foreground.
 *
 * beta1.0.54: deliberately small again. beta1.0.53 stacked a heartbeat, a
 * dual-slot alarm, a persisted JobScheduler job and a boot/package-replaced
 * receiver on top of this — and the boot receiver in particular started the
 * service during APK install, before Wi-Fi P2P was ready, which tore the
 * group down without rebuilding it ("连不上，删除后没创建出新的").
 * Keepalive here is: one alarm tick, a liveness check, a guarded restart.
 * The start call is wrapped so a ForegroundServiceStartNotAllowedException is
 * logged instead of silently killing the receiver.
 */
class HostWatchdogReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        if (!HostService.isSharingFlagSet(context)) {
            // Sharing is genuinely off — let the chain die quietly.
            HostService.cancelWatchdog(context)
            return
        }

        if (HostService.isServiceRunning) {
            // Healthy: just keep the chain armed.
            HostService.scheduleWatchdog(context)
            return
        }

        Log.w("HostWatchdog", "sharing flag is ON but the service is gone — resurrecting")
        val serviceIntent = Intent(context, HostService::class.java)
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(serviceIntent)
            } else {
                context.startService(serviceIntent)
            }
        } catch (e: Exception) {
            // Android 12+ ForegroundServiceStartNotAllowedException, or an OEM
            // SecurityException variant. Nothing more a plain BroadcastReceiver
            // can do — log it and keep the alarm armed so a later tick can
            // retry once the app is allowed to start foreground services again.
            Log.w("HostWatchdog", "restart refused: ${e.javaClass.simpleName}")
        }
        HostService.scheduleWatchdog(context)
    }
}
