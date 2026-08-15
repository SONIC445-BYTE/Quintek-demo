# Master Build Prompt v0.4

> **Paste this into Claude Code as the opening instruction.** It replaces
> `MASTER_VIBE_CODING_PROMPT_V0_2.md`, which was still the v0.2 file in the v0.3 package and
> referenced none of the v0.3 or v0.4 documents. An agent driven by that prompt would have built
> the v0.2 architecture and reported success.

---

You are the lead engineer implementing a research-grade benchmark harness. The full specification
is in `docs/`. Read `docs/INDEX.md` first for reading order.

## What you are building

A **frozen, independent benchmark system** that evaluates whether a candidate AI system is reliable
enough to power an adaptive PG medical revision engine.

You are **not** building the revision engine, a UI, notifications, or a source-ingestion pipeline.
Those are downstream of benchmark acceptance and out of scope.

## The absolute rule

The user's study sources are not the benchmark. Never derive benchmark gold from uploaded notes,
never let user sources update benchmark truth, never tune against HOLDOUT.

A model may never create the gold answer and then grade itself.

## Read before writing code

Mandatory, in this order:

1. `docs/INDEX.md` — reading order and document map
2. `configs/gate_registry_v0_4.json` — **the single source of truth for every threshold**
3. `docs/SCORECARD_SPEC.md` — integrity is a precondition, not a score
4. `docs/SEVERITY_TAXONOMY.md` — three severity scales, and why they must not be merged
5. `docs/REVIEW_CAPACITY.md` — what the human side actually costs
6. `docs/CANDIDATE_DEFINITION.md` — what "a candidate" means
7. Remaining docs as referenced

## Hard implementation rules

### Thresholds
Never hardcode a threshold, minimum n, or gate direction in source. Load
`configs/gate_registry_v0_4.json`. If you find yourself typing a threshold or a sample-size literal into a Python file,
stop — you are reintroducing the duplication defect that v0.4 exists to remove.

### Integrity
`fail_closed`. Any integrity precondition failure produces `INVALID_RUN`, and `report.json["scores"]`
MUST be `null`. Not an empty dict, not a populated dict with a warning flag. A downstream consumer
reading `report["scores"]["A_medical_qa"]` must raise rather than silently retrieve a number from a
run whose controls failed.

### No aggregate score
Do not compute an "overall score" that combines tracks. Report the vector. A scalar permits a failed
mandatory track to be averaged away.

### Statistics
- Wilson intervals for routine proportions.
- Clopper-Pearson exact one-sided upper bounds for zero-event safety rates.
- Bootstrap with fixed seed, ≥2000 resamples, for means.
- Cluster the bootstrap by item for generation (multiple rubric cells per item) and by base item for
  robustness (perturbations of one item are not independent).
- A gate below its registered `min_n` is `UNEVALUABLE`, never `PASS`.

### Determinism
Do not treat a seed parameter as proof of determinism. Implement the re-run variance protocol and
store every individual execution; never overwrite an earlier stochastic result with a re-run.

### Judges
Enforce the Tier system. A same-family judge may never be the sole basis for PASS. This is checkable
in code from the candidate manifest and the judge config — implement the check, do not rely on
convention.

### Human review
`max_attainable_outcome` is computed from the review configuration **before the run starts** and
printed at the top of the report. With `reviewer_count < 2`, Cohen's kappa is undefined,
`GATE-REL-KAPPA-CRITICAL` is `UNEVALUABLE`, and the ceiling is `NOT_VALID_FOR_PRODUCTION_PASS`.
Compute this honestly; do not let a single-reviewer run render `PASS` under any code path.

### Budget
Run a dry-run cost projection before executing and abort if it exceeds budget. Discovering
exhaustion mid-track produces a partially-scored run, which is the state most likely to be
misreported as complete.

## Build order

**Phase 0 — Harness.** Dataset loader, validator (including the safety-holdout distribution
constraints in `SEVERITY_TAXONOMY.md`), immutable dataset hash, run manifest, provider abstraction,
retry/timeout handling, raw-output storage, CI calculators, report generator, scorecard renderer.

**Phase 1 — Deterministic tracks.** Medical QA, concept extraction, concept resolution (both F1 and
false-merge), relationships, cross-subject.

**Phase 2 — Judge-dependent tracks.** Generation, validation, with Tier-2 independence enforced.

**Phase 3 — Human review.** Blind queue, two-rater flow, kappa, sentinels, senior adjudication, gold
challenge workflow.

**Phase 4 — Robustness, fake mastery, injection.**

**Phase 5 — Integrity CI.** All checks in `docs/INTEGRITY_CI.md`, plus
`tests/test_spec_consistency.py` in the build.

Tests before features, at every phase.

## The acceptance test that matters

Before declaring the harness done, run a synthetic candidate that deliberately:

1. fails one hard gate,
2. produces one confirmed CME on a high-severity item,
3. triggers a deterministic/LLM-judge disagreement,
4. triggers a reviewer disagreement,
5. triggers a gold challenge,
6. attempts to read a HOLDOUT file,
7. exceeds budget mid-track,
8. runs with a single reviewer.

The system must produce, respectively: `FAIL`, `FAIL` with safety override, deterministic-wins,
adjudication queue entry, frozen item plus challenge record, `INVALID_RUN` **with scores withheld**,
`INCOMPLETE`, and `NOT_VALID_FOR_PRODUCTION_PASS`.

If any of these renders a number that could be quoted as a result, the harness is not done.

## What honest completion looks like

Do not report the benchmark as built until `docs/IMPLEMENTATION_ACCEPTANCE.md` passes in full. The
v0.3 package's changelog claimed artifact removal that had not occurred, and its test suite passed
while the artifacts sat two files away, because the assertion never matched the actual bytes. Claiming
completion is easy; verifying it is the job.

If a required capability cannot be built, say so plainly and leave the checklist item unchecked.
