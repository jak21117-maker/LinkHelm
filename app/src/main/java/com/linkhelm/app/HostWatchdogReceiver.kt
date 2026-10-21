package com.linkhelm.app

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * v1.5.33: watchdog against aggressive OEM battery savers (MIUI/EMUI/etc.)
 * that freeze or kill the foreground sharing service. The alarm chain is
 * registered with AlarmManager, so it SURVIVES process death: when the tick
 * fires and the sharing flag is still set but the service is gone, the
 * process is started again purely to run this receiver and the service is
 * brought back to the foreground.
 */
class HostWatchdogReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        val prefs = context.getSharedPreferences("linkhelm_prefs", Context.MODE_PRIVATE)
        if (!prefs.getBoolean("host_is_running_flag", false)) {
            // Sharing is genuinely off — let the chain die quietly.
            return
        }
        if (!HostService.isServiceRunning) {
            // The service was killed by power policy — resurrect it.
            context.startForegroundService(Intent(context, HostService::class.java))
        }
        HostService.scheduleWatchdog(context)
    }
}
