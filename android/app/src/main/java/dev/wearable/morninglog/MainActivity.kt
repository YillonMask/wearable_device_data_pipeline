package dev.wearable.morninglog

import android.Manifest
import android.app.Activity
import android.app.NotificationManager
import android.app.TimePickerDialog
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.text.format.DateFormat
import android.view.View
import android.widget.Button
import android.widget.Switch
import android.widget.TextView
import java.util.Date

/** Settings screen: alarm time + on/off, Log now, unsent queue, config status. Shows no device data. */
class MainActivity : Activity() {
    private lateinit var store: Store
    private lateinit var time: Button
    private lateinit var enabled: Switch
    private lateinit var next: TextView
    private lateinit var pending: TextView
    private lateinit var config: TextView
    private lateinit var fullScreen: Button
    private lateinit var exact: Button

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        store = Store(this)
        time = findViewById(R.id.time)
        enabled = findViewById(R.id.enabled)
        next = findViewById(R.id.next)
        pending = findViewById(R.id.pending)
        config = findViewById(R.id.config)
        fullScreen = findViewById(R.id.full_screen)
        exact = findViewById(R.id.exact)

        time.setOnClickListener {
            TimePickerDialog(this, { _, h, m ->
                store.hour = h
                store.minute = m
                Alarms.scheduleDaily(this)
                refresh()
            }, store.hour, store.minute, DateFormat.is24HourFormat(this)).show()
        }
        enabled.setOnCheckedChangeListener { _, on ->
            if (on == store.enabled) return@setOnCheckedChangeListener
            store.enabled = on
            if (!on) Alarms.cancelSnooze(this)
            Alarms.scheduleDaily(this)
            refresh()
        }
        findViewById<Button>(R.id.log_now).setOnClickListener {
            startActivity(Intent(this, LogActivity::class.java))
        }
        findViewById<Button>(R.id.send_now).setOnClickListener { flush() }
        fullScreen.setOnClickListener {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
                startActivity(
                    Intent(Settings.ACTION_MANAGE_APP_USE_FULL_SCREEN_INTENT, Uri.parse("package:$packageName")),
                )
            }
        }
        exact.setOnClickListener {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                startActivity(Intent(Settings.ACTION_REQUEST_SCHEDULE_EXACT_ALARM, Uri.parse("package:$packageName")))
            }
        }

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        }
    }

    override fun onResume() {
        super.onResume()
        Alarms.scheduleDaily(this)
        refresh()
        flush()
    }

    private fun flush() {
        Sender.flushAsync(this) { if (!isDestroyed) refresh() }
    }

    private fun refresh() {
        val fmt = DateFormat.getTimeFormat(this)
        time.text = getString(R.string.alarm_time, fmt.format(Date(Alarms.nextTrigger(store))))
        enabled.isChecked = store.enabled
        next.text = if (store.enabled) {
            val at = Alarms.nextTrigger(store)
            getString(R.string.next_alarm, DateFormat.format("EEE", at), fmt.format(Date(at)))
        } else {
            getString(R.string.alarm_off)
        }
        pending.text = getString(R.string.unsent, store.pending().size)

        val warnings = mutableListOf<String>()
        warnings += getString(if (Sender.configured) R.string.configured else R.string.not_configured)
        if (!getSystemService(NotificationManager::class.java).areNotificationsEnabled()) {
            warnings += getString(R.string.notifications_off)
        }
        config.text = warnings.joinToString("\n")
        fullScreen.visibility = if (Alarms.canUseFullScreen(this)) View.GONE else View.VISIBLE
        exact.visibility = if (Alarms.canScheduleExact(this)) View.GONE else View.VISIBLE
    }
}
