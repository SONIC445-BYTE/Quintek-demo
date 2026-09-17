# Android — manual end-to-end test

**Status of the build:** `app-debug.apk` (20.1 MB) builds and is signed with
the debug key, so it installs. It has **never been installed or run** — no
device or emulator exists in the build environment.

**What the ✓ marks mean.** Several rows below are now verified by an automated
test that drives the real client module against a real server over a socket
(`tests/frontend/revision_loop_live.test.mjs`). Those are marked
**✓ auto-verified** and you do not need to re-check them by hand — but they
test the CLIENT AND SERVER, not the phone. A row marked ✓ can still fail on a
device through a WebView difference, a network policy, or a rendering fault.
Treat ✓ as "the logic is right" and ☐ as "nobody has checked at all".

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
| I | **Answering a question** — use a fixture/seeded question | Attempt records; colour (RED/ORANGE/GREEN) assigned. **Note the order changed:** the app now asks how well you knew it BEFORE showing the answer. That is forced — `POST /attempts` requires the colour and attempts are immutable — and is the better order, because a judgement collected after the answer is already contaminated by it. | **✓ auto-verified** — attempt records, colour required |
| J | **Answer reveal timing** | The correct answer is NOT in the payload before you answer. Verified automatically: `/revision/next` carries no `correct_index`, `correct_answer` or `rationale`, and the test fails if any appears. Mutation-tested by making the server leak it. **Note `GET /questions/<id>` DOES return `correct_index`** — legitimately, it serves the question bank — so the revision loop must never use it for a question being answered. | **✓ auto-verified** — mutation-tested |
| K | **Progress persistence** — answer, leave the screen, return | Progress is still there | ☐ |
| L | **Revision session** — start one | A real session starts, serves questions from `/revision/next`, records attempts and returns a summary. Ranking by unseen-first is not separately auto-verified. | **✓ auto-verified** — session runs end to end |
| M | **App restart** — force stop, reopen | Session and progress survive. Backend URL is remembered | ☐ |
| N | **Backend outage** — stop the server, use the app | Clear "cannot reach backend" message. **Not** a silent fall back to fixture data presented as live | ☐ |
| O | **Billing** — open usage/plans | Real allowance figures. With no gateway configured, checkout refuses with a clear reason; everything else works | ☐ |
| P | **Admin login** — long-press the launcher icon → Benchmark console | Opens. Without an admin token the operator routes return **404, not 403** — by design, so the surface is not advertised | ☐ |
| Q | **Admin / report panels** | Run reports render. With no runs, they say so rather than showing zeros that look like results | ☐ |
| R | **AI transparency / powering** | Reports the honest state: nothing promoted, nothing routed, generation refusing | ☐ |
| S | **Production model refusal** | `/health` reports `generation: no_qualified_model` and `status: ok`. **`no_qualified_model` is healthy** — the platform must not restart over it | ☐ |
| T | **No fake data as live data** | Sweep every screen with the backend connected. Anything still showing built-in samples is a defect | **CLOSED for the screens named below · still worth a device sweep** |

**Item T is closed in code (2026-09-17).** Every screen `APP_BEHAVIOUR.md`
§2.6–2.8 named as FIXTURE now reads live data or says why it cannot:

| Screen | Now reads |
|---|---|
| Progress — mastery split | `/progress` `colour_counts` and `due_count` |
| Progress — 12-week heatmap | `/progress` `activity`, keyed by date |
| Progress — per-concept recall | `/concepts`, with an em dash where nothing was attempted |
| Notebook list | `/notebooks` |
| Question bank / studio | `/questions` |
| Demonstrations | `/demos` |
| Reminder settings | `/settings/notifications` + its delivery log |
| Revision dashboard | `/revision/dashboard`, and the strategy picker now offers the six names the server accepts |

**Two things this pass found that the sweep was supposed to find:**

1. **The app did not parse.** `frontend/PG Revision.dc.html` declared `const
   live` twice in one scope. That is a SyntaxError, so the component class was
   never constructed and **every screen rendered blank** — on the built
   bundle, in the android assets and in `app-debug.apk`. It shipped that way
   from the revision-loop commit onward. `tests/frontend/component_parses.test.mjs`
   now parses every design source, every built bundle and every android asset,
   and fails on all three with the bug reintroduced.

2. **The strategy picker was inert.** It offered `WEAKNESS_FIRST`,
   `WEAK_PLUS_SECTION` and `COVERAGE`. The server accepts
   `adaptive/red/orange/green/due/unseen` and rejects the rest with 422 —
   and `startRevision` passed the literal `'adaptive'` regardless, so choosing
   a strategy moved a highlight and changed nothing.

**What a device sweep is still for.** Everything above is verified by tests
that drive the real client against a real server, and by `vm`-parsing the
shipped bundles. None of it is a phone. A WebView difference, a layout that
clips, a control that cannot be tapped — those still need item T walked on
hardware, and nothing in this repository can close that.


## Two extra checks worth doing

| # | Test | Expected |
|---|---|---|
| U | **Release build refuses cleartext** — install `app-release-unsigned.apk` (after signing) and point it at `http://…` | The request fails. This is ADR-022 working |
| V | **Persistence survives a redeploy** — with the backend on Postgres, restart the server process and reopen the app | Account, notebooks and progress are all still there. This is the entire point of ADR-020 |

## What this pass closed, and what it did not

| Closed | |
|---|---|
| The revision loop | serves real questions, records real attempts, returns a real summary |
| `needs_review` | reaches the reveal and renders as a banner above the passage reference |
| `chunk_confidence`, `source_locator` | render in the provenance block |
| Report path | a live question can be reported; the report freezes its provenance |
| Concept graph | fetches `/graph`, uses the server's `cross_subject` |
| Concept detail, notebook view | fetch on screen entry |
| Scope statement | fetches `/scope` |

| Still open | Why |
|---|---|
| A device run | Nothing in this repository has touched a phone |
| Notification DELIVERY | No sender is configured, so a test send honestly reports "not sent". The settings, the schedule and the log are real; the transport is not built |
| The report queue has no operator | ADR-026. The routes work; nobody reads them on a schedule |
| The 28-item corpus audit | ADR-026. Needs a clinician; at least one "clean" label is known wrong |

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
