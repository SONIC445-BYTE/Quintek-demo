package com.quintek.app

import android.content.Intent
import android.os.Bundle
import android.text.InputType
import android.view.Menu
import android.view.MenuItem
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import com.quintek.app.databinding.ActivityLauncherBinding
import com.quintek.app.databinding.ItemScreenBinding

/**
 * Picks which screen to open, and carries the single global setting.
 *
 * The four screens are separate products sharing one shell -- a student app
 * and three engineering consoles -- so a launcher is more honest than
 * bottom-tab navigation, which would imply they belong to one flow.
 */
class LauncherActivity : AppCompatActivity() {

    private lateinit var binding: ActivityLauncherBinding

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityLauncherBinding.inflate(layoutInflater)
        setContentView(binding.root)

        Screen.entries.forEach { screen ->
            val row = ItemScreenBinding.inflate(layoutInflater, binding.screenList, false)
            row.title.text = screen.title
            row.subtitle.text = screen.subtitle
            row.root.setOnClickListener {
                startActivity(
                    Intent(this, WebActivity::class.java)
                        .putExtra(Screen.EXTRA, screen.name)
                )
            }
            binding.screenList.addView(row.root)
        }
    }

    override fun onResume() {
        super.onResume()
        refreshBackendLabel()
    }

    private fun refreshBackendLabel() {
        val backend = Settings.backendUrl(this)
        binding.backendState.text = if (backend == null) {
            getString(R.string.backend_unset)
        } else {
            getString(R.string.backend_set, backend)
        }
    }

    override fun onCreateOptionsMenu(menu: Menu): Boolean {
        menuInflater.inflate(R.menu.launcher, menu)
        return true
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean =
        if (item.itemId == R.id.action_backend) {
            promptForBackend()
            true
        } else {
            super.onOptionsItemSelected(item)
        }

    private fun promptForBackend() {
        val input = EditText(this).apply {
            inputType = InputType.TYPE_TEXT_VARIATION_URI
            hint = "http://192.168.1.10:8420"
            setText(Settings.backendUrl(this@LauncherActivity) ?: "")
            setSingleLine()
        }
        val padding = (24 * resources.displayMetrics.density).toInt()
        val holder = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(padding, padding / 2, padding, 0)
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
                refreshBackendLabel()
            }
            .setNeutralButton(R.string.use_demo_data) { _, _ ->
                Settings.setBackendUrl(this, null)
                refreshBackendLabel()
            }
            .setNegativeButton(android.R.string.cancel, null)
            .show()
    }
}
