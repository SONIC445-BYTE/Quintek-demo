# What the app actually does, screen by screen

Every behaviour in the shipped app, traced from source. Nothing here is
inferred from a design document or from what a screen appears to do.

The single most important fact, stated once and then assumed throughout:

> **The student app has no backend.** It contains zero `fetch` calls, zero
> `localStorage`, zero `sessionStorage`, zero `indexedDB`, and no
> `<input type="file">`. Its only network call is one dynamic import of
> `quintek-eval-api.js`, used solely to paint the reliability screen. Nothing a
> learner does is uploaded, stored, or sent anywhere. Closing the app discards
> everything.

Verified by grep over `frontend/PG Revision.dc.html`:

| Primitive | Occurrences |
| --- | --- |
| `fetch(` | 0 |
| `localStorage` / `sessionStorage` / `indexedDB` | 0 |
| `XMLHttpRequest` | 0 |
| `<input type="file">` | 0 |
| `import('./quintek-eval-api.js')` | 1 (reliability screen only) |

**This is still true of the shipped `.dc.html` as of the eighth pass, and it is
now true for a different reason.** A real backend exists — `student/`, thirteen
phases of it, 388 tests — but the design file has not been rewired to call it.
So the two statements to keep separate are:

- *The engine is not built.* — **No longer true.** Accounts, ingestion, concept
  resolution, generation, validation, attempts, gap tracking and spaced
  repetition are implemented and tested in `student/`. Reminders are stored and
  edited on the server and delivered by the Android app on the phone (§2.8).
- *The screens the learner touches are still simulated.* — **Still true.** Every
  interaction in the sections below runs on in-file constants. Wiring the
  screens to `student/api.py` is real remaining work, not a configuration flag.

The one exception is the reliability/benchmark screen, which does call a
backend, and now can call the learner backend directly (`GET /ai/eval` and
`GET /ai/benchmark*` on `student/server.py`) rather than needing the admin
console's server.

---

## 1. Legend

Each behaviour is marked with what actually backs it:

| Mark | Meaning |
| --- | --- |
| **REAL** | Does what it appears to do, with real logic or real data. |
| **FIXTURE** | Renders hardcoded data. The interaction works; the content is canned. |
| **SIMULATED** | A timer or a boolean imitates work that never happens. |
| **ABSENT** | The control exists but is wired to nothing meaningful. |

---

## 2. The student app — `PG Revision.dc.html`

### 2.1 Navigation — REAL

A single `route` string in component state. `go(route)` sets it. Bottom tabs
(Today / Weak / Revise / Graph / More) and every in-screen link work, including
back-navigation and deep links into a concept or a source. No router, no URLs,
no history beyond the WebView's own.

### 2.2 Answering a question — REAL interaction, FIXTURE content

The loop a learner runs is genuinely implemented:

```
pick(i)      choose an option        -> state.picked, blocked after submit
submit()     reveal                  -> state.submitted
judge(col)   YOU choose R/O/G        -> state.judgement, resets gaps
toggleGap(g) tag specific gaps       -> state.gaps
next()       push attempt, advance   -> this.attempts.push({...}); qi + 1
```

Two properties of the product's design are honoured in code:

- **The system never picks your colour.** `judge` is only ever called from a
  button press. Nothing infers red/orange/green from correctness.
- **The answer is not revealed before submission.** `pick` is a no-op once
  `submitted` is true, and the reveal block is gated on it.

The questions themselves come from a hardcoded `QUESTIONS` array. Attempts are
pushed to `this.attempts`, a plain instance array — **not** state, **not**
persisted, and cleared by `restart()`, `startMade()` and `startQueued()`.

### 2.3 Uploading a source — SIMULATED

This is the one you spotted. All five source-type buttons (PDF, image, link,
video, text) call the same handler:

```js
addSource = () => {
  if (this.state.extracting) return;
  this.setState({ extracting: true, extractPage: 1, extraCount: 0, ... });
  this._ext = setInterval(() => { /* page 1 -> 14, reveal NEW_CONCEPTS */ }, 620);
};
```

It opens no file picker, reads no file, uploads nothing. It starts a 620 ms
timer that counts pages 1→14 and progressively reveals entries from a
hardcoded `NEW_CONCEPTS` array, then stops. The page-by-page progress is the
animation, not a report of work.

So "select document doesn't fire" is accurate, and the cause is that there is
no file input to fire — not a permission, an intent, or a WebView problem.
(The Android shell now implements `onShowFileChooser` regardless, because a
WebView silently ignores file inputs without it; that is groundwork, not a fix
for this.)

### 2.4 Generating questions — SIMULATED

```js
generate  = () => this.setState({ generated: true });
runStudio = () => this.setState({ studioOut: true, studioAct: null });
```

Both flip a boolean that reveals pre-written questions from the same fixture
array. **No AI is called. No prompt is built. No model sees anything.** The
Make-questions screen's count, families, difficulty and demonstration pickers
all set state that changes what is displayed, and none of them reach a model.

`studioAction(a)` records accept / edit / reject into state. Nothing is stored.

### 2.5 Concept graph — REAL physics, FIXTURE nodes

`initGraph()` runs a genuine force-directed simulation on
`requestAnimationFrame` — real repulsion, spring and centring forces. The nodes
and edges it lays out are hardcoded. Tapping a node re-centres for real.

### 2.6 Revision queue and scheduling — REAL

**Was FIXTURE. Wired 2026-09-17.**

The dashboard's R/O/G counts, open-gap count and recommended question count
come from `/revision/dashboard`. `setCount`, `clampQ` and the custom-count
field set state, and the chosen count is threaded into the session
(`sessionCount`, from all three entry points).

The selection strategy is now real, and was the more interesting half. It
offered `WEAKNESS_FIRST`, `WEAK_PLUS_SECTION` and `COVERAGE`. The server's
`RevisionEngine.STRATEGIES` is `adaptive/red/orange/green/due/unseen` and
rejects anything else with 422 — and `startRevision` passed the literal
`'adaptive'` whatever was picked. So the control set state that nothing read,
against names that do not exist. It now offers the six real ones, describes
what each does in the server's own order of preference, and passes the
learner's choice through.

Still not built: SM-2 intervals are computed server-side in `revision_state`,
but nothing on this screen explains or exposes the interval itself.

### 2.7 Weak / gaps / progress — REAL

**Gap lists were wired earlier. The progress screen was wired 2026-09-17.**

Gap lists and the weak filter read `/gaps` through `learnerView()`. The mastery
split reads `colour_counts` and `due_count` from `/progress`; per-concept
recall strength reads `/concepts`; the 12-week heatmap reads `/progress`
`activity`.

**Where gaps come from.** Only from the learner: after a Red or Orange
answer they type what they did not know, and it is saved against that answer
with `POST /attempts/<id>/gaps`. A Red answer with nothing named does not
appear on the weak list. Until 2026-09-23 the live app could not create a gap
at all — and could not record an answer either, because the colour buttons
sent a value the server refuses (ADR-032). There is still no control to
resolve a gap (NOT_BUILT).

Three specifics worth keeping, because each replaced something that looked
right:

* **The heatmap is keyed by DATE.** `activity` is sparse and newest-first, so
  a positional layout paints three real study days as three adjacent squares
  at the wrong end of the grid and presents that as twelve weeks of history.
  `activityGrid` in `quintek-student-api.js` does the placement and is
  mutation-tested against exactly that implementation.
* **A concept with no attempts has no percentage.** Not 0%. `masteryPercent`
  returns `null` and the screen renders an em dash. A genuine 0-of-4 still
  shows 0%.
* **The streak is counted, not asserted.** It was the string "41-day streak".
  One day is not a streak, so `studyStreak` returning 0 or 1 falls back to the
  attempt count, which is a fact either way.

### 2.8 Reminders — REAL; delivered by the phone (Android app), not by the server

**Rewritten 2026-09-23 (ADR-031).** The single daily "trigger" setting this
section used to describe is gone, not patched. It answered "when should the
app nag me every day", which is not what a learner asked for.

**What a reminder is.** A learner writes a label in their own words — "revise
patho", "revise micro" — and picks a date and a time. They can have any
number; each is separate. At that moment the system hands the label back,
exactly as written. A reminder is not linked to a concept, a colour, or
anything being due, and it does not start a revision session.

**What the screen does** (Settings → Reminders, live against
`GET/POST /reminders`, `GET/PUT/DELETE /reminders/<id>`):

* **Create** — label, date, time. The time is in the device's timezone, which
  the form states ("Times are in Asia/Kolkata, this device's timezone") rather
  than hides. Stored as the local date, time and IANA zone, plus the UTC
  instant computed once.
* **List** — the learner's own reminders in the order they fire, each with its
  words (rendered with newlines and spaces kept), its date, time and zone, and
  its state: *Scheduled*, *Sent*, *Not sent — reason*, or *Cancelled*.
* **Edit** — text, date or time, while it is still scheduled. An edit keeps the
  zone the reminder was set in, even if the device has moved.
* **Cancel** — while scheduled. The row stays on the list marked cancelled.
* **Refusals are shown with the server's reason** and the draft is kept: an
  empty label, one over 200 characters, a time already past, and a local time
  the clocks skip (spring forward) or repeat (fall back). The last two are
  refused rather than resolved, because picking one of two instants would be
  the app deciding when the learner meant.

**Who delivers (decided 2026-09-24, ADR-031): the phone.** In the Android
app, opening Reminders hands the phone every pending, future reminder;
Android's AlarmManager holds each one and a notification shows the label,
verbatim, at its time (inexact — possibly a few minutes late in Doze).
Alarms are re-armed after a reboot. The phone asks for notification permission
from this screen, not at launch, and says so plainly when it is off. Each row
shows what the phone did: *Shown on this phone*, *Not shown — notifications
were off*, or *Time passed — this phone did not show it* for one it never
synced. A reminder only fires on a phone that has opened Reminders since it
was added or changed; that limitation is accepted for this phase.

**What does not happen.**

* **In a browser, nothing delivers a reminder**, and the screen says so.
* **The server delivers nothing.** `ReminderService.fire` still stops at an
  unset sender, and `python -m benchmark.cli notify` is scheduled by nothing.
  Scheduling it without a sender would mark every due reminder failed.
* The phone does not report back, so the server's status for a reminder stays
  `pending` after the phone has shown it; the list shows the phone's record.
* The native half is verified only on a device (ANDROID_MANUAL_TEST W–AA).

**The retired tables.** `notification_prefs` and `notification_log` are kept
in the schema, unread and unwritten. On production they held six rows, all
defaults, and no log rows — nothing a learner wrote — so nothing was migrated.

### 2.9 Reliability / trust screen — REAL, and the only live path

```js
componentDidMount() {
  this.initGraph();
  import('./quintek-eval-api.js')
    .then((api) => this.setState({ evalApi: api, evalState: api.state }))
    .catch(() => this.setState({ evalState: 'error' }));
}
```

This is the **entire** network surface of the student app. With no backend
configured the module returns its built-in fixtures. With
`window.__QUINTEK_API__` set, it fetches `GET /ai/eval` and the screen renders
real benchmark results — overall score, per-track scores with sample size and
confidence interval, evaluation date, and current configuration. The screen
computes none of it; a missing figure renders as unavailable rather than as a
placeholder number.

---

## 3. The benchmark console — `Quintek Admin.dc.html`

Unlike the student app, this screen is genuinely wired to the backend.

| Screen | Source | Status |
| --- | --- | --- |
| Runs | `GET /api/runs` → `GET /api/runs/:id` per run | REAL |
| Scorecard | the hydrated `report.json` | REAL |
| Integrity | `report.integrity` | REAL |
| Gate registry | `GET /api/gates` | REAL |
| Leaderboard, candidate report, compare, failures, routing | `GET /ai/eval` | REAL where measured |
| Deployment / promotion | — | ABSENT: no promotion endpoint is called |

With no backend it falls back to fixtures, exactly like the student app. The
difference is that here the fallback is the exception rather than the whole
story.

---

## 4. What the benchmark engine does, and what it does not

The engine is real, tested (217 tests) and deterministic. It is also
**completely disconnected from the student app**, by design and in fact.

```
                    ┌──────────────────────────────┐
   THE ONLY LINK →  │  benchmark results           │
                    │  /ai/eval → reliability page │
                    └──────────────┬───────────────┘
                                   │  one direction only
   ┌───────────────────────────┐   ▼   ┌────────────────────────────┐
   │ STUDENT APP               │       │ BENCHMARK ENGINE           │
   │ fixtures + timers         │       │ real scoring, real gates   │
   │ attempts live in memory   │  ╳    │ evaluates candidate models │
   │ nothing persisted         │ never │ against a frozen corpus    │
   └───────────────────────────┘       └────────────────────────────┘
```

**A learner's attempts never reach the benchmark**, and must not: a student's
answers becoming benchmark gold would destroy the independence the whole
harness exists to protect.

**The benchmark does not check the questions a learner sees**, because those
questions are a hardcoded fixture array — no model produced them, so there is
nothing to evaluate.

What the engine actually does, when given a real corpus and a provider:

1. Validate a dataset (split isolation, duplicate ids, missing gold, holdout
   contamination). Refuses to score an invalid one.
2. Call a candidate model over each item via a provider adapter, recording
   every attempt, latency and token count.
3. Score deterministically per track.
4. Evaluate each gate against its pre-registered threshold, direction and
   minimum sample size.
5. Resolve one outcome, in fixed precedence: integrity → budget → safety →
   reliability → track gates → ceiling.
6. Write an immutable `report.json` and a rendered scorecard.

Four rules it enforces regardless of the numbers:

- Below the registered `min_n`, a gate reports `UNEVALUABLE` — never PASS,
  never FAIL.
- If integrity fails, `scores` is `null` — not zero, not partial.
- One confirmed critical medical error fails the run regardless of every other
  track.
- While the registry is `UNCALIBRATED`, the best attainable outcome is
  `NOT_VALID_FOR_PRODUCTION_PASS`.

---

## 5. Operating the benchmark, and who is needed

Running the engine is one person part-time. **Producing a result that means
anything is not**, and the bottleneck is people, not compute.

| Role | Needed for | Can it be skipped? |
| --- | --- | --- |
| Engineer / operator | Run the harness, register candidates, read scorecards, promote a candidate | No — but one person, part-time |
| Two qualified reviewers | Any rubric-scored or safety-adjudicated track | **No.** One reviewer means kappa cannot be computed at all, which caps every run at `NOT_VALID_FOR_PRODUCTION_PASS` |
| Senior adjudicator | Resolving reviewer disagreement on critical items | Only if you never disagree, which is not a plan |
| Corpus author(s) | Writing the frozen gold corpus | No — the corpus does not exist yet, and it is the largest single blocker |

**Minimum viable team for a result that can issue a PASS: three people** — one
operator, two qualified reviewers — plus a senior adjudicator available for
disagreements. Fewer than two reviewers is not a slower path to the same
answer; it is a different, permanently capped answer.

Day-to-day commands are in the operator guide (`--help` on each):

```
python -m benchmark.cli gates                     # read the thresholds first
python -m benchmark.cli validate <corpus.jsonl>   # refuses invalid data
python -m benchmark.cli preflight <corpus.jsonl>  # cost + best attainable outcome
python3 tools_seed_model_registry.py              # register candidates
python -m benchmark.cli serve-analytics           # serve the console's API
python3 tools_sensitivity_analysis.py             # how noise moves what students see
```

Read `preflight`'s ceiling before spending anything. If it already says
`NOT_VALID_FOR_PRODUCTION_PASS`, fix the reviewer configuration first — no
amount of model quality raises that ceiling afterwards.

---

## 6. Screens not in the app

| Screen | Why excluded |
| --- | --- |
| Harness | Three click handlers, all navigation. No fetch, no backend contact. A read-only view over hardcoded fixtures. |
| Implementation Audit | Zero click handlers, zero fetches. A static document. |

Neither controls any functionality. Both are still built for the browser by
`tools_build_standalone.py`.

---

## 6b. What a real model actually does — measured, not assumed

Everything above this section describes behaviour. This section describes
measurement, taken on 20 Aug 2026 against NVIDIA NIM with a supplied key. It
is here because the rest of this document would otherwise read as though the
AI half were as settled as the UI half, and it is not.

### The chain, end to end, on real output

`tools_alpha0.py` drives one real source through every stage. With
`meta/llama-3.1-70b-instruct` generating:

| Stage | What actually happened |
|---|---|
| Ingestion | 1,477-character screening passage → 1 chunk, processed |
| Concept extraction | 9 concepts, all correct: Sensitivity, Specificity, Predictive Value, Prevalence, True/False Positive, True/False Negative, Cut-off Value |
| Relationships | 8 typed edges, e.g. `Cut-off Value --[causes]--> Sensitivity` |
| Generation | One grounded, correct PG-entry MCQ with a rationale |
| Persistence | Stored with source id, chunk id, generating candidate |
| Artifacts | 11 files including the unmodified raw reply |

So the pipeline works, and the 70B's concept extraction is genuinely good.

### Validation does not work on the configuration tested

With `meta/llama-3.1-8b-instruct` as validator, across the 20-item
adversarial battery and a 10-item sound control arm:

| Figure | Value | What it means |
|---|---|---|
| Detection rate | 11/20 (55%) | Nearly half of deliberately broken questions were approved |
| Caught for the right reason | 4/11 | Most catches did not cite a check matching the planted defect |
| False-flag rate | 9/10 (90%) | Nine of ten SOUND questions were also rejected |
| `ungrounded` | 0/2 | It never noticed a question unanswerable from its own passage |

**A learner on this configuration would lose most of their good questions and
still be shown false ones.** The validator is not currently doing the job the
product's central promise depends on.

This does not mean validation is unimplementable — it means this model, with
this prompt, does not do it. That is now a measurement with a reproducible
script behind it (`tools_adversarial_run.py`) rather than an assumption, which
is the difference between a problem you can work on and one you cannot see.

### What this changes about the screens above

Nothing yet — the screens are still simulated. But it changes what the
transparency screen will have to say once they are wired. `/ai/benchmark`
already reports `evidence_backed: false` and states in plain language that a
model serving without a passing benchmark run is not evidence-backed. On this
configuration that warning is not a formality.

## 7. Honest summary of build state

| Area | State |
| --- | --- |
| Benchmark engine | Built, tested, deterministic |
| Benchmark API + console wiring | Built, live-verified |
| Student UI (all screens, navigation, R/O/G loop, graph physics) | Built |
| Reliability screen ← benchmark | Built, the one real link |
| Source ingestion, extraction, concept resolution | **Built and tested** in `student/`; the UI still simulates it |
| Question generation and validation | **Built and tested** in `student/`; never yet run against a live model |
| Spaced repetition, persistence, accounts | **Built and tested** in `student/`; the UI still simulates it |
| Reminders | **Built and tested**; delivered on the phone by the Android app, verified on a device only by the manual checklist; no server-side delivery (§2.8, ADR-031) |
| Learner-facing AI transparency (Quintek AI Benchmark) | **Built** — data layer, routes, and honest empty states |
| Benchmark → production promotion | **Built** — the gate is code, refusals are explained |
| The screens calling that backend | **Not done.** The `.dc.html` still runs on in-file constants |
| Android APK compiled | **Never** — `dl.google.com` returns 403 in this environment, so the Android SDK and Google Maven are unreachable. The Kotlin is correct by inspection only |
| Real corpus, calibrated thresholds, reviewer pool | Not built — needs people |

### What "built and tested" does and does not mean here

Every module named above is covered by tests that supply a **scripted** model.
That establishes the plumbing, the failure handling, and the invariants. It does
not establish that a real model writes good questions, or that validation
catches real bad ones. No candidate has a passing benchmark run, because the
corpus does not exist, so `AIEngine.resolve()` today reaches its third step — an
explicitly configured development candidate — or raises `NoEligibleModel`. Every
call made that way is stamped `development_override` in the execution log, and
the learner-facing transparency screen says so in plain language rather than
presenting it as an evaluated result.
