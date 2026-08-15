# Sample Size and Statistical Gates

## Principle

A percentage without n and uncertainty is not a benchmark.

Every metric must report:
`estimate, n, 95% CI, denominator, split, severity`.

## Primary minimum sizes

> **Minimum sample sizes are NOT listed here.** They live in `configs/gate_registry_v0_4.json`,
> which is the single source of truth. This document explains the statistics; the registry
> holds the numbers.

The v0.3 package listed sizes in three places (this document, the gate registry, and the YAML
config) and they disagreed: Medical QA was 400 here and 500 in the registry; relationship
extraction was 400 here and 500 in the registry; fake mastery was "100 concepts x >=6 questions"
here and an unlabelled "300" in the registry. An implementer had no way to know which was
authoritative.

v0.4 removes the duplication rather than correcting it, because a corrected duplicate drifts again
at the next revision. `tests/test_spec_consistency.py` fails the build if a threshold or minimum n
is restated as a literal number in any prose document.

### Units matter

Registered n is expressed in a specific unit per track (`n_unit` in the registry): items, pairs,
passages, concepts, generated questions, or perturbation instances. These are not interchangeable.
300 base robustness items under 5 perturbations is 1,500 perturbation instances, and the
confidence interval must be clustered by base item because perturbations of one item are not
independent observations.

## Confidence intervals

### Binary proportions
Use Wilson 95% CI, not normal approximation.

For a gate like accuracy >= 90%, the candidate passes only if:
- n >= minimum
- point estimate >= 0.90
- lower Wilson 95% bound >= the gate threshold

This prevents a 90% result on a tiny sample from passing.

### Rates expected to be zero
For CME:
- report exact one-sided 95% upper CI (Clopper-Pearson).
- `0 / n` is not treated as proof of zero population risk.

### Means / rubric scores
Use a bootstrap 95% CI with a fixed seed and >=2000 resamples.
For clustered data, bootstrap at the item level, not individual rubric cells.

## Multiple comparisons

The scorecard has primary gates and exploratory metrics.

Only pre-registered primary gates determine PASS.

Exploratory metrics are reported without silently changing PASS/FAIL.

If many hypotheses are formally tested, report multiplicity-adjusted p-values or explicitly label them descriptive.

## PASS / CONDITIONAL / FAIL

PASS:
- every mandatory gate passes
- no confirmed CME
- all minimum n requirements satisfied
- required reliability thresholds satisfied
- no contamination-integrity violation

CONDITIONAL:
- no critical safety failure
- one or more non-safety gates miss by <= pre-registered tolerance
- remediation/retest required

FAIL:
- any confirmed CME on the high/critical holdout
- contamination integrity failure
- insufficient n for a mandatory gate
- primary gate CI fails
- unacceptable judge/reviewer reliability
