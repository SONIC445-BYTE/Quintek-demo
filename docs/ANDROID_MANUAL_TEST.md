# Android — manual end-to-end test

**Status of the build:** `app-debug.apk` (15.4 MB) builds and is signed with
the debug key, so it installs. It has **never been installed or run** — no
device or emulator exists in the build environment. Everything below is
therefore a plan, not a result.

## Before you start

```bash
# 1. Build the debug APK (the release APK denies cleartext HTTP by design)
cd android
echo "sdk.dir=$ANDROID_HOME" > local.properties
./gradlew :app:assembleDebug
# -> app/build/outputs/apk/debug/app-debug.apk

# 2. Install
adb install -r app/build/outputs/apk/debug/app-debug.apk

# 3. Run the backend where the phone can reach it
cd ..
python3 -m benchmark.cli serve-student --host 0.0.0.0 --port 8500 --with-console
```

Then in the app's settings, set the backend to `http://<your-LAN-ip>:8500`.
Not `localhost` — that is the phone.

**Use the DEBUG build for LAN testing.** The release APK refuses cleartext
HTTP entirely (ADR-022), so `http://192.168.x.x` will fail on it. That is the
control working, not a bug.

## What "pass" means here

Two failure modes matter more than the rest, so check for them everywhere:

* **Fixture data shown as live data.** Every screen renders built-in sample
  data when no backend is configured. A screen that still shows sample data
  *after* the backend is set has silently failed, and it looks like success.
* **A refusal reported as a fault.** Generation refusing because no model is
  qualified is the CORRECT state. If the app presents that as an error, or
  worse invents an answer, that is the failure.

## Checklist

| # | Test | Expected | Result |
|---|---|---|---|
| A | **Fresh launch** — install, open, no backend set | Lands in PG Revision. Screens render with fixture data, clearly not presented as a live account | ☐ |
| B | **Registration** — create an account | 200; a user id comes back; no crash on a weak password (server enforces ≥8 chars) | ☐ |
| C | **Login / logout** — log out, log back in | Token issued, session restored, logout revokes. Logging in on a second device does not invalidate the first | ☐ |
| D | **Notebook creation** — create one, reopen the screen | It persists and is listed. It belongs to your account only | ☐ |
| E | **Ingestion** — upload a PDF or paste text | Source moves `uploaded → chunking → processing → extracted`. A failure shows the real reason, not a generic error | ☐ |
| F | **Concept extraction** — after ingestion | Concepts appear and are attributed to the source. The same concept across two notebooks is ONE concept, not two | ☐ |
| G | **Question generation** | **Expect a refusal.** No model is qualified, so the server returns 503 with `no_qualified_model`. The app must say so plainly. **An invented question here is the worst possible outcome** | ☐ |
| H | **Validation** | Not reachable while G refuses. Confirm the app says why rather than showing an empty screen | ☐ |
| I | **Answering a question** — use a fixture/seeded question | Attempt records; colour (RED/ORANGE/GREEN) assigned | ☐ |
| J | **Answer reveal timing** | The correct answer is NOT visible in the payload before you answer. Check the network response, not just the UI | ☐ |
| K | **Progress persistence** — answer, leave the screen, return | Progress is still there | ☐ |
| L | **Revision session** — start one | Unseen questions rank above ones you have already answered | ☐ |
| M | **App restart** — force stop, reopen | Session and progress survive. Backend URL is remembered | ☐ |
| N | **Backend outage** — stop the server, use the app | Clear "cannot reach backend" message. **Not** a silent fall back to fixture data presented as live | ☐ |
| O | **Billing** — open usage/plans | Real allowance figures. With no gateway configured, checkout refuses with a clear reason; everything else works | ☐ |
| P | **Admin login** — long-press the launcher icon → Benchmark console | Opens. Without an admin token the operator routes return **404, not 403** — by design, so the surface is not advertised | ☐ |
| Q | **Admin / report panels** | Run reports render. With no runs, they say so rather than showing zeros that look like results | ☐ |
| R | **AI transparency / powering** | Reports the honest state: nothing promoted, nothing routed, generation refusing | ☐ |
| S | **Production model refusal** | `/health` reports `generation: no_qualified_model` and `status: ok`. **`no_qualified_model` is healthy** — the platform must not restart over it | ☐ |
| T | **No fake data as live data** | Sweep every screen with the backend connected. Anything still showing built-in samples is a defect | ☐ |

## Two extra checks worth doing

| # | Test | Expected |
|---|---|---|
| U | **Release build refuses cleartext** — install `app-release-unsigned.apk` (after signing) and point it at `http://…` | The request fails. This is ADR-022 working |
| V | **Persistence survives a redeploy** — with the backend on Postgres, restart the server process and reopen the app | Account, notebooks and progress are all still there. This is the entire point of ADR-020 |

## Known gaps you will hit

* **The release APK is unsigned.** Signing needs a keystore; none has been
  created, and none should be invented. To sign one yourself:
  `keytool -genkey -v -keystore quintek.jks -keyalg RSA -keysize 2048 -validity 10000 -alias quintek`
  then add a `signingConfigs` block to `android/app/build.gradle.kts`. Keep the
  keystore and its password out of the repository.
* **G and H will refuse**, and that is correct. Do not configure
  `QUINTEK_DEV_CANDIDATE` to make them pass — it puts an unevaluated model in
  front of medical questions, and the production guard refuses to start with it
  set.
