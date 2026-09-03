# Quintek Android — End-to-End Production Readiness Audit

Read-only reconstruction plus live probing of both backends and the full
learner journey. Every finding below was reproduced against running servers,
not inferred from reading code.

Severity: **A** production blocker · **B** functional bug · **C** documented
limitation · **D** cosmetic · **E** intentional V1 boundary / correct behaviour.

---

## B-1 — In-app setup instructions name the wrong backend

| | |
|---|---|
| STATUS | **VERIFIED FIXED** |
| SEVERITY | **B** |
| EVIDENCE | `strings.xml:8` `backend_help` says *"python -m benchmark.cli serve-analytics --host 0.0.0.0 --port 8420"*. `Settings.kt` KDoc repeats it. Probed live: against `serve-analytics` :8420 → `/notebooks` `/progress` `/questions` `/demos` `/billing/me/entitlements` all **404**. Against `serve-student` :8500 → all **401** (route exists, needs auth). |
| IMPACT | An operator who follows the on-screen instruction gets a student app whose notebooks, question bank, progress and billing are all dead. Only `/ai/eval` resolves. This is the same class of failure the `WebScreenActivity` comment records having fixed on the injection side. |
| DECISION | Fix the guidance. Do not change the injection: pointing both globals at one origin is correct for the learner app. |
| FIX | Applied. `strings.xml` `backend_help` now names `serve-student --host 0.0.0.0 --port 8500` and states plainly that `serve-analytics` does not serve the learner routes. `Settings.kt` carries the same distinction, including why one origin is injected into both globals. `AdminActivity`'s example hint moved 8420 → 8500. |
| TEST | Probed before the fix: learner routes 404 on :8420, 401 on :8500. Backend suite after the fix: 1283 passed, 4 skipped. The strings are user-facing copy with no automated test; the probe above is the evidence. |

## B-2 — One backend setting cannot serve both shipped screens

| | |
|---|---|
| STATUS | **VERIFIED FIXED** — gateway implemented using the repository's existing mount pattern |
| SEVERITY | **B** |
| EVIDENCE | STUDENT bundle imports `quintek-student-api.js` (learner routes, `/ai/benchmark/powering`), `quintek-eval-api.js` (`/ai/eval`), `quintek-billing-api.js` (`/billing/*`) — all served by `student/server.py`. ADMIN bundle imports `quintek-report-api.js` → `/api/runs`, `/api/gates`, `/api/preflight`, `/api/datasets/*`, served **only** by `analytics_api.py`. Probed: `/api/gates` → 200 on :8420, 401-then-unrouted on :8500; `/notebooks` → 404 on :8420. |
| IMPACT | `Settings.backendUrl` is one SharedPreferences value read by both activities, so whichever server is configured, the other screen's live panels cannot resolve. No false data results — see E-4 — but the two screens cannot both be live at once. |
| DECISION | Option (a), because the repository already had the architecture for it. `billing/mount.py` establishes `owns(path)` + `handle(...)` with `student/server.py` dispatching to it, and `student/api.py` already re-serves `/ai/eval` and `/ai/benchmark/*` documented as *"so the app talks to one origin"*. The gateway finishes that job rather than inventing one. |
| FIX | `benchmark/analytics_mount.py` — `AnalyticsMount`, shaped like `BillingMount`, mounted into `student/server.py` behind `--with-console`. Ownership is **declared, not discovered**: `/api/*` plus every `/ai/*` route the learner API does not answer itself. No fallthrough-on-404, so a missing notebook can never be rerouted into the benchmark archive. Read-only: any non-GET returns 405, so promotion — the one analytics surface that writes — is unreachable from a phone origin. **Opt-in**, because mounting operator routes on the learner origin widens the security posture and a default must not do that quietly. |
| TEST | 37 new tests in `tests/test_analytics_mount.py`, including one that parses `student/api.py::_ai` and fails if the two ownership lists ever drift. Live, one origin, authenticated: `/capabilities` `/notebooks` `/ai/eval` `/api/runs` `/api/gates` `/ai/discovery` `/ai/routing/current` all **200**; `POST /api/runs` **405**. Default (no flag): console routes **404** authenticated — posture unchanged. Suite 1320 passed, 4 skipped. |

## C-1 — Cleartext traffic enabled app-wide

`AndroidManifest.xml:22` `usesCleartextTraffic="true"`, no `networkSecurityConfig`.
The manifest's own comment says to narrow it before any release outside
development. Correct for LAN development against `http://192.168.x.x`; must be
replaced with a scoped network-security-config for a public release.

## C-2 — AdminActivity is exported with no intent filter

`AndroidManifest.xml:50` `android:exported="true"`. Any installed app can
launch the benchmark console. It is read-only, holds no credential and shows
only what the configured backend returns, so impact is low — but it does not
need to be reachable from outside the app.

## C-3 — The asset guard checks existence, not freshness

`app/build.gradle.kts:checkQuintekAssets` fails the build when a bundle is
missing, with the exact command to run. It does not compare timestamps against
`frontend/`, so a stale bundle built from older sources passes — the failure
`tools_build_standalone.py` warns about ("keeps the phone from quietly running
an older screen than the browser").

## C-4 — The Android build cannot be executed in this environment

No Android SDK, `ANDROID_HOME` unset, no `android/local.properties`.
`./gradlew :app:checkQuintekAssets` fails resolving
`com.android.application:8.5.2`. The Gradle configuration is internally
coherent (AGP 8.5.2, compileSdk/targetSdk 34, minSdk 26, Kotlin JVM 17) but
**no APK has been produced or verified here**, and none is claimed.

## C-5 — Billing has no gateway credentials

`serve-student` prints at startup: `gateway=NONE`, *"no gateway credentials
configured -- checkout will refuse, everything else works"*. Announced at boot
rather than at checkout, which is the right place.

## C-6 — The scripted provider produces degenerate content

With `QUINTEK_DEV_CANDIDATE` set, ingestion succeeded and produced "concepts"
that are fragments of the prompt (`"Extract the medical concepts this passage
TEACHES"`, `"It"`), and one question whose stem is prompt text. This is exactly
what a scripted test double should produce. It proves the pipeline is wired and
demonstrates why a scripted provider must never be mistaken for a qualified
model. The server labels itself `provider: scripted (NOT a real model)` at
startup.

---

## E-1 — Missing admin bundle is caught at build time, not shipped

A fresh checkout has no `quintek-admin.html` (assets are gitignored build
output). `checkQuintekAssets` is wired to `preBuild` and fails with the exact
command to run. Running `tools_build_standalone.py` produced both bundles
(4.9 MB and 4.6 MB). Not a defect.

## E-2 — The learner journey stops at generation, correctly

Registration, login, capabilities, notebook creation and source upload all
work and persist. Ingestion then refuses:

    NoEligibleModel: no model is available for CONCEPT_EXTRACTION: nothing is
    promoted, no candidate is benchmark-eligible, and no development candidate
    is configured

and generation refuses to ground questions on a source that failed. This is
qualification enforcement holding **at the product boundary**, consistent with
Phase 0 being INCOMPLETE and zero candidates being in PRODUCTION. The
resolution ladder is `promoted → routed → development_override → refuse`, and
the chosen source is recorded with the execution.

## E-3 — Unvalidated questions are labelled, never presented as checked

Generation returned `validation: {approved 0, flagged 0, skipped 1}` because
the validator uses a **separate** configuration and did not silently reuse the
generator's model — independence held. The question is served with
`validation_status: "pending"`, the UI renders it as a badge, and the summary
line reads *"N not checked (no independent model was available)"*.

## E-4 — Fixtures are never substituted for a failed backend

`quintek-eval-api.js` uses fixtures **only when no backend was ever
configured**; a configured backend that fails yields emptiness plus
`isOutage`/`loadError`, explicitly because the fixtures name real vendors and
assert "93.4% PASS". `quintek-student-api.js` ships no fixtures at all and
throws `BackendError`. Verified by reading both modules and by probing a dead
port.

## E-5 — The app cannot promote a model

`student/api.py:_ai` returns **405 "the AI benchmark screen is read-only"** for
any non-GET. There is no promotion path from the Android client.

## E-6 — No JavaScript bridge is exposed

`addJavascriptInterface` is not used anywhere. `allowFileAccess=false`,
`allowContentAccess=false`. The only native↔web coupling is a one-line
`<script>` prepended to the document stream to set the backend origin.

## E-7 — Credentials are not present in the repository

Scanned for NVIDIA and Razorpay key shapes and generic secret assignments. The
only `nvapi-` literals are test fixtures, several of which are **assertions
that the key never leaks** (`assert "nvapi-" not in written`). Secrets are read
from `RAZORPAY_KEY_SECRET` / `RAZORPAY_WEBHOOK_SECRET` / `NVIDIA_API_KEY` by
name. The live key value appears in no committed file.
