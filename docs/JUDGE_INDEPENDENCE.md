# Independent Judge Policy

## Problem

A judge from the same model family can share systematic errors with the candidate and may rubber-stamp it.

## Judge tiers

### Tier 0 — Deterministic
Preferred for:
- exact answer matching
- option-key checking
- schema validation
- arithmetic
- ID/link consistency

No model correlation.

### Tier 1 — Human
Required for:
- critical medical errors
- gold disputes
- low-confidence validation
- final generation-quality adjudication samples

### Tier 2 — Independent LLM judge
A primary LLM judge must satisfy:
1. different model family from candidate
2. different provider when practical
3. no candidate answer/rationale hidden chain-of-thought is used as judge instruction
4. judge sees the item and candidate output, but not the candidate's internal reasoning
5. judge is not fine-tuned on candidate outputs
6. judge is frozen before the holdout is scored

Example:
- Candidate: MedGemma
- Primary LLM judge: a non-Gemma family model from another provider.

### Tier 3 — Same-family judge
Allowed only as a diagnostic secondary judge.
It can NEVER be the sole basis for PASS.

## Correlated-judge test

For a calibration subset:
- compare Tier-2 judge decisions against human adjudication
- compare Tier-3 same-family decisions against human adjudication
- if same-family agreement is materially higher without corresponding human agreement, flag possible rubber-stamping.

## Blindness

The judge must not receive:
- benchmark split name if it could leak test status
- model ranking
- previous scores
- other model outputs unless explicitly performing a multi-model comparative task.

## Judge disagreement

If deterministic and LLM judge disagree:
- deterministic wins for deterministic properties
- human adjudication for medical semantics.
