package com.quintek.app

/**
 * The screens shipped inside the APK.
 *
 * Each `asset` is a single self-contained HTML file produced by
 * `tools_build_standalone.py`, with React, Babel and the Quintek API modules
 * already inlined, and the design files' phone-mockup chrome stripped. They
 * render with no network access; a backend, when one is configured, only
 * replaces fixture data with live data.
 *
 * Two screens, because they have two different audiences. STUDENT is the app
 * a learner opens. ADMIN is the engineering console and is reached only from
 * the overflow menu -- a learner has no reason to see benchmark internals, and
 * putting both behind one picker made every user choose between them on
 * launch.
 *
 * The harness and audit screens are deliberately not here. Both were checked
 * before removing them: the harness has three click handlers and all three are
 * navigation, the audit has none at all, and neither performs a fetch or
 * touches the backend. They are read-only views over hardcoded fixtures, so
 * dropping them costs no functionality and halves the APK.
 */
enum class Screen(
    val asset: String,
    val title: String,
    val wideLayout: Boolean,
) {
    STUDENT(
        asset = "pg-revision.html",
        title = "Quintek",
        wideLayout = false,
    ),
    ADMIN(
        asset = "quintek-admin.html",
        title = "Benchmark Console",
        wideLayout = true,
    );

    val url: String get() = "file:///android_asset/$asset"

    companion object {
        const val EXTRA = "com.quintek.app.SCREEN"

        fun fromName(name: String?): Screen =
            entries.firstOrNull { it.name == name } ?: STUDENT
    }
}
