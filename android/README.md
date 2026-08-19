# Quintek — Android app

A native Android shell around the two Quintek screens. Each screen ships
inside the APK as a single self-contained HTML file, so the app installs and
runs with **no server and no network**. Pointing it at a benchmark backend is
optional and only swaps demo figures for live ones.

```
MainActivity       the learner's app -- launches straight into PG Revision
AdminActivity      the benchmark console + the backend setting
WebScreenActivity  shared WebView host (backend injection, file chooser, back)
Screens.kt         the two bundled screens and their asset names
Settings.kt        the one persisted setting: backend URL
```

**Opening the app lands a learner in PG Revision.** There is no picker in
front of it and no title bar over it -- the web screen draws its own header
and bottom tabs. The benchmark console is reached by **long-pressing the
launcher icon** (an app shortcut), which keeps it one gesture away for whoever
runs the benchmark and invisible to everyone else.

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
| PG Revision | `PG Revision.dc.html` | The student app. Designed for a phone. The launch destination. |
| Benchmark Console | `Quintek Admin.dc.html` | Runs, scorecards, integrity, gates. A 1440px desktop design — it loads zoomed out and pinch-zooms; a tablet or landscape is far more comfortable. |

The harness and audit screens are **not** in the app. Both were checked before
being dropped: the harness has three click handlers and all three are
navigation, the audit has none at all, and neither performs a fetch or touches
the backend. They are read-only views over hardcoded fixtures, so excluding
them costs no functionality and halves the APK. `tools_build_standalone.py`
still builds them for the browser.

The design files draw each screen inside a phone mockup sitting on a dark
backdrop, under a caption, with a painted-on status bar. That is right for
reviewing a design and wrong for running one, so the build strips it — see
`strip_device_frame` in the builder. Pass `--keep-frame` to preserve it.

## Connecting to a backend

Long-press the launcher icon → **Benchmark console**, then its ⋮ menu → **Backend**.
Leave it empty for demo data. The setting lives in the console rather than on the
learner's screen because pointing the app at a benchmark API is an operator's job.

The benchmark API has to be reachable from the phone, which means binding it to
the network rather than loopback and using the computer's LAN address:

```bash
python -m benchmark.cli serve-analytics --host 0.0.0.0 --port 8420
# then set the backend to http://<computer-lan-ip>:8420
```

`WebScreenActivity` injects `window.__QUINTEK_API__` by intercepting the document
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
- **APK is around 9 MB**, because two ~4.6 MB bundles ship inside it and each
  carries its own copy of React and Babel. Sharing one runtime between them is
  the remaining win if that matters.
- **No student-app backend at all.** The student screens contain zero `fetch`
  calls, zero `localStorage`, and no file input. Answering questions, grading
  yourself, tagging gaps, uploading a source and generating questions are all
  prototype interactions over hardcoded fixtures — `addSource` runs a timer
  that counts pages, and `generate` flips a boolean that reveals pre-written
  questions. Nothing is uploaded, stored or generated. That is a property of
  the product as built, not of this shell. See `docs/APP_BEHAVIOUR.md`.

- **File chooser is wired but unused.** `WebScreenActivity` implements
  `onShowFileChooser`, because an `<input type="file">` in a WebView is
  silently ignored without it. No screen has a file input yet, so this is
  groundwork for the first real upload control rather than something you can
  exercise today.
