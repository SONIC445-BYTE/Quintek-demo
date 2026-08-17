# Implementation Status

Honest accounting of what is built, what is stubbed, and what cannot be built here.

This file was written by the original implementer and has since been independently verified
and extended by a second pass: every module below was read (not skimmed), every claim was
checked by actually running `python -m pytest tests/ -v` and the CLI (`validate`, `gates`,
`preflight`, `demo`) rather than trusting the docstrings, and two real gaps found during that
verification were closed rather than left silently unchecked. See `docs/V0_4_CHANGELOG.md`
item 1 for why "a green test suite" is not, by itself, sufficient evidence here.

## Built and tested (52 tests passing)

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

## Not built

| Component | Why |
|---|---|
| ~~Live-verified NVIDIA NIM calls~~ | **Done, this pass.** Network access was granted mid-session; a real `NVIDIAProvider` call against `meta/llama-3.1-70b-instruct` returned a real, correctly-parsed response (`{"answer": "B"}`, 63 input / 7 output tokens, ~23.5s latency). No longer a gap. |
| A real benchmark run against any registered candidate | The registry now has 5 real candidates (see "Sixth pass"), but none has been benchmarked — still blocked on the corpus, same root cause as everything else in this table. |
| Other provider adapters (OpenAI/Anthropic/Google/local) | The abstraction supports them (see `providers/base.py`, `providers/nvidia.py` as the template); none exist yet because nothing has asked for one. |
| Product-side persistence (notebooks, questions, question provenance, source ingestion) | Out of this repository's scope by design — this repo is the benchmark harness plus the orchestration layer a product backend calls into (`benchmark/orchestration.py`), not the product backend itself. `ExecutionRecord` carries everything a product's "question provenance" table would need to reference (`execution_id`, `candidate_id`, `prompt_version`, tokens, latency); attaching that to an actual question/notebook record happens in a different codebase. |
| LLM judge (Tier 2) | **No pipeline exists at all** — `benchmark/judges/__init__.py` is a 0-byte file, not a partial implementation. What exists is judge-independence *enforcement* (`integrity.py:_judge_family`, tested) and the scorers that would consume a judge's verdict. Nothing calls an LLM to judge anything, because that needs a live provider and API keys this environment doesn't have. Said plainly because an earlier summary of this work described Phase 2 as more complete than this. |
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

## Next steps

1. Add a real provider adapter and run Phase 1 against a live model on synthetic data.
2. Author a 50-item pilot corpus in one subject with two qualified reviewers — enough to compute a
   real kappa and calibrate thresholds.
3. Only then scale the corpus.

Step 2 is the gate. Everything downstream depends on whether a second qualified reviewer exists.
