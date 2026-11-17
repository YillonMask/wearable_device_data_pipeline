package dev.wearable.morninglog

import android.app.AlarmManager
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.media.AudioAttributes
import android.media.RingtoneManager
import android.os.Build
import android.os.SystemClock
import android.util.Log
import java.util.Calendar

/** AlarmManager scheduling plus the full-screen "ringing" notification. */
object Alarms {
    const val ACTION_FIRE = "dev.wearable.morninglog.FIRE"
    const val ACTION_SNOOZE_FIRE = "dev.wearable.morninglog.SNOOZE_FIRE"
    const val ACTION_RETRY = "dev.wearable.morninglog.RETRY"
    const val SNOOZE_MS = 9 * 60_000L
    private const val RETRY_MS = 30 * 60_000L
    private const val CHANNEL = "alarm"
    private const val NOTIFICATION_ID = 1
    private const val REQ_FIRE = 1
    private const val REQ_SNOOZE = 2
    private const val REQ_RETRY = 3
    private const val REQ_SHOW = 4
    private const val REQ_RING = 5

    /** Next wall-clock time of HH:MM strictly after [after]. */
    fun nextTrigger(store: Store, after: Long = System.currentTimeMillis()): Long {
        val c = Calendar.getInstance().apply {
            timeInMillis = after
            set(Calendar.HOUR_OF_DAY, store.hour)
            set(Calendar.MINUTE, store.minute)
            set(Calendar.SECOND, 0)
            set(Calendar.MILLISECOND, 0)
        }
        if (c.timeInMillis <= after) c.add(Calendar.DAY_OF_YEAR, 1)
        return c.timeInMillis
    }

    /**
     * Arm (or cancel, if disabled) the daily alarm. [justFired] skips past the current minute so an
     * alarm delivered a few ms early cannot re-arm itself for the same morning.
     */
    fun scheduleDaily(context: Context, justFired: Boolean = false) {
        val store = Store(context)
        val pi = broadcast(context, ACTION_FIRE, REQ_FIRE)
        if (!store.enabled) {
            am(context).cancel(pi)
            return
        }
        val now = System.currentTimeMillis()
        setAlarmClock(context, nextTrigger(store, if (justFired) now + 60_000 else now), pi)
    }

    fun snooze(context: Context) {
        val at = System.currentTimeMillis() + SNOOZE_MS
        Store(context).snoozeAt = at
        setAlarmClock(context, at, broadcast(context, ACTION_SNOOZE_FIRE, REQ_SNOOZE))
    }

    fun cancelSnooze(context: Context) {
        Store(context).snoozeAt = 0
        am(context).cancel(broadcast(context, ACTION_SNOOZE_FIRE, REQ_SNOOZE))
    }

    /** Re-arm a snooze that was pending before a reboot/update. */
    fun restoreSnooze(context: Context) {
        val at = Store(context).snoozeAt
        if (at > System.currentTimeMillis()) setAlarmClock(context, at, broadcast(context, ACTION_SNOOZE_FIRE, REQ_SNOOZE))
        else if (at != 0L) Store(context).snoozeAt = 0
    }

    /** Inexact retry ~30 min out while the queue is non-empty; cancelled once it drains. */
    fun scheduleRetry(context: Context, needed: Boolean) {
        val pi = broadcast(context, ACTION_RETRY, REQ_RETRY)
        if (needed) am(context).set(AlarmManager.ELAPSED_REALTIME_WAKEUP, SystemClock.elapsedRealtime() + RETRY_MS, pi)
        else am(context).cancel(pi)
    }

    fun canScheduleExact(context: Context): Boolean =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.S || am(context).canScheduleExactAlarms()

    fun canUseFullScreen(context: Context): Boolean =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.UPSIDE_DOWN_CAKE ||
            context.getSystemService(NotificationManager::class.java).canUseFullScreenIntent()

    /** Post the ringing notification whose full-screen intent opens the log screen over the lock screen. */
    fun showRinging(context: Context) {
        val nm = context.getSystemService(NotificationManager::class.java)
        ensureChannel(nm)
        val ring = PendingIntent.getActivity(
            context, REQ_RING,
            Intent(context, LogActivity::class.java)
                .putExtra(LogActivity.EXTRA_RINGING, true)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val n = Notification.Builder(context, CHANNEL)
            .setSmallIcon(R.drawable.ic_alarm)
            .setContentTitle(context.getString(R.string.notif_title))
            .setContentText(context.getString(R.string.notif_text))
            .setCategory(Notification.CATEGORY_ALARM)
            .setVisibility(Notification.VISIBILITY_PUBLIC)
            .setOngoing(true)
            .setContentIntent(ring)
            .setFullScreenIntent(ring, true)
            .build()
        // Keeps the channel's alarm sound looping if the full-screen screen is not shown (phone in use);
        // the log screen cancels this and takes over the ringing once it opens.
        n.flags = n.flags or Notification.FLAG_INSISTENT
        try {
            nm.notify(NOTIFICATION_ID, n)
        } catch (e: SecurityException) {
            Log.w("MorningLog", "notification permission missing")
        }
    }

    fun cancelRinging(context: Context) =
        context.getSystemService(NotificationManager::class.java).cancel(NOTIFICATION_ID)

    private fun ensureChannel(nm: NotificationManager) {
        val ch = NotificationChannel(CHANNEL, "Morning alarm", NotificationManager.IMPORTANCE_HIGH).apply {
            description = "Rings each morning to log how you feel"
            setSound(
                RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM),
                AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ALARM)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build(),
            )
            enableVibration(true)
            vibrationPattern = longArrayOf(0, 800, 600)
            lockscreenVisibility = Notification.VISIBILITY_PUBLIC
        }
        nm.createNotificationChannel(ch)
    }

    private fun setAlarmClock(context: Context, at: Long, operation: PendingIntent) {
        if (!canScheduleExact(context)) {
            Log.w("MorningLog", "exact alarm permission not granted; alarm not armed")
            return
        }
        val show = PendingIntent.getActivity(
            context, REQ_SHOW, Intent(context, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE,
        )
        try {
            am(context).setAlarmClock(AlarmManager.AlarmClockInfo(at, show), operation)
        } catch (e: SecurityException) {
            Log.w("MorningLog", "exact alarm permission revoked; alarm not armed")
        }
    }

    private fun broadcast(context: Context, action: String, req: Int): PendingIntent =
        PendingIntent.getBroadcast(
            context, req, Intent(context, AlarmReceiver::class.java).setAction(action),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )

    private fun am(context: Context) = context.getSystemService(AlarmManager::class.java)
}
