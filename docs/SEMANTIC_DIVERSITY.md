# Track H — Semantic Diversity / Fake-Mastery Resistance

## Goal

Prevent the system from creating six surface rewrites of the same question and calling that mastery.

## Fixed embedding model

v0.4 default:
`BAAI/bge-small-en-v1.5`

The embedding model/version is part of the benchmark manifest.

If changed, it creates a new benchmark version.

## Question-family coverage

For each concept, target at least six distinct families:
1. direct recall
2. clinical vignette
3. interpretation
4. mechanism
5. differential diagnosis
6. management/application

Future:
7. image
8. video

## Near-duplicate metric

Normalize question text and compute embeddings.

For every pair within the same concept:
- cosine similarity >= 0.90 = near-duplicate candidate
- 0.82–0.90 = review band
- <0.82 = presumptively semantically distinct

These thresholds are INITIAL calibration thresholds.

Before using them as a holdout gate:
1. create 200 human-labeled same-vs-distinct pairs in DEV
2. sweep thresholds
3. select thresholds maximizing F1
4. freeze thresholds
5. never retune on HOLDOUT.

## Metrics

- mean pairwise cosine distance
- near-duplicate rate
- review-band rate
- family coverage
- template repetition rate
- answer-position diversity
- concept fidelity

## Fake-mastery failure — two distinct quantities

v0.3 contained a 2.5x contradiction: this document failed a concept when its near-duplicate rate
exceeded 20%, while the gate registry failed the run above 8%. These were read as the same number
and are not.

They measure different things and both are retained, at different levels:

### Per-concept diagnostic flag (not a gate)
A single concept is FLAGGED FOR REVIEW when any of:
- fewer than the registered minimum families are represented (`min_families_per_concept`), OR
- that concept's near-duplicate rate exceeds the per-concept diagnostic threshold, OR
- human reviewers classify a substantial share of its generated items as repetitive.

A flagged concept does not by itself fail the run. It is a signal pointing at where the candidate
is collapsing question variety, and it drives the family-coverage gate below.

### Run-level gates (mandatory)
Two gates, both defined in `configs/gate_registry_v0_4.json`:

- `GATE-H-DUP` — the near-duplicate rate aggregated across **all** generated questions.
- `GATE-H-FAMILY` — the proportion of concepts meeting family coverage. **Added in v0.4.**
  v0.3 defined family coverage as a per-concept condition and never aggregated it, so a candidate
  could fail coverage on the majority of concepts without failing the benchmark.

Thresholds and minimum n for both live in the registry, not here.

### Why the aggregate gate is stricter than the per-concept flag
A per-concept threshold tolerates local variation across 100 concepts. The aggregate rate is
computed over ~600 generated questions and is correspondingly less noisy, so it can and should sit
tighter. Applying the per-concept number to the aggregate would let a candidate produce one-fifth
near-duplicates overall and still pass, which defeats the track.

## Threshold calibration status

These thresholds are UNCALIBRATED engineering starting points. Before they may gate an official
run, the calibration procedure above must be executed on DEV and the values frozen in the registry.
Per `configs/gate_registry_v0_4.json`, a run against uncalibrated thresholds cannot yield PASS.
