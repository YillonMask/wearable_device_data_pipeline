package dev.wearable.morninglog

import android.annotation.SuppressLint
import android.content.Context
import org.json.JSONObject

/** One unsent score. Only the latest entry per local [date] is kept. */
data class Entry(val date: String, val readiness: Int, val loggedAt: String)

/** SharedPreferences-backed settings and send queue. Queue writes use commit() so a score survives a kill. */
@SuppressLint("ApplySharedPref")
class Store(context: Context) {
    private val prefs = context.applicationContext.getSharedPreferences("morning_log", Context.MODE_PRIVATE)

    var hour: Int
        get() = prefs.getInt("hour", 7)
        set(v) = prefs.edit().putInt("hour", v).apply()

    var minute: Int
        get() = prefs.getInt("minute", 0)
        set(v) = prefs.edit().putInt("minute", v).apply()

    var enabled: Boolean
        get() = prefs.getBoolean("enabled", false)
        set(v) = prefs.edit().putBoolean("enabled", v).apply()

    /** Wall-clock millis of a pending snooze, or 0. Kept so a reboot doesn't lose it. */
    var snoozeAt: Long
        get() = prefs.getLong("snooze_at", 0L)
        set(v) = prefs.edit().putLong("snooze_at", v).apply()

    fun pending(): List<Entry> = synchronized(LOCK) {
        val q = queue()
        q.keys().asSequence().sorted().map { d ->
            val o = q.getJSONObject(d)
            Entry(d, o.getInt("readiness"), o.getString("logged_at"))
        }.toList()
    }

    fun enqueue(e: Entry) = synchronized(LOCK) {
        val q = queue()
        q.put(e.date, JSONObject().put("readiness", e.readiness).put("logged_at", e.loggedAt))
        prefs.edit().putString("queue", q.toString()).commit()
    }

    /** Remove [e] only if it is still the latest entry for its date (a newer save may have replaced it). */
    fun remove(e: Entry) = synchronized(LOCK) {
        val q = queue()
        val cur = q.optJSONObject(e.date) ?: return@synchronized
        if (cur.optString("logged_at") == e.loggedAt && cur.optInt("readiness") == e.readiness) {
            q.remove(e.date)
            prefs.edit().putString("queue", q.toString()).commit()
        }
    }

    private fun queue(): JSONObject =
        prefs.getString("queue", null)?.let { runCatching { JSONObject(it) }.getOrNull() } ?: JSONObject()

    companion object {
        private val LOCK = Any()
    }
}
