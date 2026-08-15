# PG Revision Benchmark v0.4

A frozen, independent benchmark that evaluates whether a candidate AI system is reliable enough to
power an adaptive PG medical revision engine.

## Status — read this first

**This package is a specification. The engine is not built.**

No runner, provider adapter, scorer, judge, or adjudication interface exists in this package. What
exists is the specification, the gate registry, and a spec-consistency test suite. Implementation is
the next step, driven by `docs/MASTER_BUILD_PROMPT_V0_4.md`.

Two further things must be true before any result from this benchmark means anything:

1. **The corpus does not exist.** Roughly 3,850 expert-authored items are required, at an estimated
   800–1,200 hours of qualified medical author time. No model may author or verify the gold it will
   be graded against. See `docs/REVIEW_CAPACITY.md`.
2. **The thresholds are uncalibrated.** Every gate value is an engineering starting point that has
   never been validated against real candidate behaviour. Per the gate registry, a run against
   uncalibrated thresholds cannot yield an official PASS.

Do not claim the benchmark is built until `docs/IMPLEMENTATION_ACCEPTANCE.md` passes in full.

## The absolute rule

```
User pipeline:  source -> notebook -> concept graph -> questions -> revision
Benchmark:      frozen independent corpus -> candidate -> evaluation -> scorecard
```

The first can never write to the second. A model may never create the gold answer and then grade
itself.

## What it measures

Medical factual QA, concept extraction, concept resolution, relationship extraction, cross-subject
linking, PG-level question generation, question validation, fake-mastery resistance, robustness,
prompt injection, and critical medical error rate — each gated independently, with no aggregate
score that could average a failed track away.

## Start here

`docs/INDEX.md` gives the reading order and authority hierarchy.

`configs/gate_registry_v0_4.json` is the single source of truth for every threshold. No other file
may state one.

## Scope limitation

This is an engineering qualification instrument for an educational tool. A PASS means a candidate
configuration met pre-registered engineering gates on a frozen corpus. It is **not** a claim of
clinical safety, clinical validity, or regulatory fitness, and must never be reported as one.
