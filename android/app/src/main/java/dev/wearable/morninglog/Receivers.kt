package dev.wearable.morninglog

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Handles our own explicit alarms: daily fire, snooze fire, and send retry. Not exported. */
class AlarmReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        when (intent.action) {
            Alarms.ACTION_FIRE -> {
                Alarms.scheduleDaily(context, justFired = true)
                Alarms.showRinging(context)
            }
            Alarms.ACTION_SNOOZE_FIRE -> {
                Store(context).snoozeAt = 0
                Alarms.showRinging(context)
            }
            Alarms.ACTION_RETRY -> Unit
            else -> return
        }
        flushInBackground(context)
    }
}

/** Re-arms alarms after a reboot or an app update (AlarmManager forgets them on both). */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED && intent.action != Intent.ACTION_MY_PACKAGE_REPLACED) return
        Alarms.scheduleDaily(context)
        Alarms.restoreSnooze(context)
        flushInBackground(context)
    }
}

private fun BroadcastReceiver.flushInBackground(context: Context) {
    val pending = goAsync()
    val app = context.applicationContext
    Thread {
        try {
            Sender.flush(app)
        } finally {
            pending.finish()
        }
    }.start()
}
