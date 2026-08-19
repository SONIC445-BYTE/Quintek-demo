package com.quintek.app

import android.annotation.SuppressLint
import android.os.Bundle
import android.view.ViewGroup
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity
import java.io.ByteArrayInputStream
import java.io.InputStream
import java.io.SequenceInputStream
import java.util.Collections

/**
 * Hosts one Quintek screen in a WebView.
 *
 * The screens are ordinary web pages, so the interesting work here is the two
 * places Android has to meet them:
 *
 *  * **Backend injection.** `quintek-eval-api.js` reads
 *    `window.__QUINTEK_API__` at module-evaluation time and falls back to
 *    built-in fixtures when it is unset. Injecting after `onPageFinished`
 *    would be far too late, so the document request itself is intercepted and
 *    a one-line `<script>` prepended to the byte stream. The asset is still
 *    streamed rather than read into memory -- each bundle is around 5 MB.
 *
 *  * **Back navigation.** The screens are single-page apps that push their own
 *    history, so the system back gesture should walk that history first and
 *    only leave the activity once there is nothing left to go back to.
 */
class WebActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private lateinit var screen: Screen

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        screen = Screen.fromName(intent.getStringExtra(Screen.EXTRA))
        title = screen.title
        supportActionBar?.setDisplayHomeAsUpEnabled(true)

        webView = WebView(this).apply {
            layoutParams = ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT,
            )
        }
        setContentView(webView)

        with(webView.settings) {
            javaScriptEnabled = true
            domStorageEnabled = true
            // The bundles are one file with everything inlined; nothing is
            // loaded from the filesystem or another origin, so the file-access
            // escape hatches stay off.
            allowFileAccess = false
            allowContentAccess = false
            cacheMode = WebSettings.LOAD_NO_CACHE
            mediaPlaybackRequiresUserGesture = false

            if (screen.wideLayout) {
                // The console is a 1440px desktop design. Let it lay out at
                // its intended width and zoom out to fit, rather than
                // reflowing into an unreadable column.
                useWideViewPort = true
                loadWithOverviewMode = true
                builtInZoomControls = true
                displayZoomControls = false
                setSupportZoom(true)
            }
        }

        webView.webViewClient = object : WebViewClient() {
            override fun shouldInterceptRequest(
                view: WebView,
                request: WebResourceRequest,
            ): WebResourceResponse? {
                val url = request.url.toString()
                if (!request.isForMainFrame || url != screen.url) return null
                return try {
                    val asset: InputStream = assets.open(screen.asset)
                    val backend = Settings.backendUrl(this@WebActivity)
                    val stream = if (backend.isNullOrBlank()) {
                        asset
                    } else {
                        val prefix = """<script>window.__QUINTEK_API__ = ${quote(backend)};</script>"""
                        SequenceInputStream(
                            Collections.enumeration(
                                listOf(ByteArrayInputStream(prefix.toByteArray()), asset)
                            )
                        )
                    }
                    WebResourceResponse("text/html", "utf-8", stream)
                } catch (e: Exception) {
                    // Returning null hands the request back to the WebView,
                    // which will surface its own load error rather than
                    // showing a blank screen with no explanation.
                    null
                }
            }
        }

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (webView.canGoBack()) webView.goBack() else finish()
            }
        })

        if (savedInstanceState == null) {
            webView.loadUrl(screen.url)
        } else {
            webView.restoreState(savedInstanceState)
        }
    }

    /** JSON-safe string literal for the injected script. */
    private fun quote(value: String): String =
        "\"" + value.replace("\\", "\\\\").replace("\"", "\\\"").replace("<", "\\u003c") + "\""

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        webView.saveState(outState)
    }

    override fun onSupportNavigateUp(): Boolean {
        onBackPressedDispatcher.onBackPressed()
        return true
    }

    override fun onDestroy() {
        webView.destroy()
        super.onDestroy()
    }
}
