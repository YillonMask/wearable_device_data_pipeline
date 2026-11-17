package dev.wearable.morninglog

import android.app.Activity
import android.content.Intent
import android.media.AudioAttributes
import android.media.MediaPlayer
import android.media.RingtoneManager
import android.os.Build
import android.os.Bundle
import android.os.VibrationAttributes
import android.os.VibrationEffect
import android.os.Vibrator
import android.view.View
import android.view.WindowManager
import android.widget.Button
import android.widget.SeekBar
import android.widget.TextView
import java.time.Instant
import java.time.LocalDate
import java.time.temporal.ChronoUnit
import java.util.Locale

/** The rating screen. Opened by the alarm (ringing) or from "Log now" (silent). */
class LogActivity : Activity() {
    private var player: MediaPlayer? = null
    private var vibrator: Vibrator? = null
    private var ringing = false

    private lateinit var value: TextView
    private lateinit var seek: SeekBar
    private lateinit var status: TextView
    private lateinit var snooze: Button

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // API 27+ uses the manifest's showWhenLocked/turnScreenOn; API 26 needs the window flags.
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O_MR1) {
            @Suppress("DEPRECATION")
            window.addFlags(WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON)
        }
        setContentView(R.layout.activity_log)
        value = findViewById(R.id.value)
        seek = findViewById(R.id.seek)
        status = findViewById(R.id.status)
        snooze = findViewById(R.id.snooze)

        seek.setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
            override fun onProgressChanged(s: SeekBar, p: Int, fromUser: Boolean) = showValue()
            override fun onStartTrackingTouch(s: SeekBar) = Unit
            override fun onStopTrackingTouch(s: SeekBar) = Unit
        })
        showValue()
        findViewById<Button>(R.id.save).setOnClickListener { save() }
        snooze.setOnClickListener {
            stopRinging()
            Alarms.snooze(this)
            finish()
        }
        handle(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handle(intent)
    }

    private fun handle(intent: Intent) {
        if (intent.getBooleanExtra(EXTRA_RINGING, false)) startRinging()
        snooze.visibility = if (ringing) View.VISIBLE else View.GONE
    }

    /** SeekBar is 0..99; the score is 1..100. */
    private fun score() = seek.progress + 1

    private fun showValue() {
        value.text = String.format(Locale.getDefault(), "%d", score())
    }

    private fun save() {
        stopRinging()
        snooze.visibility = View.GONE
        val entry = Entry(
            date = LocalDate.now().toString(),
            readiness = score(),
            loggedAt = Instant.now().truncatedTo(ChronoUnit.SECONDS).toString(),
        )
        Store(this).enqueue(entry)
        status.text = getString(R.string.saving)
        Sender.flushAsync(this) { r ->
            if (isDestroyed) return@flushAsync
            status.text = when {
                entry in r.sent -> getString(R.string.saved_sent)
                Sender.configured -> getString(R.string.saved_retry)
                else -> getString(R.string.saved_retry) + "\n" + getString(R.string.not_configured)
            }
        }
    }

    private fun startRinging() {
        // The activity takes over from the notification's insistent sound.
        Alarms.cancelRinging(this)
        Alarms.cancelSnooze(this)
        if (ringing) return
        ringing = true
        val uri = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM)
            ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE)
        player = try {
            MediaPlayer().apply {
                setAudioAttributes(
                    AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ALARM)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build(),
                )
                setDataSource(this@LogActivity, uri)
                isLooping = true
                prepare()
                start()
            }
        } catch (e: Exception) {
            null
        }
        val pattern = VibrationEffect.createWaveform(longArrayOf(0, 800, 600), 0)
        vibrator = getSystemService(Vibrator::class.java)?.also { v ->
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                v.vibrate(pattern, VibrationAttributes.createForUsage(VibrationAttributes.USAGE_ALARM))
            } else {
                v.vibrate(pattern)
            }
        }
    }

    private fun stopRinging() {
        Alarms.cancelRinging(this)
        ringing = false
        player?.run {
            runCatching { stop() }
            release()
        }
        player = null
        vibrator?.cancel()
        vibrator = null
    }

    @Deprecated("Back is ignored while ringing so the alarm can't be dismissed without Save or Snooze.")
    override fun onBackPressed() {
        if (!ringing) @Suppress("DEPRECATION") super.onBackPressed()
    }

    override fun onDestroy() {
        stopRinging()
        super.onDestroy()
    }

    companion object {
        const val EXTRA_RINGING = "ringing"
    }
}
