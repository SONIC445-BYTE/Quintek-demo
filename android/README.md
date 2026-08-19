# Quintek — Android app

A native Android shell around the four Quintek screens. Each screen ships
inside the APK as a single self-contained HTML file, so the app installs and
runs with **no server and no network**. Pointing it at a benchmark backend is
optional and only swaps demo figures for live ones.

```
LauncherActivity   pick a screen; set the backend URL
WebActivity        hosts one screen in a WebView
Screens.kt         the four bundled screens and their asset names
Settings.kt        the one persisted setting: backend URL
```

## Build it

You need Android Studio (Ladybug or newer) or a command-line Android SDK.
JDK 17+ is required; the Gradle wrapper pins Gradle 8.7.

```bash
# 1. Build the web bundles first -- they are generated, not checked in.
cd ..                              # repository root
python3 tools_build_standalone.py

# 2. Build the app
cd android
./gradlew assembleDebug            # -> app/build/outputs/apk/debug/app-debug.apk
```

Then either open `android/` in Android Studio and press Run, or install
directly over ADB:

```bash
./gradlew installDebug
# or
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

Step 1 is not optional. `app/build.gradle.kts` registers a `checkQuintekAssets`
task that fails the build with a clear message if the bundles are missing,
rather than producing an APK whose screens are blank.

**To sideload without a computer**: build the APK once, put it somewhere your
phone can reach, and allow "install unknown apps" for whichever app you use to
open it. Nothing here is on the Play Store.

## What each screen is

| Screen | Source | Notes |
| --- | --- | --- |
| PG Revision | `PG Revision.dc.html` | The student app. Designed for a phone. |
| Benchmark Console | `Quintek Admin.dc.html` | Runs, scorecards, integrity, gates. A 1440px desktop design — it loads zoomed out and pinch-zooms; a tablet or landscape is far more comfortable. |
| Harness | `Quintek Harness.dc.html` | Run status and track progress. |
| Implementation Audit | `Quintek Audit.dc.html` | What exists versus what is only described. |

## Connecting to a backend

Menu → **Backend** on the launcher screen. Leave it empty for demo data.

The benchmark API has to be reachable from the phone, which means binding it to
the network rather than loopback and using the computer's LAN address:

```bash
python -m benchmark.cli serve-analytics --host 0.0.0.0 --port 8420
# then set the backend to http://<computer-lan-ip>:8420
```

`WebActivity` injects `window.__QUINTEK_API__` by intercepting the document
request and prepending a one-line `<script>` to the asset stream. That timing
matters: `quintek-eval-api.js` reads the global while its module body is
evaluating, so anything injected after `onPageFinished` would arrive too late
and the screen would silently keep showing fixtures.

`usesCleartextTraffic` is enabled in the manifest because a local harness is
plain `http://`. Replace it with a scoped `network-security-config` before any
release build that leaves development.

## Why a WebView and not native screens

The screens are the product's actual design artefacts, authored as design
components and iterated on outside this repository. Reimplementing them in
Compose would fork the design: every change would need doing twice, and the two
would drift. The WebView keeps one source of truth. If the student app later
needs real offline storage, camera capture or background sync, those are the
points worth going native for — not layout.

## Known limits

- **minSdk 26** (Android 8.0). Chosen so the launcher icon can be a pure vector
  adaptive icon instead of five densities of PNG.
- **APK is large** — roughly 19 MB, because four ~4.6 MB bundles ship inside it.
  Each one carries its own copy of React and Babel. Dropping a screen from
  `Screens.kt` and `TARGETS` in the builder is the easy win if that matters.
- **No native student-app backend.** Answering questions, gap tagging and the
  revision queue are prototype-only in the web layer; attempts live in memory
  and are not persisted anywhere. That is a property of the product as built,
  not of this shell.
