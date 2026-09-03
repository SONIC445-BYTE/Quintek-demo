package com.quintek.app

import android.os.Bundle
import android.text.InputType
import android.view.Menu
import android.view.MenuItem
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.Toast
import androidx.appcompat.app.AlertDialog

/**
 * The benchmark console, reached by long-pressing the launcher icon.
 *
 * Keeps its action bar, unlike the student screen: this one is entered from
 * outside the app and needs an obvious way back. The backend setting lives
 * here rather than on the learner's screen, because pointing the app at a
 * benchmark API is an operator's job, not a student's.
 */
class AdminActivity : WebScreenActivity() {

    override val screen: Screen by lazy { Screen.fromName(intent.getStringExtra(Screen.EXTRA)) }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        title = screen.title
        supportActionBar?.setDisplayHomeAsUpEnabled(true)
    }

    override fun onCreateOptionsMenu(menu: Menu): Boolean {
        menuInflater.inflate(R.menu.console, menu)
        return true
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean = when (item.itemId) {
        R.id.action_backend -> { promptForBackend(); true }
        else -> super.onOptionsItemSelected(item)
    }

    override fun onSupportNavigateUp(): Boolean {
        onBackPressedDispatcher.onBackPressed()
        return true
    }

    private fun promptForBackend() {
        val input = EditText(this).apply {
            inputType = InputType.TYPE_TEXT_VARIATION_URI
            hint = "http://192.168.1.10:8500"
            setText(Settings.backendUrl(this@AdminActivity) ?: "")
            setSingleLine()
        }
        val pad = (24 * resources.displayMetrics.density).toInt()
        val holder = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad / 2, pad, 0)
            addView(input)
        }

        AlertDialog.Builder(this)
            .setTitle(R.string.backend_title)
            .setMessage(R.string.backend_help)
            .setView(holder)
            .setPositiveButton(R.string.save) { _, _ ->
                val value = input.text.toString().trim()
                if (value.isNotEmpty() && !Settings.looksReachable(value)) {
                    Toast.makeText(this, R.string.backend_needs_scheme, Toast.LENGTH_LONG).show()
                    return@setPositiveButton
                }
                Settings.setBackendUrl(this, value)
                // The backend is injected while the document loads, so an
                // already-rendered page keeps whatever it started with.
                webView.reload()
            }
            .setNeutralButton(R.string.use_demo_data) { _, _ ->
                Settings.setBackendUrl(this, null)
                webView.reload()
            }
            .setNegativeButton(android.R.string.cancel, null)
            .show()
    }
}
