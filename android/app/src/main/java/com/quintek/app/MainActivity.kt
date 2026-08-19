package com.quintek.app

/**
 * What a learner opens: the PG Revision app, full screen, immediately.
 *
 * Nothing stands in front of it -- no picker, no title bar, no menu button. A
 * student has no reason to choose between their revision app and an
 * engineering console on launch, and the web screen already draws its own
 * header and bottom tabs, so a native chrome above them would just be a second
 * one competing for the same job.
 *
 * The console is reached by long-pressing the launcher icon (see
 * res/xml/shortcuts.xml), which keeps it one gesture away for whoever runs the
 * benchmark and invisible to everyone else.
 */
class MainActivity : WebScreenActivity() {
    override val screen = Screen.STUDENT
}
