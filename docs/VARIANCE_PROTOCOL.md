# Re-run Variance Protocol v0.4

## Principle

Most APIs are not guaranteed to be deterministic even at temperature 0. A seed parameter, when available, is metadata — not proof of deterministic execution.

## Protocol

For stochastic candidates:
- Run each required item at least 3 times in DEV during characterization.
- For the official VALIDATION/HOLDOUT run, use the registered execution policy.
- Re-run a random 10% sentinel sample in a second execution batch.

## Variance metrics

For deterministic-choice tasks:
- answer disagreement rate
- concept-label disagreement rate
- validator-decision disagreement rate

For scalar scores:
- mean absolute score difference
- standard deviation
- 95% empirical interval

## Initial stability gate

A candidate must have:
- <= 2% answer disagreement on sentinel QA items
- <= 3% validator decision disagreement
- <= 0.20/4 mean absolute generation-score difference

These are v0.4 engineering starting gates and must be calibrated on DEV.

## Failure interpretation

If the system fails variance:
> VARIANCE UNSTABLE

It does not automatically mean the model is bad.

The system may require:
- deterministic decoding where available,
- majority voting,
- constrained decoding,
- caching,
- repeated sampling with aggregation.

## Reproducibility

Store every individual execution. Never overwrite an earlier stochastic result with the re-run.
