# Implementation Status

Honest accounting of what is built, what is stubbed, and what cannot be built here.

This file was written by the original implementer and has since been independently verified
and extended by a second pass: every module below was read (not skimmed), every claim was
checked by actually running `python -m pytest tests/ -v` and the CLI (`validate`, `gates`,
`preflight`, `demo`) rather than trusting the docstrings, and two real gaps found during that
verification were closed rather than left silently unchecked. See `docs/V0_4_CHANGELOG.md`
item 1 for why "a green test suite" is not, by itself, sufficient evidence here.

## Built and tested (388 tests passing at the eighth pass; 52 when this table was written)

| Module | Status | Notes |
|---|---|---|
| `benchmark/stats.py` | complete | Wilson, Clopper-Pearson (exact, bisection on incomplete beta), bootstrap, cluster bootstrap, Cohen's kappa, weighted kappa. Validated against published reference values (e.g. Clopper-Pearson upper bound at n=200/500 matches the closed form `1-alpha**(1/n)` to 5 decimal places; Wilson 45/50 matches hand computation). |
| `benchmark/gates.py` | complete | Registry-driven. Five gate states. No hardcoded thresholds. |
| `benchmark/integrity.py` | complete | 13 preconditions, fail-closed. An unimplemented check reports FAILURE, never skip. |
| `benchmark/dataset.py` | complete | Schema, split isolation, safety-holdout distribution constraints. |
| `benchmark/scorers/deterministic.py` | complete | All Tier-0 scorers including GATE-C-MERGE and GATE-H-FAMILY. |
| `benchmark/reports/scorecard.py` | complete | Integrity suppression enforced in code. |
| `benchmark/runner.py` | complete | Preflight projection, budget, review ceiling, run storage, now wires the per-candidate retry ceiling from `configs/v0_4.yaml` into the provider before execution. |
| `benchmark/providers/` | complete | Abstraction plus retry/timeout: `BaseProvider.generate` retries up to `retry_policy.max_retries` on any exception (including a concrete provider's own timeout), passes `timeout_seconds` through to `_call` so the concrete client enforces it, and records `attempts` on every response so a report never hides how many tries a number cost. Scripted provider for harness testing, extended with a `fail_attempts` knob so the retry loop itself is exercised by tests, not just asserted to exist. |
| `benchmark/adjudication/` | complete | Was an empty stub package; now holds the Phase 3 human-review workflow: `ReviewQueue` (blind, independently-shuffled two-rater assignment with sentinel insertion), `SentinelMonitor` (drift detection against a reference bank), `SeniorAdjudicationQueue` (disagreement escalation that preserves both original labels, per `CRITICAL_MEDICAL_ERROR.md`), and `GoldChallengeLedger` (the full lifecycle in `GOLD_ERROR_PATHWAY.md`, append-only so a correction never rewrites a historical run). This is the workflow mechanism, not a substitute for real reviewers — see below. |
| `benchmark/cli.py` | complete | validate / gates / preflight / demo. |

## What the second pass found and fixed

1. **Retry/timeout handling did not exist.** `docs/MASTER_BUILD_PROMPT_V0_4.md` Phase 0 lists it
   explicitly; grepping the codebase for `retry`/`timeout` before this pass turned up only the
   *cost projection's* retry-overhead ratio, not an actual retry loop. Added `RetryPolicy` and
   rewrote `BaseProvider.generate` around it; added `tests/test_providers_retry.py` (6 tests,
   including one that drives a simulated provider through two failures before success and checks
   `attempts == 3`, and one confirming a provider error is never silently recorded as a wrong
   answer).
2. **`benchmark/adjudication/__init__.py` was empty.** The acceptance checklist calls for a
   blinded queue, sentinels, senior adjudication, and a gold challenge workflow; none of that
   code existed, only the kappa arithmetic it would feed. Added `queue.py` and
   `gold_challenge.py`; added `tests/test_adjudication.py` (17 tests) which also closes two gaps
   in the master prompt's own 8-scenario acceptance test that the existing `test_acceptance.py`
   did not cover: reviewer disagreement routing to a senior adjudication queue entry, and a gold
   challenge producing a frozen-item-plus-challenge record.

## Third pass: closed two more gaps surfaced by a direct "are all phases done?" audit

Re-checking against `docs/MASTER_BUILD_PROMPT_V0_4.md` phase-by-phase (not re-reading the
docstrings, grepping the actual code) found two more real holes, both closeable without API
keys, so they're closed rather than left as footnotes:

3. **`docs/INTEGRITY_CI.md` item 10, "injection battery smoke test", did not exist.** The
   synthetic corpus already distributes all 10 `PI-01`..`PI-10` attack families evenly (30 items
   each), but no code anywhere read `gold.attack_family` — no coverage check, no per-family
   breakdown, just one aggregate success rate across all 300 items, which can hide a single
   completely-broken family inside a passing average. Added a corpus-composition check in
   `dataset.py:validate` (structural, not a scoring gate — no threshold for this exists in the
   registry and none was invented) and a diagnostic-only `score_injection_attack_success_by_family`
   in `scorers/deterministic.py`. `tests/test_injection_battery.py` (7 tests) includes one that
   constructs a case where the aggregate rate reads fine but one family is at 100% attack
   success, and shows only the breakdown catches it.
4. **`docs/VARIANCE_PROTOCOL.md`'s actual protocol had no executing code.** Only the reliability
   gate *thresholds* (`GATE-REL-VARIANCE-*`) existed in the registry; nothing performed the DEV
   characterization (>=3 runs/item) or the official-run 10% sentinel re-run, and nothing computed
   the disagreement rates those gates check. Added `benchmark/variance.py`
   (`characterize`, `sentinel_rerun`, the three disagreement-rate calculators, and
   `reliability_from_variance`, which produces the exact dict shape `gates.evaluate_run` already
   consumes) plus an opt-in `Runner.run(..., run_sentinel_variance=True)` integration — off by
   default, since it is additional provider calls beyond what a caller's own `reliability` dict
   might already supply, and a caller running their own re-run harness must not have it silently
   overridden. `tests/test_variance.py` (17 tests) includes one that runs a stochastic provider
   3x/item and asserts real disagreement is observed, not just that the code path executes
   without error.

## Fourth pass: benchmark analytics data layer (student frontend contract + admin analytics)

A frontend implementation spec asked for two things beyond the harness itself: a student-facing
"AI Reliability" UI consuming a typed API contract (never a hardcoded score), and an admin analytics
data layer (leaderboard, comparison, failure analytics, routing analytics, historical runs) that
precomputes everything so the frontend is a pure visualization layer.

- **`benchmark/analytics.py`** — entities (`BenchmarkRun`, `CandidateBenchmarkResult`, `TrackResult`,
  `BenchmarkCaseResult`, `FailureRecord`, `CriticalErrorRecord`, `RoutingDecision`,
  `HumanReviewResult`), a read-only `RunArchive` over `runs/` supporting the candidate -> runs ->
  versions -> tracks traversal, a leaderboard with ranking explicitly separated from production
  eligibility, a two-candidate comparison, filterable failure analytics, an append-only
  `RoutingLog`, and `ai_overview`/`student_track_results`/`candidate_summary` producing the exact
  `AIOverview`/`TrackResult`/`CandidateSummary` shapes the frontend contract specifies. The
  registry's technical outcome states (PASS/CONDITIONAL/FAIL/NO_PASS_CAPABILITY_GAP/INCOMPLETE/
  INVALID_RUN/NOT_VALID_FOR_PRODUCTION_PASS/UNEVALUABLE) map to a student-facing pass/review/fail/
  unavailable vocabulary in one place (`RUN_STATUS_MAP`), tested for exhaustiveness against the live
  registry rather than hand-copied.
- **`benchmark/analytics_api.py`** — a reference-only read-only JSON API (`http.server`, stdlib,
  zero new dependencies) exposing `/api/candidates`, `/api/leaderboard`, `/api/ai-overview`,
  `/api/tracks`, `/api/compare`, `/api/failures`, `/api/routing`. Framed explicitly as a contract
  reference, not a prescribed production stack — the transport (`AnalyticsAPI.handle`) is kept
  separate from the stdlib HTTP wrapper so a real framework adapter can wrap the same core. Runnable
  via `python -m benchmark.cli serve-analytics`.

**A real bug, caught by its own test, not by re-reading the spec:** the first version of
`_ranking_score` averaged every measured track's raw estimate together to produce a leaderboard sort
key, including `GATE-SAFETY-CME`'s raw estimate -- which is a confirmed-CME **rate** (lower is
better), not an accuracy-like score. A candidate with 99% accuracy and a 2% confirmed-CME rate
averaged to 50.5%, which sorted *below* a safer candidate with a merely-good 91% accuracy and no
CME gate at all. That is the exact "aggregate that averages a failed/dangerous signal away" failure
mode `docs/SCORECARD_SPEC.md` exists to prevent on the official scorecard -- and it reappeared here,
in a code path that only ever claimed to be a display convenience. `test_ranking_never_implies_eligibility`
in `tests/test_analytics.py` failed on first run and caught it; the same defect existed a second time
in the grouped per-track score for the frontend's "Concept understanding" track (blending
`GATE-C-F1`, higher-is-better, with `GATE-C-MERGE`, an error rate) and was fixed the same way.
**Fixed:** every score aggregation in `analytics.py` now excludes `direction != "lower"` gates (error
rates, zero-tolerance safety gates) from any numeric average; their PASS/FAIL status still
participates in status roll-ups via worst-status-wins, which is direction-agnostic and correct.

**What this does NOT do:** `FailureRecord`/`CriticalErrorRecord`/`HumanReviewResult` are defined and
fully testable, but nothing in `runner.py` automatically populates them from a real run yet -- a
caller (or a future runner integration) constructs them from adjudication data. `RoutingDecision`
logging is likewise a library a production routing layer would call; the benchmark run itself
doesn't route production traffic, so it correctly has no opinion here.

## Fifth pass: NVIDIA NIM provider + Model Registry + deterministic Router + orchestration

The architecture requirement changed mid-session: from a fixed 2-4 model pool on Groq to "Quintek
maintains an evaluated pool of AI configurations, measures them against Quintek-specific tasks,
filters them through safety gates, and routes each task to the strongest eligible configuration" --
provider-agnostic, registry-driven, with NVIDIA NIM as the initial (not permanent) provider.

- **`benchmark/providers/nvidia.py`** — `NVIDIAProvider`, NVIDIA NIM's OpenAI-compatible
  `/v1/chat/completions` over `urllib.request` (stdlib, no new dependency). The API key is read
  from `NVIDIA_API_KEY` at call time only — never a constructor argument, never written to disk by
  this module. 13 tests, all against a mocked HTTP layer (auth header, request body, JSON-in-content
  parsing, 429 retry-then-succeed, retries-exhausted, connection failure, and a test asserting the
  key literally never appears in `.manifest()`'s output, since that dict is what ends up in
  `report.json`).
- **`benchmark/registry.py`** — `Registry` / `ModelCandidate`, JSON-file-backed, atomic writes.
  Candidate identity is derived from the same fields as `docs/CANDIDATE_DEFINITION.md` (provider +
  model + version + prompt version + retrieval version + config), so re-registering an identical
  configuration returns the same candidate rather than a duplicate. Lifecycle
  (`REGISTERED -> BENCHMARK_REQUIRED -> EVALUATING -> ELIGIBLE/FAILED -> PRODUCTION -> DEPRECATED`)
  is enforced by an explicit transition table, not convention — skipping straight to `PRODUCTION`,
  or leaving `FAILED`/`DEPRECATED`, raises. Only `ELIGIBLE`/`PRODUCTION` candidates are ever
  returned by `eligible_candidates()`. 14 tests.
- **`benchmark/tasks.py`** — `TaskType` enum (`SOURCE_PROCESSING` .. `REVISION_SELECTION`), each
  mapped to the registry gate IDs that measure fitness for it and the capabilities it requires — the
  join key between "what Quintek needs to do" and "what the benchmark measured."
- **`benchmark/router.py`** — `Router.select(task, policy, ...)`: capability filter -> safety filter
  (excludes any candidate whose latest run is not `production_eligible`, or whose task-relevant gate
  status is `fail`) -> score -> policy. Five policies (`QUALITY_FIRST` default,
  `COST_OPTIMIZED`/`LATENCY_OPTIMIZED` with graceful fallback to `QUALITY_FIRST` when no
  cost/latency hint is given, `BALANCED`, `EXPERIMENTAL` seeded-random for evidence collection). No
  step calls a model — `test_no_llm_call_anywhere_in_selection` asserts this structurally by
  scanning the module source. 11 tests, including the exact acceptance scenario from the
  architecture spec: three candidates, one scores highest on question generation but fails
  `GATE-SAFETY-CME`, and must be excluded from routing regardless of its raw score.
- **`benchmark/orchestration.py`** — `Orchestrator.generate(task, prompt, policy)`: routes, calls
  the provider, and on failure excludes that candidate and re-routes (`max_fallbacks`), recording
  every attempt — success, failure, or "no eligible candidate" — to an append-only `ExecutionLog`,
  never silently switching models. `CallLimiter` bounds concurrency (semaphore), call count, and
  token budget for *production* traffic, deliberately separate from `runner.Budget` (which scopes
  one benchmark *run*, a different lifetime). 10 tests, including one that actually blocks a second
  thread on a concurrency limit of 1 and unblocks it on release.
- **`benchmark/analytics_api.py` extended** — `/ai/reliability`, `/ai/candidates`,
  `/ai/candidates/<id>`, `/ai/candidates/<id>/tasks`, `/ai/benchmark`, `/ai/leaderboard` (overall or
  `?task=` for a task-specific ranking), `/ai/routing/current` (what's routed where, right now — "why
  did Quintek use this model"), `/ai/how-it-works` (static, and tested to never claim one model is
  globally best). Added alongside the existing `/api/*` routes, not replacing them —
  `test_original_api_routes_still_work_unchanged` guards that.
- **`benchmark/analytics.py` extended** — `normalized_track_score`/`task_leaderboard`, shared by the
  router and the new leaderboard endpoint (moved out of `router.py` during this pass so the
  direction-normalization logic exists in exactly one place).

**End-to-end verified, not just unit-tested:** a full `Registry -> Router -> Orchestrator ->
ExecutionLog/RoutingLog` run against a real (scripted) provider, and a real `serve-analytics`
process hit over an actual socket with `curl` (`/ai/how-it-works`, `/ai/candidates`,
`/ai/benchmark`), both by hand outside the test suite, both working.

**What's still not verified: a live call to NVIDIA NIM itself.** This sandbox's egress policy
rejects the CONNECT to `integrate.api.nvidia.com:443` outright (403 at the gateway, confirmed via
`$HTTPS_PROXY/__agentproxy/status`, not a bad key or a transient failure) — per that proxy's own
guidance, a policy denial is reported, not retried or routed around. The adapter is fully unit-
tested against a mocked HTTP layer and ready to run unchanged the moment the environment's network
policy allows the host, or when run outside this sandbox.

## Sixth pass: a real, persistent, committed Model Registry

Everything in the "Fifth pass" registry work existed only inside test fixtures (`tmp_path`-scoped,
never written to the repo). This pass seeds a real one:

- **`tools_seed_model_registry.py`** — reads five general-purpose-appropriate model IDs straight
  from a live `GET /v1/models` call against `integrate.api.nvidia.com` (not guessed), and registers
  them into `configs/model_registry.json`, now committed. Four general-purpose candidates across
  distinct model families/lineages (`meta/llama-3.3-70b-instruct`, `openai/gpt-oss-120b`,
  `google/gemma-3-12b-it`, `nvidia/llama-3.1-nemotron-70b-instruct`) plus one medical-domain
  specialist (`writer/palmyra-med-70b`, deliberately registered with a **narrower** capability set —
  no evidence supports claiming it's good at long-context source processing or concept extraction,
  so the seed doesn't claim that). NVIDIA hosts 100+ endpoints; embedding, vision, translation, and
  safety-moderation models (`nvidia/nv-embed-v1`, `meta/llama-3.2-11b-vision-instruct`,
  `meta/llama-guard-4-12b`, etc.) were deliberately excluded — see the script's module docstring for
  why lumping those in as ordinary candidates would be dishonest.
- **Every seeded candidate is `REGISTERED` and nothing more.** None are `ELIGIBLE`/`PRODUCTION` --
  that requires a real benchmark run against real gold data, which doesn't exist (see "The corpus
  does not exist" in README.md). `test_no_seeded_candidate_is_fabricated_eligible` in
  `tests/test_model_registry_seed.py` asserts this directly, and a further test points a real
  `Router` at the real seed with an empty run archive and confirms it correctly returns "no eligible
  candidate" for all nine task types — verified behavior, not an assumption.
- **`benchmark/cli.py serve-analytics`** now defaults `--registry` to this seed file when present,
  so the reference API works out of the box. Manually verified over a real socket: `/ai/benchmark`
  reports `{"REGISTERED": 5}` and zero leaderboard entries (no runs yet, correctly); `/ai/routing/current`
  reports `selected_candidate: null` for every task with an explicit reason, not a silent default.
- 9 new tests, including one confirming the seeded file isn't accidentally shadowed by the
  `registry.json` entry in `.gitignore` (different filename, `model_registry.json`, so this one stays
  committed while ad hoc local registries created during development stay ignored).

## Seventh pass: wiring the real UI to the backend, and the four defects that exposed

The engineering dashboard and student app were supplied as built design files (now vendored in
`frontend/`). Wiring them surfaced a mismatch that neither side could see alone, plus three real
backend bugs.

**The mismatch.** The admin console consumed nine `/api/*` run-centric endpoints
(`/api/runs`, `/api/gates`, `/api/datasets`, `/api/preflight`). The backend served fourteen
*candidate*-centric ones (`/api/leaderboard`, `/ai/candidates`, ...). Zero paths overlapped — every
one of the nine returned 404. Both halves were correct and neither was reachable from the other.
Underneath, the contract already matched exactly: `gates.py:GateResult.as_dict()` emits the same
fourteen fields `quintek-report-api.js` expects, and the suppression invariant held on both sides.
`benchmark/runs_api.py` adds the nine missing routes; `benchmark/eval_api.py` adds the
candidate-centric published-evaluation view (`/ai/eval`).

**Defect 1 — `build_report` dropped five fields `Runner._meta()` had already computed.**
`timestamp`, `ceiling_reason`, `review_mode`, `reviewer_count` and `kappa_computable` were
calculated and then discarded, so the console rendered blank dates and could not say *why* a run
could not pass. A report that states an outcome owes the reader the conditions that produced it.

**Defect 2 — the safety block was never emitted at all.** `GATE-SAFETY-CME` appeared only as one
row among the track gates. The gate that overrides every other gate had no first-class
representation, so the console's safety panel rendered empty for every run — silently, because the
UI guards on `run.safety ?`. `RunOutcome.safety` now carries confirmed events, the exact one-sided
upper bound and the registered limit, and is `null` (never zero) on a suppressed run.

**Defect 3 — a metric on a 0-4 scale was averaged with proportions.** `GATE-E-RUBRIC` is a mean
rubric rating out of 4. `_ranking_score` averaged its raw 3.6 with ~0.97 accuracies and returned
**1.35** — a score above the top of its own scale, which rendered as *134.6%*, with a confidence
interval whose upper bound was *360* on a 0-100 axis. The registry had declared `scale_max: 4.0`
all along; nothing read it. This is the same defect class as the direction-mixing bug fixed in the
fourth pass — that fix stopped `lower` and `upper` gates being blended, but nobody noticed one gate
was not a proportion at all. `scale_max` now flows from registry → `GateResult` → `report.json` →
`TrackResult`, and every aggregation divides by it.

Found by rendering the number, not by reading the code — the same way the fourth pass's bug was
found. An aggregate that is never displayed is never checked.

**Defect 4 — a track composed entirely of error-rate gates scored blank.** The direction filter
that correctly protects mixed groups left "Question validation" (only `GATE-F-FALSEAPPROVE`) with
nothing to average, so the one screen whose purpose is disclosure showed an empty cell beside a
perfectly good measurement. Mixed groups still drop error-rate gates — inverting a *passing* error
rate into a mixed average inflates it, which `test_grouped_score_excludes_error_rate_gates...`
correctly forbids. Only the all-error-rate case falls back to inversion, where no mixing hazard
exists.

**Honest nulls, kept honest.** Three fields the UI renders are not things this repository measures:
`costPer1k` (a commercial fact — needs `configs/model_costs.json`, shipped unpriced),
`latencyMs` (real orchestrator executions, `null` if a candidate has never run), and the
`invalid`/`unsafe`/`failedValidation`/`humanReview` breakdown (the gate engine records a pass count
and an `n`, not a failure taxonomy). All report `null`, never `0`. Zero asserts "we looked and found
none"; null says "not measured".

`POST /api/runs` returns **501**, not `{"status": "queued"}`. Starting a run needs a provider,
credentials and a corpus; answering "queued" for work that will never execute is the same class of
dishonesty as reporting an unmeasured score. It accepts a `run_launcher` when one exists.

Verified end-to-end by importing the actual `quintek-report-api.js` and `quintek-eval-api.js`
modules in Node against a live server and running the UI's own `assertSuppression()` over real
`report.json` output — not by asserting against a fixture of what the backend was assumed to emit.
217 tests passing.

## Eighth pass: the student engine, promotion, and the transparency screen

The thirteen product phases were built in order, each with tests before moving on. This
section records what is real, and — more usefully — what is real *code* whose behaviour has
still never been observed against a live model.

### What is built and tested

| Module | What it does | Tests |
|---|---|---|
| `student/schema.sql` | The product schema. Three invariants from `QUINTEK_LOGIC.md` §3 are enforced in SQL rather than in application code: concepts are global (`UNIQUE(normalized_name)`), attempts are immutable (triggers that `RAISE(ABORT)` on UPDATE and DELETE), and a question links to many concepts across notebooks. | via `test_student_db.py` |
| `student/db.py` | Connection management and auth. `PRAGMA foreign_keys = ON` per connection, because SQLite disables them by default and does so silently. PBKDF2-HMAC-SHA256 at 240,000 rounds with a per-user salt; an unknown email still costs a hash, so login timing does not enumerate users. | 17 |
| `student/ingestion.py` | PDF and text extraction, chunking on sentence boundaries with locators carried through merges. `pypdf` is optional and its absence is reported, not swallowed. | 13 |
| `student/concepts.py` | Concept resolution by exact match and explicit alias only. **No fuzzy auto-merge**: a false merge silently destroys a learner's distinction between two concepts and cannot be undone from the data; a false split is visible and fixable. `merge_candidates()` exists but only advises. | 15 |
| `student/ai.py` | Resolution order: promoted deployment → deterministic router → explicitly-configured development candidate → `NoEligibleModel`. Every call is stamped with which of those it came from. | 13 |
| `student/generation.py` | Question generation grounded in retrieved passages, with full provenance stored per question. Malformed model output is dropped, not repaired into something plausible. | 13 |
| `student/validation.py` | Eight named checks. The validator must be a different candidate than the generator, and never sees the generator's rationale. The verdict is derived from the checks, not read off the model's own summary line. | via generation/e2e |
| `student/knowledge.py` | R/O/G colour derivation over a five-attempt window, gap recording, and SM-2 scheduling graded on the learner's own colour rather than on raw correctness. | via `test_student_revision.py` |
| `student/revision.py` | Priority scoring with named weights and a reason list per concept, and the eight-step adaptive selection order. `next_question` never returns the answer key. | 21 |
| `student/notifications.py` | One learner-chosen time, computed in the learner's own zone via `zoneinfo`. Nothing here reschedules or "optimises" that time. Every firing is logged so "did it actually send" is answerable. | via e2e |
| `student/api.py` | The learner API surface, transport-independent. | via e2e + server |
| `student/server.py` | Stdlib HTTP transport. | 10 |
| `student/transparency.py` | The learner-facing Quintek AI Benchmark screen's data layer. | 30 |
| `benchmark/promotion_api.py` | The benchmark → production gate as an admin surface. | 22 |

### What is real code but has never run against a live model

This is the honest part. Every module above is tested, but the tests supply scripted
providers. The following behaviours have therefore never been *observed*, only specified:

- **Whether generated questions are actually good.** `QuestionGenerator` is exercised with a
  scripted model that returns well-formed JSON. What a real model produces from a real medical
  passage — and whether the grounding rule holds — is unmeasured.
- **Whether validation catches anything.** The eight checks are tested against hand-built
  inputs. No real model has ever rejected a real bad question here.
- **Whether concept extraction produces a usable graph.** Tested with scripted extractions.
- **End-to-end latency and cost.** Unknown. The one live NVIDIA measurement in this repo is
  from the fifth pass, and a later attempt at a full benchmark run timed out at 180s per call
  against endpoint capacity (recorded in the seventh pass).

All of it depends on the same root cause as the rest of this table: no candidate has a passing
benchmark run, because the corpus does not exist. Today `AIEngine.resolve()` reaches step 3 —
an explicitly-configured development candidate — or raises. That is visible in every execution
record rather than buried in a config file, which is the point.

### Two defects found by building the transparency screen

1. **The frontend invented data during an outage.** Both `quintek-eval-api.js` and
   `quintek-report-api.js` fell back to fixtures whenever a *configured* backend failed. The
   fixtures name real vendors and assert real scores, so a learner opening the transparency
   screen during an outage was told, specifically and falsely, which AI was marking their work.
   On the admin side, `getRun('abc')` returned a different run's fixture labelled `abc`. Fixed:
   fixtures are now used only when no backend was ever configured; a configured backend that
   fails is reported as an outage. `tests/frontend/data_origin.test.mjs`.
2. **`schema.sql` could not add a column to an existing database.** `CREATE TABLE IF NOT
   EXISTS` is inert against a database that already exists, so a column added to the schema
   after deployment would never appear and the first query touching it would fail on live data.
   Added an explicit additive-migration list in `student/db.py`, verified against a hand-built
   old-shape database.

## Ninth pass: REAL_MODEL_ALPHA — the first run against an actual model

An API key was supplied, so for the first time in this repository the pipeline
was driven end to end by a real model rather than a scripted double.
`tools_alpha0.py` records the run; `alpha0_runs/` holds the artifacts.

**Configuration.** Generator `meta/llama-3.1-70b-instruct`, validator
`meta/llama-3.1-8b-instruct`, both NVIDIA NIM. The script refuses to run when
generator and validator are the same configuration.

### What worked

| Stage | Result |
|---|---|
| Ingestion | A 1,477-character passage on screening chunked and processed, 1/1 chunks |
| Concept extraction | **9 concepts**, all correct and correctly described: Sensitivity, Specificity, Predictive Value, Prevalence, True/False Positive, True/False Negative, Cut-off Value |
| Relationship extraction | 8 typed edges, e.g. `Cut-off Value --[causes]--> Sensitivity`, `Sensitivity --[measured_by]--> False Negative` |
| Generation | A grounded, correct PG-entry MCQ with a 132-character rationale |
| Persistence | Stored with source_id, chunk_id and generating candidate |
| Artifact capture | 11 artifacts including the unmodified 1,223-character raw reply |
| API → UI | `GET /questions` served the stored question |

The generated question was: *"A test with a high sensitivity is likely to have
which of the following characteristics?"* keyed to *"A low number of false
negatives"* — correct, answerable from the supplied passage, and testing the
concept it claims to test.

**9 of 11 acceptance criteria met.**

### The two failures, which are the valuable part

**1. The validator approved a question that was deliberately false.**

The run offers the validator a question asserting that sensitivity rises with
prevalence — which the supplied passage explicitly contradicts. The validator
returned all eight checks `true`, `issues: []`, `verdict: approved`, in 89
completion tokens.

This is the single most important measured finding in this repository. Every
claim the product makes about questions being checked before a learner sees
them rests on the validator, and on this item, with this model, it did not
check. It is exactly what the adversarial battery exists to quantify, and it
was found by running the thing rather than by reading it.

`tools_adversarial_run.py` then ran the full 20-item, 10-defect-class battery
against the same validator, with a 10-item sound control arm. The result:

| Figure | Value |
|---|---|
| Detection rate | **11/20 (55%)** |
| Caught citing a check matching the planted defect | **4/11** |
| False-flag rate on sound questions | **9/10 (90%)** |

Per defect class (n=2 each): hallucinated_fact 2/2, out_of_syllabus 2/2,
wrong_key 1/2, two_correct 1/2, ambiguous_stem 1/2, hallucinated_reference
1/2, poor_reasoning 1/2, giveaway 1/2, trivial 1/2, **ungrounded 0/2**.

**`meta/llama-3.1-8b-instruct` is not fit to serve as Quintek's validator.**
It rejects nine out of ten sound questions while letting nearly half the
broken ones through, and only four of its eleven catches cite a check
corresponding to the actual defect — so most of the remainder are consistent
with guessing. A learner on this configuration would lose most of their good
questions and still be shown false ones.

The `ungrounded 0/2` line deserves its own sentence. Quintek's central promise
is that questions come from the learner's own source. This validator did not
once notice a question that could not be answered from the passage it was
given.

None of this is a defect in the harness — it is the harness working. The
figures are indicative, not gate-grade: n=2 per class carries an interval wide
enough to include most values of interest, and the report says so in its own
`interpretation` field rather than leaving it to the reader.

What it establishes is the claim that mattered: **generation is not
acceptance, and on this configuration acceptance is broken.**

### The 70B validator is dramatically better, and dramatically slower

The obvious next measurement — does a larger validator do better? — was run
immediately. `meta/llama-3.1-70b-instruct` as validator, same battery:

**Partial result: 10/10 adversarial items caught before the run stalled.**

Every defect class it reached was flagged: wrong_key 2/2, two_correct 2/2,
ambiguous_stem 2/2, hallucinated_fact 2/2, hallucinated_reference 2/2.
Against the 8B's 11/20 overall, that is not a marginal difference.

The run did not finish. Per-item latency was 7s, 15s, 30s, 36s, 65s, 103s and
then an indefinite stall on item 11; the process was killed. This matches the
seventh pass's finding and the `meta/llama-3.3-70b-instruct` timeouts: the 70B
endpoints on this account have severe, intermittent latency spikes.

So the two findings are in tension, and both are real:

| | llama-3.1-8b | llama-3.1-70b |
|---|---|---|
| Detection (adversarial) | 11/20 (55%) | 10/10 before stalling |
| False-flag (sound controls) | 9/10 (90%) | not reached |
| Per-item latency | 0.9–4.6s | 7–103s, then indefinite |

**The validator that works is too slow to serve interactively on this
endpoint; the validator that is fast enough does not work.** That is the
problem the project actually has, stated in numbers. It is solvable — a
different host, a smaller specialised validator, a cheap first-pass filter, or
moving validation off the interactive path — but it could not be worked on
while it was invisible.

One caveat stated rather than glossed: the 70B run's false-flag rate is
**unmeasured**, because the control arm runs after the adversarial items. So
"the 70B is the answer" is not yet supportable — the control arm is exactly
where a flag-everything validator gets caught, and the 8B failed precisely
there. Completing that run is the next concrete step.

**2. `No development_override` cannot pass yet, and this is structural.**

Every call in the run is stamped `development_override` because no candidate
has a passing benchmark run to be promoted from. That needs the expert corpus.
The criterion is left failing rather than redefined, because the promotion
gate refusing to promote an unbenchmarked model is the gate working.

### Judge independence is only partially satisfied on this account

`docs/JUDGE_INDEPENDENCE.md` Tier 2 asks for a judge from a **different model
family**. Of 102 models the account lists, only `meta/llama-3.1-8b-instruct`
and `meta/llama-3.1-70b-instruct` actually served `/v1/chat/completions`;
everything else tested returned 404, including `writer/palmyra-med-70b` and
every Mistral and Nemotron id. So generator and validator are different
models but the **same family**, and the independence claim is weaker than the
protocol asks for. Recorded here rather than quietly accepted.

`meta/llama-3.3-70b-instruct` consistently exceeded even a 120-second timeout
on this account, consistent with the seventh pass's finding.

## Tenth pass: provider status, failure classes, and the two-layer router

### Provider registry, as of the last probe

```
PROVIDERS

nvidia        adapter: yes   mock: yes   live: yes   status: DEGRADED / SLOW
cerebras      adapter: yes   mock: yes   live: —     status: EGRESS_BLOCKED
openrouter    adapter: yes   mock: yes   live: —     status: EGRESS_BLOCKED
scripted      adapter: yes   mock: yes   live: yes   status: AVAILABLE
```

| Provider | Blocked host | Reason |
|---|---|---|
| Cerebras | `api.cerebras.ai:443` | Organization egress policy (403 on CONNECT) |
| OpenRouter | `openrouter.ai:443` | Organization egress policy (403 on CONNECT) |

**Neither has failed.** Both adapters are written and mock-tested; neither has
ever been reached from this environment. `live` stays `—` rather than becoming
`failed`, because "we could not test this" is not a test result. Running the
same code where those hosts are permitted changes the `live` column and
nothing else.

The three columns exist because collapsing them gets the answer wrong twice:
a correct, tested adapter behind a firewall reads as a broken provider, and a
reachable host with an untested adapter reads as a working one.

    ADAPTER WORKS  ≠  PROVIDER REACHABLE  ≠  PROVIDER HEALTHY

### Failure classes

`benchmark/provider_status.py` classifies a failure from observable evidence
and attaches a policy. Three failures that look identical to a `try/except`
demand opposite responses:

| Status | Retry? | Circuit | Counts against the model? |
|---|---|---|---|
| `EGRESS_BLOCKED` | never | opens, **no cooldown** | no |
| `AUTH_FAILED` | never | opens, no cooldown | no |
| `MODEL_UNAVAILABLE` | never | opens, no cooldown | no |
| `RATE_LIMITED` | back off | **stays closed** | no |
| `TIMEOUT` | once | opens, 60s | no |
| `DEGRADED` | once | opens, 120s | no |
| `INVALID_RESPONSE` | once | stays closed | **yes** |
| `UNKNOWN_ERROR` | once | opens, 60s | no |

Two of these rows are the point. A rate limit must **not** open the circuit —
the provider is healthy and we are being greedy; taking it out of rotation
punishes it for our request rate. An egress block must open the circuit with
**no cooldown**, because no amount of waiting makes a firewall go away, and a
60-second retry loop just re-learns the same fact forever.

`INVALID_RESPONSE` is the only class that counts against a model's quality. A
model is not worse because a firewall exists, a key is wrong, or a host is
busy. `HealthRegistry.health()` reports `attributable_success_rate` separately
from `success_rate` for exactly this reason, and returns `None` — not zero —
when every failure was environmental.

### Two-layer routing

**Layer 1 — can I use this at all?** Reachability, credentials, model
existence, declared capability. Never consults quality or speed.

**Layer 2 — should I use it?** Task fit, quality history, latency, cost,
current health, then the exploration policy.

Kept apart so that a provider which merely works does not become the
preferred provider by default. `RoutingDecision` reports `layer1_eligible` and
`layer2_ranked` separately, and flags every candidate dropped for
environmental reasons so a firewall never silently becomes evidence.

### Evaluation scheduler

`EvaluationScheduler` fills the emptiest `(candidate, task_type)` cells first,
never hands a candidate the same item twice, and **skips candidates whose
provider is unusable** — scheduling work for a blocked host produces a queue
of certain failures and a coverage matrix of zeros that read as poor
performance.

### One bug this pass found

Wiring classification into the breaker surfaced a silent failure loss:
`observe(success=False, timeout=True)` with no error text classified as
`AVAILABLE`, whose policy does not open circuits — so the failure was recorded
nowhere and the breaker never tripped. A failure can now never classify as
`AVAILABLE`; three regression tests hold the line.

## Not built

| Component | Why |
|---|---|
| ~~Live-verified NVIDIA NIM calls~~ | **Done, this pass.** Network access was granted mid-session; a real `NVIDIAProvider` call against `meta/llama-3.1-70b-instruct` returned a real, correctly-parsed response (`{"answer": "B"}`, 63 input / 7 output tokens, ~23.5s latency). No longer a gap. |
| A real benchmark run against any registered candidate | The registry now has 5 real candidates (see "Sixth pass"), but none has been benchmarked — still blocked on the corpus, same root cause as everything else in this table. |
| Other provider adapters (OpenAI/Anthropic/Google/local) | The abstraction supports them (see `providers/base.py`, `providers/nvidia.py` as the template); none exist yet because nothing has asked for one. |
| ~~Product-side persistence (notebooks, questions, question provenance, source ingestion)~~ | **Built, eighth pass — this row was wrong.** It said the product backend was out of scope by design; the product backend now lives in `student/` and this repo holds both halves. See the eighth-pass section below for what is real and what still needs a live model. |
| ~~LLM judge (Tier 2)~~ | **Built and executed — this row is superseded.** The judge pipeline is `validator/judge.py` (Layer C of the validator), not `benchmark/judges/`, which is still a 0-byte stub. It calls a real independent model, compares against the key, and refuses a same-family or self-authored judge via `assert_independent`. Executed under frozen configurations against `deepseek-ai/deepseek-v4-flash-0731` judged by `nvidia/ising-calibration-1.5-31b`. The original text follows for the record: "No pipeline exists at all" — `benchmark/judges/__init__.py` is a 0-byte file, not a partial implementation. What exists is judge-independence *enforcement* (`integrity.py:_judge_family`, tested) and the scorers that would consume a judge's verdict. Nothing calls an LLM to judge anything, because that needs a live provider and API keys this environment doesn't have. Said plainly because an earlier summary of this work described Phase 2 as more complete than this. |
| A real reviewer pool | Cannot be built by a model — see `docs/REVIEW_CAPACITY.md`. The `ReviewQueue` / `SeniorAdjudicationQueue` / `GoldChallengeLedger` workflow exists and is tested against synthetic labels; it is the mechanism reviewers would use, not a substitute for them. |
| Generation prompt templates | `benchmark/prompts/` is still an empty stub. `score_generation_rubric` (the scoring side) is built and tested; eliciting a generation from a real candidate needs a live provider this environment doesn't have. |
| Embedding / semantic diversity | Needs `BAAI/bge-small-en-v1.5`; not downloadable in this sandbox. Scorer signature and aggregation (`score_near_duplicate_rate`, `score_family_coverage`) are built and tested against synthetic similarity values; no real embedding has ever been computed. |
| Full contamination battery (C1/C2/C6) | Split isolation and holdout-path access are enforced in code. Exact/near-duplicate retrieval against public corpora and a temporal holdout need an external corpus this environment doesn't have. |
| Automatic `FailureRecord`/`CriticalErrorRecord` capture during a run | The entities and the query layer (`failure_analytics`) are built and tested against hand-built records. Nothing in `runner.py` constructs them automatically from a real run's outputs yet -- needs per-track failure classification, which depends on the same real-corpus/real-model gap as everything else. |
| A production stack for `analytics_api.py` | Deliberately stdlib-only and framework-agnostic (see its module docstring). It is a reference implementation of the contract, not a recommendation to actually run `http.server` in production. |
| Exploratory-metrics reporting path | `SAMPLE_SIZE_AND_STATISTICS.md` distinguishes primary gates from exploratory/descriptive metrics; `report.json` has no field for the latter. The new per-family injection breakdown is exploratory in spirit but is returned by its own function, not folded into a general-purpose reporting path. |
| **The corpus** | **Cannot be built by a model.** ~3,850 expert-authored items, 800–1,200 hours. A model authoring gold it will be graded against is the exact failure the benchmark exists to prevent. |

## What it would take to close the one real remaining gap that doesn't need a corpus or reviewers

The Tier-2 LLM judge is the last piece with no code at all. Closing it needs, from you:

1. **Which provider(s) and model(s)** — the candidate model under test, and a separate judge
   model from a different model family (per `docs/JUDGE_INDEPENDENCE.md` Tier 2: different
   family, different provider when practical). Two Anthropic models, for instance, would not
   satisfy this on their own.
2. **API key(s)**, supplied as environment variables at run time (e.g. `ANTHROPIC_API_KEY`,
   `OPENAI_API_KEY`) — never pasted into chat or committed to the repo. I'll wire the adapter to
   read them from the environment the same way any SDK does.
3. **Confirmation that a small number of live calls during testing is acceptable** — verifying a
   real adapter means actually calling it a handful of times, which costs a few cents to a few
   dollars depending on the model, not the full benchmark budget.

## Synthetic corpus — deliberate design decision

`data/synthetic_harness_v0_4.jsonl` (3,400 items) contains **no medical content**. Prompts are
abstract symbol tasks; gold answers are arbitrary option letters. Every item carries
`provenance.type = "synthetic_harness_test"`.

This exercises the harness. It measures nothing about medical capability, and it cannot be mistaken
for real gold.

## Three bugs found by running the harness, not by reading the spec

1. **`wilson_cluster_by_base_item` had no clustered handler** — returned `None`, so every clustered
   track rendered UNEVALUABLE regardless of n. Robustness silently could not be scored.
2. **Scorecard labelled all UNEVALUABLE as `n<min`** — hiding "metric not computable", which is a
   different and more serious condition.
3. **`GATE-C-MERGE` min_n of 600 was unreachable by construction.** Only pairs whose gold is *not* a
   merge label can be false-merged. With 5 label classes and 3 non-merge classes, 600 resolution
   pairs yield ~360 eligible pairs. The gate could never have been evaluated. Corrected to 350 with
   the unit `distinguishable_pair`.

The third is the interesting one: it was introduced in v0.4, survived spec review, and was only
exposed by executing the code. It is the same class of defect as v0.3's self-passing artifact test.

## Precedence correction found by the acceptance tests

The first implementation let the uncalibrated-registry ceiling short-circuit before gate evaluation,
which masked safety failures — a run with a confirmed CME reported `NOT_VALID_FOR_PRODUCTION_PASS`
instead of `FAIL`. Corrected: the ceiling caps positive outcomes only. FAIL, UNEVALUABLE, and
INVALID_RUN always surface, because "this candidate is dangerous" is more important than "this run
couldn't have been certified anyway".

## What has been built since

The provider adapters, the student engine, the learner app, the billing system and the compute
budget all exist and are covered. The full inventory is in `README.md`. Two findings from that work
belong here because they change what the numbers mean:

**The validator is not fit for purpose.** Measured against the adversarial battery, the 8B validator
caught 11 of 20 planted defects, false-flagged 9 of 10 clean items, and missed both ungrounded
items entirely. A 70B validator caught 10 of 10 before stalling, but its latency went 7s → 103s →
indefinite and its false-flag rate was never measured, because the control arm never completed.
Neither result licenses a claim that Quintek can recognise a bad question.

**The provider catalogue is much thinner than it looks.** Of 102 NVIDIA models probed, 27 served and
17 returned usable JSON; 62 returned 404. OpenRouter listed 414 catalogue entries, of which 21 of 26
probed actually served. A catalogue entry is not an available model, and the funnel in
`benchmark/candidates.py` exists so that difference is never assumed away again.

## Where the model catalogue comes from

`discovery/catalogue_raw.json` was a 1.1 MB copy of OpenRouter's catalogue
living inside the product repository — the "someone manually copies models,
six months later it is stale" shape, already instantiated. It moved by six
entries in the single day between being copied and being checked.

Discovery now lives in a separate repository, `SONIC445-BYTE/Registry-repo`,
under one rule:

> **The agent records observations. Quintek applies policy.**

The external agent fetches and normalises, keeping the raw response
byte-for-byte alongside the normalised form, both hashed in a manifest. It
applies **no eligibility rule whatsoever** — every entry is kept, including
routers, aliases and things with no price. A test there parses its own source
for judgement vocabulary and fails the build if any appears.

Two consequences justify the split:

**The negative space survives.** A snapshot holding only what some rule
accepted could never answer *"what did we pass over in August, and would
today's rules have taken it?"* Re-filtering an old observation under new rules
is only possible if the observation was never filtered.

**Policy keeps one implementation.** Eligibility lives in
`benchmark/candidates.py` and nowhere else. A second copy in the discovery
repository would drift, and when it did nobody could say which of the two
dropped a model.

Availability probing is deliberately absent from v0.1. The record shape is
fixed — keyed on `(model, endpoint, credential_class, time)`, never on the
model alone — and probing refuses rather than half-working. The same model
answers PASS at 08:00 and BILLING_BLOCKED at 18:00; this project measured
exactly that on Cerebras, which authenticated perfectly and returned 402 on
inference.

`benchmark/candidates.load_discovery_snapshot` is the consuming half. It loads
every entry a snapshot holds — routers and aliases included — and applies no
rule of its own; capability is derived on this side, because what counts as
"supports structured output" is Quintek's reading of a parameter list, not a
fact the catalogue stated. Against the first real snapshot: 420 entries in,
355 / 258 / 225 through the generation / validation / vision filters, zero
routers admitted.

Quintek never *requires* the discovery repository to be present. The test that
reads a real snapshot skips when it is absent.

## A router was in every shortlist

`openrouter/free` — "Free Models Router" — was in the generation, validation
AND vision shortlists as though it were a model. It passed every capability
filter, and it prices itself at **0/0** rather than the `-1` sentinel, so the
guard that catches `openrouter/auto` missed it completely.

A leaderboard containing it compares model against model against a
model-selection algorithm, which makes every ranking on it unreadable.

`CatalogueEntry` now carries `entry_kind`, derived from what the catalogue
itself states — routing products carry `tokenizer: "Router"`, aliases carry
`alias_target` — and never from the name, since a router published under
another vendor's namespace would slip a name-based rule. Of 414 entries: 396
MODEL, 12 ALIAS, 6 ROUTER.

`Filter.require_single_model` defaults True and is a named requirement rather
than a hardcoded skip, so a future router-versus-router board can switch it
off deliberately.

## The question-authoring path, as designed

Question Studio was specified as a configurable authoring workspace: source,
target concepts, cross-notebook concepts, question type, difficulty, reasoning
depth, demonstrations and constraints in, generated questions out, through
validation and into the question bank and revision engine. It was never meant
to be the learner's revision destination.

What was built instead was a Studio screen wired entirely to constants --
`runStudio` reveals one fixed example and calls nothing -- while the learner's
own Make Questions screen offered only a count and a question kind. The two
inputs that make the engine do what it was designed to do, reasoning depth and
DEMONSTRATIONS, were therefore unreachable from any path a learner walks.

Both now live on Make Questions, alongside free-text constraints, and travel
into `QuestionGenerator.generate`. Demonstrations were not retired; the
dedicated destination was. The style-not-facts rule they exist for is enforced
server-side in `student/generation.py` and stated on the screen where
references are attached, because a learner uploading a past paper reasonably
expects its questions back and will not get them.

Two defects found while wiring it:

**Demonstrations were readable across accounts.** `_demos` looked ids up with
no owner check, and those ids now arrive from the client. One learner could
read another's reference question by guessing an id, through a channel that
puts the text straight into a prompt. Scoped to the owner.

**Question Studio printed fabricated provenance** -- specific model and prompt
versions for a draft no model wrote. In a product whose claim is traceability
from source to generation to validation, that undermines the architecture
directly. Both fields now say `none`.

## The economics, as measured

`tools_compute_budget.py` answers "given ₹X of recognised revenue, how much inference can Quintek
consume" as a waterfall, because the deductions are the argument: GST is not revenue, annual cash is
twelve months of obligation, unconsumed allowance is a liability denominated in compute, and
free-tier questions come out of the same provider credit.

At the shipped plan prices, the cheapest confirmed PAID pairing costs 0.291 paise per accepted
question (₹1.46 per 500) against a Pro-plan ceiling of 6.023 paise — roughly 95% headroom. The token
profile behind that is measured from a real run (524/137 generation, 594/89 validation). The 80%
acceptance rate is **not** measured and is labelled as an assumption everywhere it is used.

That headroom is not a result. It is the budget for buying a validator that works.

## Track D: the apparatus for measuring a validator

Full detail in `docs/VALIDATOR.md`. In summary:

**A 100-case development corpus and a 93-case holdout now exist.** 40 clean / 40 controlled
defects / 20 ambiguous-edge in development; 53 / 30 / 10 in the holdout. All ten defect classes
are exercised in both. The two sets share no id, no stem and no source passage, and
`devset.assert_disjoint` refuses if they ever do.

**Defects are derived, not written.** Each defective item is one clean item with one edit;
`mutate.apply` computes which fields actually changed and refuses when the edit exceeds what the
mutation declared. Only the `ungrounded` operation may touch the source passage, because the
passage is what "correct" means. That gives matched pairs — and `analysis.matched_pairs` reports
the statistic that separates discrimination from flagging everything.

**The validator is four layers, and every flag names the layer that raised it.** Structural
(deterministic, free), grounding (is the key supported by the supplied passage), an independent
judge (which refuses to run when the judge wrote the item, or shares its family), and conformance
(is this the question that was asked for). An outage in a configured layer raises; it is never
converted into a PASS.

**The design's ceiling was measured before any inference was bought.** Against ground-truth
oracles, v0.1 — structural, grounding, judge — topped out at **60% sensitivity**, because four of
the ten defect classes are invisible to every one of those layers: nothing about an off-concept,
under-difficulty, giveaway or circular-explanation item is wrong in isolation. The missing input
was the generation request. Layer D takes it, and the ceiling is now 100/100 with every layer
earning its place. A design whose ceiling sits below the pass threshold cannot be rescued by a
better model, and finding that out from a model bill is the expensive way.

**Two findings from building it.** A deterministic giveaway check was built, measured at 5 false
positives per 40 clean items against 2 of 4 catches, and removed rather than tuned. And Layer A's
"no false-flag rate" claim was untrue: the option normaliser reduced `Na - (Cl + HCO3)` and
`(Na + Cl) - HCO3` to the same string and reported a real item as having duplicate options.

**A consequence of judging the lower bound that changes what the corpus must be.** Thirty clean
items, every one correctly passed, give a 95% lower bound of 88% — which does not establish 90% at
any level of performance. That is INSUFFICIENT_EVIDENCE, not FAIL, and `metrics.min_items_for` now
reports the number that would settle it: 35 clean items for a perfect run, **53 to tolerate a
single false positive**, 69 for two. The holdout was sized to 53 for that reason. The development
set's 40 tolerates zero, which is one more reason it is not the gate.

### Track D state, as generated

`python3 tools_track_d_status.py --text` produces this, and every field in it is
computed from the corpora, the holdout ledger and the recorded runs:

```
Implementation          COMPLETE
Development testing     PASS
Development evidence    NOT_RUN
Holdout evaluation      NOT_RUN
Human validation        NOT_RUN
Real-model validation   NOT_RUN
Production readiness    NOT_ESTABLISHED
```

**Track D is built. It is not validated.** The report exists because that
distinction erodes by summary rather than by decision: `validator_production_status`
has one code path, its input is four preconditions, and a ceiling run is recorded
as `kind: ceiling` and excluded from `real_runs()` permanently, so the 100/100
ceiling has no route by which it could be reported as performance.

**Read that ceiling correctly.** It says the architecture now carries enough
information to detect all ten planted defect classes. It does not say the
validator detects them. v0.1's 60% was an information gap, not a competence gap,
and Layer D closed it by taking the generation request — which changes what
exists in the pipeline, not how well anything reads it.

**Two benchmarks, one of them supported.** The corpora answer "can the system
detect deliberately planted defects", where ground truth is sound because the
defects were constructed. They say nothing about "does the system agree with
qualified reviewers about real question quality". Both are needed; only the first
is available.

### What Track D has NOT established

- **No validator has been scored against the holdout.** Its ledger has zero `score` entries.
- **No real model has been run through the pipeline.** Only ground-truth oracles, which measure
  the design and are reported as invalid for gating.
- **Nobody has reviewed the corpus.** Every item is `model_authored`, `gold_standard: false`,
  `label_status: unreviewed`. `tools_validator_review.py` is the protocol; two named clinicians
  are what it needs.
- **One bit has already leaked from the holdout**, and is recorded in its ledger as an
  `inspection`: a ceiling run showed the locator check missing a planted `hallucinated_reference`
  because the pattern lacks the word "clause". The pattern was deliberately **not** widened.

## Next steps

Two questions are tangled and must be separated: **can the validator work**, and **which model
should do the validating**. Establish the first while minimising dependence on the second.

1. Run `tools_validator_eval.py experiments` on the **development** set. Three configurations,
   recorded together, because the number that matters is the difference between them: A+B+D (the
   validator without the layer whose failure mode is agreeing with itself), C alone (the judge's
   contribution), and A+B+C+D. "Every layer earns its place" was asserted from a ceiling; this is
   where it is confirmed or withdrawn.
   The first set is `--candidate nvidia:meta/llama-3.1-8b-instruct --judge
   nvidia:meta/llama-3.1-70b-instruct`. Not two 8B checkpoints: Layer C exists to supply a
   judgement the candidate did not produce, and "different checkpoint, same family" is not that.
   The 70B may stall as it did before; that is recorded as INCOMPLETE and excluded from every
   delta, and it is not retried selectively.

2. Run the same fixed development set against the alternative pairing, and compare sensitivity,
   specificity, FP, FN, latency, cost and edge calibration before promoting either. The
   development corpus tolerates zero false positives against a 90% threshold, so read the error
   analysis rather than the headline rate.
3. Freeze the configuration, then score **once** against the holdout with a note saying what
   changed. No prompt edits after the freeze, and in particular none in response to what the
   holdout shows — the locator gap already in its ledger is the worked example.
4. Author a pilot corpus in one subject with two qualified reviewers — enough to compute a real
   kappa. `tools_validator_review.py` computes it and refuses to settle labels while anything is
   disputed.
5. Measure the real production acceptance rate, so the compute budget stops resting on an
   assumption.
6. Only then freeze a benchmark corpus and start the 355-model funnel.

Step 4 is still the gate on any claim that rests on these labels. Everything downstream depends on
whether a second qualified reviewer exists.
