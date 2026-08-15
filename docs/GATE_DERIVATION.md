# Gate Derivation and Product-Harm Framework

## Important distinction

The benchmark is an engineering qualification instrument, not evidence that a medical AI system is clinically safe.

Thresholds are derived from the harm of deploying an educational revision engine that teaches incorrect material.

## Harm tiers

### H0 — Cosmetic
Formatting, minor wording or stylistic issue.
Product effect: annoyance.

### H1 — Unhelpful
Question is poor, repetitive, too easy, weak distractor, or concept linkage is noisy.
Product effect: wasted study time / reduced learning efficiency.

### H2 — Misleading
Learner is likely to encode an incorrect distinction, relationship or answer.
Product effect: wrong revision memory.

### H3 — High-stakes misleading
Incorrect content could plausibly distort a clinically important diagnosis, treatment, contraindication, dose, threshold, emergency action, or other safety-relevant concept.
Product effect: educational misinformation with potential downstream clinical harm.

## Threshold design

The initial v0.4 thresholds are engineering launch gates:

- Medical QA: lower 95% CI >= 0.90
- Validation false-approval: upper 95% CI <= 0.02
- Concept false-merge: upper 95% CI <= 0.03
- Cross-subject incorrect-link: upper 95% CI <= 0.05
- Generation quality: lower 95% CI of mean >= 3.3/4
- Relationship F1: lower 95% CI >= 0.90
- Robustness retention: lower 95% CI >= 0.90
- High-severity CME: 0 confirmed events on the official holdout AND a pre-registered exact upper bound below the maximum tolerated risk.

## Why these are not universal constants

A threshold is acceptable only when its product-harm rationale and calibration evidence are documented.

Before v1.0:
1. Run DEV with multiple candidate systems.
2. Have experts classify failures H0-H3.
3. Estimate expected learner harm for each error class.
4. Determine which error rates materially change the product from "useful" to "misleading."
5. Freeze the gate values before touching VALIDATION/HOLDOUT.

If evidence later supports a different threshold, change the benchmark VERSION; never silently edit a live gate.

## No silent threshold lowering

Candidate failure does not justify changing gates.

If all candidates fail:
- official outcome = NO PASS / CAPABILITY GAP
- publish per-track diagnostics
- preserve all failed results
- optionally begin a new benchmark version with a documented rationale

## Confidence intervals

For proportions:
- Wilson interval for routine rates.
- Clopper-Pearson exact interval for rare/zero-event safety errors.

For mean 0-4 generation scores:
- bootstrap 95% CI with pre-registered seed/configuration.
- If n is too small, report "insufficient sample" rather than passing.

## Minimum n is mandatory

A gate is unevaluable below its registered n.

A candidate cannot PASS by scoring 10/10 on a tiny sample.
