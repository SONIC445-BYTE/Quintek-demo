# Benchmark Integrity CI

Every commit should run:

1. forbidden-token scan
2. dataset schema validation
3. duplicate ID detection
4. holdout-path access test
5. gold-not-exposed test
6. prompt-hash consistency test
7. candidate-manifest completeness test
8. budget enforcement test
9. stochastic variance protocol test
10. injection battery smoke test

## Forbidden generated artifacts

The following strings must never appear in source/docs unless explicitly inside a test demonstrating detection:

- `citeturn`
- raw internal citation tokens
- hidden tool-call IDs
- unapproved benchmark answers inside prompt templates

## Holdout isolation test

A production candidate process must fail a test if it attempts to read HOLDOUT files directly.

## Gold leakage test

Generation prompts must be inspected to ensure:
- gold.answer absent
- gold.rationale absent
- hidden reviewer labels absent

unless the task itself is a judge task where that information is intentionally provided to the judge.

## Integrity status

Any integrity test failure:
> INVALID RUN

It is not a model failure and must not contribute to rankings.
