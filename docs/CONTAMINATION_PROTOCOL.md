# Public Benchmark Contamination Protocol

## Problem

Public datasets such as MedQA and MedMCQA are unsuitable as the sole generalization holdout for a modern medical LLM. MedGemma's current model documentation explicitly warns that medical benchmark performance can be affected by training-data contamination and recommends validation on data not publicly available to non-institutional researchers.

> **CITATION REQUIRED.** This claim is currently unsourced in this package. Before any public release of benchmark results it must carry a verifiable reference (model card URL + access date). It was inherited through three spec versions carrying a broken inline citation token and was never verified by a human. 

## Policy

Public benchmarks are:
- diagnostic
- development/DEV
- regression
- never the sole PASS gate for generalization.

## Private holdout

Create a private, expert-authored holdout that:
- is not published
- is not uploaded to public repositories
- is access-controlled
- is never included in prompts used for tuning
- is versioned and hashed
- contains original stems and cases
- includes novel combinations of established concepts
- has two independent medical reviews

## Contamination test battery

### C1 — Exact-string retrieval
Search candidate training corpora when available and legally inspectable.

### C2 — Near-duplicate retrieval
Use a fixed embedding model to compare benchmark items against known public corpora.

### C3 — Canary questions
Create novel expert-authored items with unusual but medically valid combinations and wording.

### C4 — Paraphrase challenge
Create multiple semantically equivalent versions with novel wording.

### C5 — Counterfactual consistency
Construct paired items where a controlled fact changes and the answer must change accordingly.

### C6 — Temporal holdout
For knowledge that can change, use information created after the model's documented training cutoff where feasible.

## Interpretation

A high score on public datasets = benchmark competence, not proof of uncontaminated generalization.

A high score on private holdout + canary/counterfactual tracks = stronger evidence of generalization.

No contamination detector can prove that a model has never seen a concept. The benchmark should therefore report contamination risk rather than pretend to establish absolute absence.
