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

## Not built

| Component | Why |
|---|---|
| Real provider adapters | Need API keys and a cost budget. The abstraction (including retry/timeout) is done; each adapter is ~40 lines. |
| LLM judge (Tier 2) | **No pipeline exists at all** — `benchmark/judges/__init__.py` is a 0-byte file, not a partial implementation. What exists is judge-independence *enforcement* (`integrity.py:_judge_family`, tested) and the scorers that would consume a judge's verdict. Nothing calls an LLM to judge anything, because that needs a live provider and API keys this environment doesn't have. Said plainly because an earlier summary of this work described Phase 2 as more complete than this. |
| A real reviewer pool | Cannot be built by a model — see `docs/REVIEW_CAPACITY.md`. The `ReviewQueue` / `SeniorAdjudicationQueue` / `GoldChallengeLedger` workflow exists and is tested against synthetic labels; it is the mechanism reviewers would use, not a substitute for them. |
| Generation prompt templates | `benchmark/prompts/` is still an empty stub. `score_generation_rubric` (the scoring side) is built and tested; eliciting a generation from a real candidate needs a live provider this environment doesn't have. |
| Embedding / semantic diversity | Needs `BAAI/bge-small-en-v1.5`; not downloadable in this sandbox. Scorer signature and aggregation (`score_near_duplicate_rate`, `score_family_coverage`) are built and tested against synthetic similarity values; no real embedding has ever been computed. |
| Full contamination battery (C1/C2/C6) | Split isolation and holdout-path access are enforced in code. Exact/near-duplicate retrieval against public corpora and a temporal holdout need an external corpus this environment doesn't have. |
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
