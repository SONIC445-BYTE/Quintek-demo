package com.quintek.app

import android.content.Context

/**
 * The one setting this app has: where the benchmark backend lives.
 *
 * Unset (the default) means every screen renders its built-in fixture data,
 * which is why the app is fully usable with no server and no network. Set it
 * to a reachable origin -- typically a laptop on the same Wi-Fi running
 * `python -m benchmark.cli serve-analytics` -- and the reliability, leaderboard
 * and run screens switch to live data.
 */
object Settings {

    private const val PREFS = "quintek"
    private const val KEY_BACKEND = "backend_url"

    fun backendUrl(context: Context): String? =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getString(KEY_BACKEND, null)
            ?.trim()
            ?.trimEnd('/')
            ?.takeIf { it.isNotEmpty() }

    fun setBackendUrl(context: Context, value: String?) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit()
            .apply {
                val cleaned = value?.trim()?.trimEnd('/')
                if (cleaned.isNullOrEmpty()) remove(KEY_BACKEND) else putString(KEY_BACKEND, cleaned)
            }
            .apply()
    }

    /**
     * True for something that could plausibly be reached. Deliberately not a
     * strict URL validator -- the point is to catch a missing scheme, which is
     * the mistake that produces a silent failure inside the WebView.
     */
    fun looksReachable(value: String): Boolean =
        value.startsWith("http://") || value.startsWith("https://")
}
