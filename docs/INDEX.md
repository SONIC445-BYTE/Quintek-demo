# Document Index and Reading Order v0.4

## Authority hierarchy

When documents conflict, resolve in this order:

1. `configs/gate_registry_v0_4.json` — all thresholds, minimum n, gate directions, outcome states
2. `docs/SCORECARD_SPEC.md` — how results may and may not be rendered
3. `docs/SEVERITY_TAXONOMY.md` — severity, CME, and harm-tier semantics
4. All other documents

Prose never overrides the registry. If a document appears to state a threshold, it is either
narrating history or is a defect — report it.

## Reading order for an implementer

| # | Document | Why |
|---|---|---|
| 1 | `README.md` | scope, status, the absolute rule |
| 2 | `docs/MASTER_BUILD_PROMPT_V0_4.md` | the build instruction |
| 3 | `configs/gate_registry_v0_4.json` | every number |
| 4 | `docs/SCORECARD_SPEC.md` | integrity as precondition |
| 5 | `docs/SEVERITY_TAXONOMY.md` | the three severity scales |
| 6 | `docs/REVIEW_CAPACITY.md` | human cost and attainable outcomes |
| 7 | `docs/CANDIDATE_DEFINITION.md` | what is under test |
| 8 | `docs/SAMPLE_SIZE_AND_STATISTICS.md` | CI methods |
| 9 | `docs/GATE_DERIVATION.md` | harm tiers, why thresholds exist |
| 10 | `docs/TRACK_GATES.md` | gate ID map |
| 11 | `docs/JUDGE_INDEPENDENCE.md` | judge tiers |
| 12 | `docs/INTER_RATER_AND_HUMAN_REVIEW.md` | kappa, calibration, sentinels |
| 13 | `docs/REVIEWER_QUALIFICATION.md` | who may review |
| 14 | `docs/CRITICAL_MEDICAL_ERROR.md` | CME categories |
| 15 | `docs/GOLD_ERROR_PATHWAY.md` | gold challenge lifecycle |
| 16 | `docs/CONTAMINATION_PROTOCOL.md` | holdout integrity |
| 17 | `docs/INJECTION_BATTERY.md` | attack families |
| 18 | `docs/VARIANCE_PROTOCOL.md` | nondeterminism handling |
| 19 | `docs/BUDGET_PROTOCOL.md` | modes and cost control |
| 20 | `docs/SEMANTIC_DIVERSITY.md` | fake-mastery measurement |
| 21 | `docs/CROSS_SUBJECT_SPEC.md` | concept graph evaluation |
| 22 | `docs/ALL_CANDIDATES_FAIL.md` | the outcome nobody plans for |
| 23 | `docs/INTEGRITY_CI.md` | per-commit checks |
| 24 | `docs/IMPLEMENTATION_ACCEPTANCE.md` | definition of done |
| 25 | `docs/DECISION_LOG.md` | architecture decisions |
| 26 | `docs/V0_4_CHANGELOG.md` | what changed and why |
| 27 | `docs/MODEL_DISCOVERY.md` | dynamic provider/model discovery, and why it is not the freeze |

## New in v0.4

- `docs/SCORECARD_SPEC.md`
- `docs/SEVERITY_TAXONOMY.md`
- `docs/REVIEW_CAPACITY.md`
- `docs/MASTER_BUILD_PROMPT_V0_4.md` (replaces the stale v0.2 prompt)
- `docs/INDEX.md`
- `configs/gate_registry_v0_4.json`, `configs/v0_4.yaml`
- `tests/test_spec_consistency.py`
