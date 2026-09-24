package com.quintek.app

import android.Manifest
import android.app.AlarmManager
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.webkit.JavascriptInterface
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import org.json.JSONArray
import org.json.JSONObject

/*
 * Reminders, delivered by THIS PHONE (ADR-031, decided 2026-09-24).
 *
 * WHY ON-DEVICE
 * -------------
 * The server stores reminders and could fire them, but delivering from the
 * server needs a push credential (Firebase), a scheduler (a paid Render cron
 * job or an always-on instance), or both. Scheduling on the phone needs none
 * of that: the app reads the learner's pending reminders and hands each one to
 * Android's own AlarmManager, and Android shows the notification at its time.
 *
 * The cost, accepted for a single-user testing phase and for a revision
 * reminder (not an alert): a reminder only fires on a phone that has SYNCED
 * it, i.e. opened the Reminders screen since it was created or changed. FCM
 * stays recorded as the route for multi-device or guaranteed delivery.
 *
 * WHAT IS DECIDED WHERE
 * ---------------------
 * WHICH reminders to schedule is decided in JavaScript
 * (`reminderSyncPlan` in quintek-student-api.js), where it is unit-tested. This
 * file only does what JavaScript cannot: hold alarms, show notifications, and
 * put the alarms back after a reboot. Nothing here reads the server.
 *
 * THE LABEL
 * ---------
 * Delivered verbatim: the notification's text IS the label, character for
 * character, set as both the collapsed text and the expanded BigText so a
 * long or multi-line label is not cut. Nothing is prepended to it; the fixed
 * title is the app's own line, not part of the learner's words.
 *
 * TIMING
 * ------
 * `setAndAllowWhileIdle`, not an exact alarm. Exact alarms need
 * SCHEDULE_EXACT_ALARM, which Android 14 denies by default and Play reserves
 * for alarm-clock and calendar apps. An inexact alarm can be deferred a few
 * minutes in Doze; for "revise patho at 8pm" that is acceptable, and it is
 * recorded rather than hidden.
 */

private const val PREFS = "quintek_reminders"
private const val KEY_SCHEDULED = "scheduled"   // id -> {label, at}
private const val KEY_OUTCOMES = "outcomes"     // id -> "delivered" | "blocked"
private const val CHANNEL = "reminders"
private const val EXTRA_ID = "com.quintek.app.REMINDER_ID"
private const val EXTRA_LABEL = "com.quintek.app.REMINDER_LABEL"
private const val EXTRA_AT = "com.quintek.app.REMINDER_AT"

/**
 * How late a reminder may still be shown. An inexact alarm can be deferred a
 * few minutes in Doze; that is expected and still useful. Beyond this -- the
 * phone was off, or Android held the alarm far longer -- the reminder is NOT
 * shown late. It is recorded as "missed", and the list says so. "Revise patho
 * at 8pm" arriving at 3am is not a reminder, it is noise.
 */
const val LATE_TOLERANCE_MS = 15 * 60 * 1000L

/** The one rule both paths use: may a reminder due at `at` be shown at `now`? */
fun stillOnTime(at: Long, now: Long): Boolean = now - at <= LATE_TOLERANCE_MS

/** What this phone has scheduled and what happened to it. SharedPreferences,
 *  because alarms do not survive a reboot and have to be rebuilt from here. */
object ReminderStore {
    private fun prefs(ctx: Context) = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    fun scheduled(ctx: Context): JSONObject =
        JSONObject(prefs(ctx).getString(KEY_SCHEDULED, "{}") ?: "{}")

    fun saveScheduled(ctx: Context, value: JSONObject) {
        prefs(ctx).edit().putString(KEY_SCHEDULED, value.toString()).apply()
    }

    fun outcomes(ctx: Context): JSONObject =
        JSONObject(prefs(ctx).getString(KEY_OUTCOMES, "{}") ?: "{}")

    fun recordOutcome(ctx: Context, id: String, outcome: String) {
        val all = outcomes(ctx).put(id, outcome)
        val left = scheduled(ctx).apply { remove(id) }
        prefs(ctx).edit()
            .putString(KEY_OUTCOMES, all.toString())
            .putString(KEY_SCHEDULED, left.toString())
            .apply()
    }
}

object ReminderScheduler {

    private fun intentFor(ctx: Context, id: String, label: String?, at: Long = 0L): PendingIntent? {
        val intent = Intent(ctx, ReminderReceiver::class.java).setAction("com.quintek.app.REMINDER.$id")
        if (label != null) intent.putExtra(EXTRA_ID, id).putExtra(EXTRA_LABEL, label)
            .putExtra(EXTRA_AT, at)
        val flags = PendingIntent.FLAG_IMMUTABLE or
            (if (label == null) PendingIntent.FLAG_NO_CREATE else PendingIntent.FLAG_UPDATE_CURRENT)
        // The action carries the id, so each reminder is its own PendingIntent
        // and cancelling one never cancels another.
        return PendingIntent.getBroadcast(ctx, 0, intent, flags)
    }

    private fun arm(ctx: Context, id: String, label: String, at: Long) {
        val alarms = ctx.getSystemService(AlarmManager::class.java) ?: return
        val pending = intentFor(ctx, id, label, at) ?: return
        alarms.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, at, pending)
    }

    private fun disarm(ctx: Context, id: String) {
        val alarms = ctx.getSystemService(AlarmManager::class.java) ?: return
        intentFor(ctx, id, null)?.let { alarms.cancel(it); it.cancel() }
    }

    /**
     * Make this phone's alarms exactly `plan`: `[{id, label, at}]`, `at` in
     * epoch milliseconds. Anything scheduled earlier and absent from the plan
     * -- cancelled, edited away, deleted with the account -- is disarmed.
     * Returns how many are now scheduled.
     */
    fun replaceWith(ctx: Context, plan: JSONArray): Int {
        val before = ReminderStore.scheduled(ctx)
        val after = JSONObject()
        for (i in 0 until plan.length()) {
            val item = plan.getJSONObject(i)
            val id = item.getString("id")
            val label = item.getString("label")
            val at = item.getLong("at")
            after.put(id, JSONObject().put("label", label).put("at", at))
            arm(ctx, id, label, at)
        }
        val gone = before.keys().asSequence().filter { !after.has(it) }.toList()
        gone.forEach { disarm(ctx, it) }
        ReminderStore.saveScheduled(ctx, after)
        return after.length()
    }

    /** After a reboot or an app update Android has dropped every alarm. Each
     *  is put back -- except one whose time passed while the phone was off,
     *  beyond the tolerance: that is recorded as missed, not fired late. */
    fun rearmAll(ctx: Context) {
        val all = ReminderStore.scheduled(ctx)
        val now = System.currentTimeMillis()
        for (id in all.keys().asSequence().toList()) {
            val entry = all.getJSONObject(id)
            val at = entry.getLong("at")
            if (stillOnTime(at, now)) arm(ctx, id, entry.getString("label"), at)
            else ReminderStore.recordOutcome(ctx, id, "missed")
        }
    }

    fun notificationsAllowed(ctx: Context): Boolean {
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(ctx, Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED) return false
        return NotificationManagerCompat.from(ctx).areNotificationsEnabled()
    }
}

/** The alarm went off: show the learner's words. */
class ReminderReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        val id = intent.getStringExtra(EXTRA_ID) ?: return
        val label = intent.getStringExtra(EXTRA_LABEL) ?: return
        val at = intent.getLongExtra(EXTRA_AT, 0L)
        if (at > 0L && !stillOnTime(at, System.currentTimeMillis())) {
            ReminderStore.recordOutcome(ctx, id, "missed")
            return
        }
        if (!ReminderScheduler.notificationsAllowed(ctx)) {
            // Recorded, so the Reminders screen can say it was NOT shown
            // rather than leaving the learner to assume it was.
            ReminderStore.recordOutcome(ctx, id, "blocked")
            return
        }
        val manager = ctx.getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL, "Reminders", NotificationManager.IMPORTANCE_DEFAULT))
        val open = PendingIntent.getActivity(
            ctx, 0, Intent(ctx, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP),
            PendingIntent.FLAG_IMMUTABLE)
        val notification = NotificationCompat.Builder(ctx, CHANNEL)
            .setSmallIcon(R.drawable.ic_launcher_foreground)
            .setContentTitle("Quintek reminder")
            .setContentText(label)                                   // verbatim
            .setStyle(NotificationCompat.BigTextStyle().bigText(label))
            .setContentIntent(open)
            .setAutoCancel(true)
            .build()
        try {
            NotificationManagerCompat.from(ctx).notify(id.hashCode(), notification)
            ReminderStore.recordOutcome(ctx, id, "delivered")
        } catch (e: SecurityException) {
            ReminderStore.recordOutcome(ctx, id, "blocked")
        }
    }
}

/** Alarms do not survive a reboot or an app update; put them back. */
class ReminderBootReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        when (intent.action) {
            Intent.ACTION_BOOT_COMPLETED, Intent.ACTION_MY_PACKAGE_REPLACED ->
                ReminderScheduler.rearmAll(ctx)
        }
    }
}

/**
 * What the learner's screen may ask of the phone, as `window.QuintekReminders`.
 *
 * Every method refuses unless the page on screen is the app's own bundle
 * (`allowed()`), so nothing the WebView might navigate to can schedule
 * notifications on the learner's phone.
 */
class ReminderBridge(
    private val ctx: Context,
    private val allowed: () -> Boolean,
    private val askPermission: () -> Unit,
) {
    @JavascriptInterface
    fun sync(planJson: String): String {
        if (!allowed()) return JSONObject().put("error", "not the Quintek screen").toString()
        return try {
            val n = ReminderScheduler.replaceWith(ctx, JSONArray(planJson))
            JSONObject().put("scheduled", n).put("permission", permission()).toString()
        } catch (e: Exception) {
            JSONObject().put("error", e.message ?: e.javaClass.simpleName).toString()
        }
    }

    /** "granted", or "prompt" when the learner has not been asked or said no.
     *  Android does not say which of those two it is without a prompt. */
    @JavascriptInterface
    fun permission(): String =
        if (ReminderScheduler.notificationsAllowed(ctx)) "granted" else "prompt"

    @JavascriptInterface
    fun requestPermission() {
        if (allowed()) askPermission()
    }

    /** `{id: "delivered" | "blocked" | "missed"}` for reminders this phone
     *  has dealt with. */
    @JavascriptInterface
    fun outcomes(): String = if (allowed()) ReminderStore.outcomes(ctx).toString() else "{}"
}
