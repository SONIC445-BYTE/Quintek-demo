# All-Candidates-Fail Policy

## Official outcome

If every candidate fails one or more mandatory gates:

> **NO PASS — CAPABILITY GAP**

This is a valid and expected benchmark outcome.

## Prohibited response

Never:
- lower thresholds after seeing results,
- remove a failing track,
- redefine critical error,
- swap to a more favorable metric,
- increase tolerance post hoc,
- expose holdout items to candidates,
- label the least-bad candidate as PASS.

## What happens next

1. Freeze the benchmark results.
2. Publish the failure matrix.
3. Identify whether failures are model, prompt, retrieval, validation, ontology, robustness or cost-related.
4. Improve the candidate architecture.
5. Re-run DEV.
6. If the benchmark itself needs changing, create v0.4 with a written change rationale.
7. Never overwrite v0.4 results.

## "Best available" is different from "qualified"

The report may say:

> Candidate A is currently best.

It may not say:

> Candidate A is safe/qualified.

unless every mandatory gate passes.
