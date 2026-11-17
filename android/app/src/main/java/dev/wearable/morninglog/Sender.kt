package dev.wearable.morninglog

import android.content.Context
import android.os.Handler
import android.os.Looper
import android.util.Log
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

/** Sends queued scores to the Apps Script web app. Never logs the URL or token. */
object Sender {
    private const val TAG = "MorningLog"
    private const val TIMEOUT_MS = 10_000
    private val sendLock = Any()

    val configured: Boolean get() = BuildConfig.SELF_REPORT_URL.isNotBlank()

    /** Result of a flush: entries that went out, and entries still queued afterwards. */
    data class Result(val sent: List<Entry>, val remaining: Int)

    /** Blocking: try every queued entry once, then (re)arm or cancel the retry alarm. Call off the main thread. */
    fun flush(context: Context): Result = synchronized(sendLock) {
        val store = Store(context)
        val sent = mutableListOf<Entry>()
        if (configured) {
            for (e in store.pending()) {
                if (send(e)) {
                    store.remove(e)
                    sent += e
                }
            }
        }
        val remaining = store.pending().size
        Alarms.scheduleRetry(context, remaining > 0)
        Result(sent, remaining)
    }

    /** Run [flush] on a background thread and deliver the result on the main thread. */
    fun flushAsync(context: Context, then: (Result) -> Unit) {
        val app = context.applicationContext
        val main = Handler(Looper.getMainLooper())
        Thread {
            val r = flush(app)
            main.post { then(r) }
        }.start()
    }

    private fun send(e: Entry): Boolean = try {
        val body = JSONObject()
            .put("token", BuildConfig.SELF_REPORT_TOKEN)
            .put("date", e.date)
            .put("readiness", e.readiness)
            .put("logged_at", e.loggedAt)
            .toString()
        val url = URL(BuildConfig.SELF_REPORT_URL)
        val post = url.openConnection() as HttpURLConnection
        val reply = try {
            post.instanceFollowRedirects = false
            post.requestMethod = "POST"
            post.connectTimeout = TIMEOUT_MS
            post.readTimeout = TIMEOUT_MS
            post.doOutput = true
            post.setRequestProperty("Content-Type", "application/json")
            post.outputStream.use { it.write(body.toByteArray(Charsets.UTF_8)) }
            when (val code = post.responseCode) {
                // Apps Script runs the script on POST, then answers 302 -> googleusercontent with the output.
                HttpURLConnection.HTTP_MOVED_PERM, HttpURLConnection.HTTP_MOVED_TEMP, HttpURLConnection.HTTP_SEE_OTHER -> {
                    val location = post.getHeaderField("Location") ?: error("redirect without Location")
                    get(URL(url, location))
                }
                HttpURLConnection.HTTP_OK -> post.inputStream.bufferedReader().use { it.readText() }
                else -> error("HTTP $code")
            }
        } finally {
            post.disconnect()
        }
        val ok = JSONObject(reply).optBoolean("ok", false)
        if (!ok) Log.w(TAG, "send ${e.date} rejected by server")
        ok
    } catch (ex: Exception) {
        Log.w(TAG, "send ${e.date} failed: ${ex.javaClass.simpleName}")
        false
    }

    private fun get(url: URL): String {
        val c = url.openConnection() as HttpURLConnection
        try {
            c.connectTimeout = TIMEOUT_MS
            c.readTimeout = TIMEOUT_MS
            val code = c.responseCode
            if (code != HttpURLConnection.HTTP_OK) error("HTTP $code")
            return c.inputStream.bufferedReader().use { it.readText() }
        } finally {
            c.disconnect()
        }
    }
}
