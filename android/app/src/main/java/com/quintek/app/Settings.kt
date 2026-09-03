package com.quintek.app

import android.content.Context

/**
 * The one setting this app has: where the benchmark backend lives.
 *
 * Unset (the default) means every screen renders its built-in fixture data,
 * which is why the app is fully usable with no server and no network. Set it
 * to a reachable origin and the screens switch to live data.
 *
 * WHICH SERVER. The learner app needs `serve-student` (default port 8500):
 * that process serves the notebooks, questions, progress and billing routes
 * AND the `/ai/eval` and `/ai/benchmark/*` transparency routes, which is why
 * one origin is injected into both `__QUINTEK_API__` and
 * `__QUINTEK_STUDENT_API__`.
 *
 * Pass `--with-console` and that same origin also answers the benchmark
 * console's `/api/*` and operator `/ai/*` routes, so ONE setting serves both
 * screens. It is opt-in because those are operator routes and the origin a
 * learner's phone points at should not carry them by default.
 *
 * `serve-analytics` (port 8420) remains a separate, independently runnable
 * server for operators. It answers the console routes and returns 404 for
 * every learner route, so pointing the student app at it leaves notebooks,
 * questions, progress and billing dead while only the transparency screen
 * loads. This help text used to name it, which was exactly that mistake.
 *
 * Bind it where the phone can reach it -- `--host 0.0.0.0`, not the default
 * loopback -- and use the machine's LAN address.
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
