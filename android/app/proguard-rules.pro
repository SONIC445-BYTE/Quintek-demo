# The UI is HTML in assets and there is almost no Kotlin to shrink, so release
# builds keep minification off (see app/build.gradle.kts). These rules exist so
# that turning it on later does not silently break the WebView bridge.
-keepclassmembers class * {
    @android.webkit.JavascriptInterface <methods>;
}
-keep class com.quintek.app.Screen { *; }
