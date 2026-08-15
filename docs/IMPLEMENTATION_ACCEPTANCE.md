# Implementation Acceptance Checklist

The benchmark is NOT considered built until all of these exist and pass tests.

Status below was produced by running `python -m pytest tests/ -v` against this repository
(52 tests, all passing at the time this checklist was last updated) and by reading every
module listed, not by trusting a prior claim of completion — see `V0_4_CHANGELOG.md` item 1
for why that distinction matters. See `IMPLEMENTATION_STATUS.md` in the repo root for the
fuller account, including what is explicitly NOT built and why.

## Core
- [x] Dataset loader — `benchmark/dataset.py:load`
- [x] Dataset validator — `benchmark/dataset.py:validate`, including split isolation and the
      safety-holdout distribution constraints from `SEVERITY_TAXONOMY.md`
- [x] immutable dataset hash — `benchmark/integrity.py:sha256_file`
- [x] run manifest — `benchmark/runner.py:Runner._meta` / `_candidate_manifest`, fields per
      `CANDIDATE_DEFINITION.md`
- [x] provider abstraction — `benchmark/providers/base.py`
- [x] retry/timeout/error handling — `benchmark/providers/base.py:RetryPolicy`; the base
      `generate()` retries on any exception up to `max_retries`, timeout is passed to the
      concrete provider's own client, and every response records `attempts`. Covered by
      `tests/test_providers_retry.py`.
- [x] raw-output storage — `Runner.run` writes `outputs.jsonl` (one line per item, including
      failed attempts) and `errors.jsonl`; re-runs get a new `run_id` directory, never
      overwritten
- [x] deterministic scoring — `benchmark/scorers/deterministic.py`
- [x] CI calculators — `benchmark/stats.py`: Wilson, Clopper-Pearson (exact, one-sided),
      bootstrap, cluster bootstrap, Cohen's kappa, weighted kappa
- [x] report generator — `benchmark/reports/scorecard.py:build_report` / `write_report`

## Tracks
- [x] medical QA — `score_medical_qa`
- [x] concept extraction — `score_concept_extraction`
- [x] concept resolution — `score_concept_resolution_f1` + `score_concept_false_merge`
      (`GATE-C-MERGE`, added in v0.4)
- [x] relationship extraction — `score_relationships`
- [~] question generation — `score_generation_rubric` aggregates human rubric ratings
      (clustered by item, matching `GATE-E-RUBRIC`'s CI method); there is no generation
      *prompt template* pipeline (`benchmark/prompts/` is an empty stub) because that needs a
      real candidate provider to generate against, which needs API keys this environment
      does not have. The scoring side is complete; the elicitation side is not built.
- [x] question validation — `score_validation_false_approval`
- [x] cross-subject — `score_cross_subject_incorrect_links`
- [~] fake mastery — `score_near_duplicate_rate` + `score_family_coverage` (`GATE-H-FAMILY`,
      added in v0.4) take precomputed cosine similarities as input; the embedding model
      itself (`BAAI/bge-small-en-v1.5`, per `SEMANTIC_DIVERSITY.md`) is not downloadable in
      this environment, so nothing here has ever computed a real similarity. The scorer
      signature and aggregation logic are complete and tested against synthetic pairs.
- [x] robustness — `score_robustness_retention`, clustered by `base_item_id`

## Human review
- [x] blinded queue — `benchmark/adjudication/queue.py:ReviewQueue` (never carries candidate
      identity; independently shuffled per rater)
- [x] two independent raters — `ReviewQueue.raters_per_item`; a single-rater configuration is
      accepted as the legitimate developmental mode from `REVIEW_CAPACITY.md`, not silently
      upgraded to look like two
- [x] Cohen/weighted kappa — `benchmark/stats.py:cohens_kappa` / `weighted_kappa`; raises
      (does not return a degenerate number) with fewer than two raters
- [x] sentinel items — `benchmark/adjudication/queue.py:SentinelMonitor`
- [x] senior adjudication — `benchmark/adjudication/queue.py:SeniorAdjudicationQueue`; per
      `CRITICAL_MEDICAL_ERROR.md`, disagreement is never averaged, both rater labels are kept,
      and only a senior-adjudicated, confirmed label counts
- [x] gold challenge workflow — `benchmark/adjudication/gold_challenge.py:GoldChallengeLedger`,
      full lifecycle from `GOLD_ERROR_PATHWAY.md`; append-only, so a correction never rewrites
      a historical run
- [ ] a real reviewer pool — cannot be built by a model; see `REVIEW_CAPACITY.md`. The
      workflow above is the mechanism reviewers would use, not a substitute for reviewers.

## Integrity
- [x] holdout isolation — `benchmark/integrity.py:_holdout_isolation` + dataset split-overlap
      check
- [~] contamination protocol — split isolation and holdout-path-access are enforced in code;
      the C1/C2/C6 battery in `CONTAMINATION_PROTOCOL.md` (exact/near-duplicate retrieval
      against public corpora, temporal holdout) needs an external corpus and an embedding
      model this environment does not have, so those checks are specified but not executable
      here
- [x] prompt version hashes — `_prompt_hashes` precondition + recorded in the run manifest
- [x] model version capture — part of the candidate manifest
- [x] deterministic seeds where supported — fixed bootstrap seed; `ScriptedProvider` seed
- [x] no self-grading — architecturally enforced: judge config is never derived from the
      candidate manifest
- [x] same-family judge cannot gate PASS — `_judge_family`, exercised by
      `test_scenario_8_same_family_judge_blocks_pass`

## Statistics
- [x] minimum n enforced — `evaluate_gate` returns `UNEVALUABLE` below registered `min_n`,
      never `PASS`
- [x] Wilson CI — `stats.wilson`, validated against a hand-computed reference value
- [x] exact upper CI for zero-event safety rates — `stats.clopper_pearson_upper_one_sided`,
      validated against the closed form `1 - alpha**(1/n)` and against `test_spec_consistency.py`
- [x] bootstrap CI — `stats.bootstrap_mean`
- [x] cluster-aware bootstrap — `stats.bootstrap_cluster`
- [x] pre-registered gates — `configs/gate_registry_v0_4.json`, loaded not hardcoded; enforced
      by `test_spec_consistency.py:test_registry_is_sole_source_of_thresholds`
- [ ] exploratory metrics separated — no exploratory/descriptive-metric reporting path exists
      as a first-class concept distinct from the registered gates. Nothing currently smuggles
      an exploratory number into PASS/FAIL (there is nothing to smuggle it FROM), but
      `SAMPLE_SIZE_AND_STATISTICS.md`'s distinction between primary gates and exploratory
      metrics has no dedicated field in `report.json`. Left unchecked rather than papered over.

## Final test

Run a synthetic test where:
1. candidate intentionally fails one hard gate
2. candidate produces one CME
3. judge disagrees with deterministic scoring
4. reviewers disagree
5. gold is challenged
6. holdout is inaccessible

The system must produce FAIL/ADJUDICATION states correctly.

**Status: built and passing.** `tests/test_acceptance.py` covers 1, 2, 3, and 6 (plus budget
exhaustion and single-reviewer/same-family-judge ceilings beyond what this checklist asked
for). `tests/test_adjudication.py::test_disagreement_escalates_and_preserves_both_labels`
covers 4. `tests/test_adjudication.py::test_gold_challenge_lifecycle_end_to_end` covers 5.
All are exercised against the real registry, runner, and gate-evaluation code — not mocked
out — per `python -m pytest tests/ -v`.
