package com.quintek.app

import android.annotation.SuppressLint
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.view.ViewGroup
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import java.io.ByteArrayInputStream
import java.io.InputStream
import java.io.SequenceInputStream
import java.util.Collections

/**
 * Shared WebView host. Subclasses pick which [Screen] to show.
 *
 * Three places Android has to meet the web layer:
 *
 *  * **Backend injection.** `quintek-eval-api.js` reads
 *    `window.__QUINTEK_API__` at module-evaluation time and falls back to
 *    built-in fixtures when it is unset. Injecting after `onPageFinished`
 *    would be far too late, so the document request itself is intercepted and
 *    a one-line `<script>` prepended to the byte stream. The asset is still
 *    streamed rather than read into memory -- each bundle is around 4.6 MB.
 *
 *  * **File chooser.** An `<input type="file">` in a WebView does nothing at
 *    all unless the host implements [WebChromeClient.onShowFileChooser]. There
 *    is no error and no callback -- the tap is simply swallowed, which is
 *    indistinguishable from a broken button. Nothing in the current screens
 *    uses a file input yet (source upload is still a simulated animation), but
 *    wiring it here means the first real upload control works instead of
 *    failing silently.
 *
 *  * **Back navigation.** The screens are single-page apps that push their own
 *    history, so the system back gesture walks that history first and only
 *    leaves the activity once there is nothing left to go back to.
 */
abstract class WebScreenActivity : AppCompatActivity() {

    protected lateinit var webView: WebView
    protected abstract val screen: Screen

    private var filePathCallback: ValueCallback<Array<Uri>>? = null

    private val fileChooser = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        val callback = filePathCallback ?: return@registerForActivityResult
        filePathCallback = null
        // The callback MUST be invoked on every path, including cancellation.
        // Leaving it unanswered wedges the input element: the WebView believes
        // a chooser is still open and ignores every later tap.
        callback.onReceiveValue(
            WebChromeClient.FileChooserParams.parseResult(result.resultCode, result.data)
        )
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

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
            // The bundles are one file with everything inlined; nothing loads
            // from the filesystem or another origin, so these stay off.
            allowFileAccess = false
            allowContentAccess = false
            cacheMode = WebSettings.LOAD_NO_CACHE
            mediaPlaybackRequiresUserGesture = false

            if (screen.wideLayout) {
                // The console is a 1440px desktop design. Let it lay out at its
                // intended width and zoom out to fit, rather than reflowing
                // into an unreadable column.
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
                    val backend = Settings.backendUrl(this@WebScreenActivity)
                    val stream = if (backend.isNullOrBlank()) {
                        asset
                    } else {
                        // Every JS module the bundles ship reads its own global
                        // name for the backend origin -- __QUINTEK_API__ for
                        // the benchmark/eval client, __QUINTEK_STUDENT_API__
                        // for the learner engine and billing clients. Both
                        // point at the SAME server: student/server.py serves
                        // /ai/*, the learner API and /billing/* on one port.
                        // Setting only one left the student screen's
                        // notebooks, generation and billing permanently
                        // "not configured" even with a correct URL saved --
                        // only the AI-transparency screen ever connected.
                        val prefix = """<script>
                            window.__QUINTEK_API__ = ${quote(backend)};
                            window.__QUINTEK_STUDENT_API__ = ${quote(backend)};
                        </script>""".trimIndent()
                        SequenceInputStream(
                            Collections.enumeration(
                                listOf(ByteArrayInputStream(prefix.toByteArray()), asset)
                            )
                        )
                    }
                    WebResourceResponse("text/html", "utf-8", stream)
                } catch (e: Exception) {
                    // Returning null hands the request back to the WebView,
                    // which surfaces its own load error rather than showing a
                    // blank screen with no explanation.
                    null
                }
            }
        }

        webView.webChromeClient = object : WebChromeClient() {
            override fun onShowFileChooser(
                view: WebView,
                callback: ValueCallback<Array<Uri>>,
                params: FileChooserParams,
            ): Boolean {
                filePathCallback?.onReceiveValue(null)   // abandon any stale request
                filePathCallback = callback
                return try {
                    fileChooser.launch(params.createIntent())
                    true
                } catch (e: Exception) {
                    filePathCallback = null
                    callback.onReceiveValue(null)
                    false
                }
            }
        }

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (webView.canGoBack()) webView.goBack() else finish()
            }
        })

        if (savedInstanceState == null) webView.loadUrl(screen.url)
        else webView.restoreState(savedInstanceState)
    }

    /** JSON-safe string literal for the injected script. */
    private fun quote(value: String): String =
        "\"" + value.replace("\\", "\\\\").replace("\"", "\\\"").replace("<", "\\u003c") + "\""

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        webView.saveState(outState)
    }

    override fun onDestroy() {
        webView.destroy()
        super.onDestroy()
    }

    protected fun openScreen(target: Screen) {
        startActivity(Intent(this, AdminActivity::class.java).putExtra(Screen.EXTRA, target.name))
    }
}
