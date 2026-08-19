package com.quintek.app

/**
 * The screens shipped inside the APK.
 *
 * Each `asset` is a single self-contained HTML file produced by
 * `tools_build_standalone.py`, with React, Babel and the Quintek API modules
 * already inlined. They render with no network access; a backend, when one is
 * configured, only replaces fixture data with live data.
 */
enum class Screen(
    val asset: String,
    val title: String,
    val subtitle: String,
    val wideLayout: Boolean,
) {
    STUDENT(
        asset = "pg-revision.html",
        title = "PG Revision",
        subtitle = "The student app — today's queue, self-grading, knowledge gaps",
        wideLayout = false,
    ),
    ADMIN(
        asset = "quintek-admin.html",
        title = "Benchmark Console",
        subtitle = "Runs, scorecards, integrity, leaderboard, gate registry",
        wideLayout = true,
    ),
    HARNESS(
        asset = "quintek-harness.html",
        title = "Harness",
        subtitle = "Run status and track-level progress",
        wideLayout = false,
    ),
    AUDIT(
        asset = "quintek-audit.html",
        title = "Implementation Audit",
        subtitle = "What exists, what is only described",
        wideLayout = false,
    );

    val url: String get() = "file:///android_asset/$asset"

    companion object {
        const val EXTRA = "com.quintek.app.SCREEN"

        fun fromName(name: String?): Screen =
            entries.firstOrNull { it.name == name } ?: STUDENT
    }
}
