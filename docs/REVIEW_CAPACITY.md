# Review Capacity and Corpus Authoring Cost v0.4

## Why this document exists

Every version of this benchmark from v0.1 through v0.3 specified reviewer *standards* with increasing precision and never once specified reviewer *quantity*. `REVIEWER_QUALIFICATION.md` defines who is permitted to review. This document establishes how many hours of that person's time the specification actually consumes.

This is not a scheduling detail. It is the constraint that determines whether the benchmark can exist. A specification that requires more qualified expert hours than are available does not produce a rigorous benchmark; it produces either an unfinished one or a quietly compromised one, because the pressure to relax standards arrives precisely when the reviewing gets tedious.

Writing the number down in advance is what makes the `NOT VALID FOR PRODUCTION PASS` fallback in `REVIEWER_QUALIFICATION.md` an honest option rather than an admission of failure.

## Corpus authoring cost

Before any candidate is evaluated, the corpus must exist. Per `CONTAMINATION_PROTOCOL.md`, the holdout must be **private and expert-authored**, which forecloses bulk import from public datasets for the gating splits.

Item counts required to satisfy registered `min_n` at full mode, per `configs/gate_registry_v0_4.json`:

| Track | Registered n | Unit | Authoring burden |
|---|---:|---|---|
| A Medical QA | 500 | item | full stem, options, answer, rationale, concept IDs |
| Safety holdout (CME) | 500 | high/critical item | as above, plus severity justification |
| B Concept extraction | 300 | passage | passage plus gold concept set |
| C Concept resolution | 600 | pair | pair plus relation label |
| D Relationships | 500 | passage | passage plus gold edge set |
| F Validation | 500 | item | item plus *injected defect* plus defect classification |
| G Cross-subject | 250 | item | item plus primary and supporting concept IDs across disciplines |
| H Fake mastery | 100 | concept | concept specification, not questions (candidate generates those) |
| I Robustness | 300 | base item | base item plus 5 controlled perturbations each |
| J Injection | 300 | item | benign carrier plus crafted attack payload |

Distinct authored artifacts: approximately **3,850**, of which roughly 1,500 (robustness perturbations) are controlled variants rather than original items.

At a realistic 15–25 minutes per original PG-level item including rationale, concept mapping, and provenance capture, and 5 minutes per perturbation variant:

> **Corpus authoring: approximately 800–1,200 hours of qualified medical author time.**

Track C's 600 pairs and Track I's perturbations are cheaper per unit; Tracks A, F, and the safety holdout are more expensive, because a defect-injected validation item requires authoring a *plausible* defect, which is harder than authoring a correct item.

## Review cost

Per `INTER_RATER_AND_HUMAN_REVIEW.md`, gold requires two independent reviews, plus adjudication of disagreements, plus calibration, plus sentinels.

| Activity | Volume | Basis |
|---|---:|---|
| Gold verification, 2 reviewers | ~5,000 review actions | ~2,500 gold-bearing items x 2 |
| Generation rubric scoring | 600 sittings | 300 items x 2 raters, 8 criteria each |
| Calibration sets | ~420 | 30 items x 7 scoring tracks x 2 reviewers |
| Semantic diversity labeled pairs | 400 | 200 pairs x 2, per `SEMANTIC_DIVERSITY.md` |
| Sentinel drift items | ~5% ongoing | inserted per `INTER_RATER_AND_HUMAN_REVIEW.md` |
| Senior adjudication | ~500–900 | assuming 10–15% disagreement |

> **Review and adjudication: approximately 8,000–9,000 discrete review actions, or 400–500 hours.**

Because two independent reviewers are mandatory, this total already counts both reviewers' time. It divides to roughly **200–250 hours each for two qualified reviewers**, before any re-review triggered by gold challenges, threshold recalibration, or a second candidate.

## Total and its implication

> **First full qualification cycle: roughly 1,200–1,700 hours of qualified medical expert time**, of which none is substitutable by the model under evaluation, because a model cannot author or verify the gold it is graded against. That is the non-negotiable rule in the root README.

Re-runs are much cheaper — the corpus is frozen and reused, so subsequent candidates cost only the generation-scoring and adjudication portion, roughly 100–200 hours each.

## The consequence that must be stated plainly

A single reviewer cannot satisfy this specification, and no revision of the specification changes that. Specifically:

1. **Two independent reviewers is structural, not stylistic.** Cohen's kappa is undefined with one rater. `GATE-REL-KAPPA-CRITICAL` is mandatory. A one-person benchmark cannot compute it, therefore cannot satisfy a mandatory reliability gate, therefore cannot issue PASS. This is not a rule that can be waived; it is an arithmetic impossibility.

2. **Self-review does not become independent review by being done twice.** Rating the same item on two occasions produces intra-rater consistency, which measures the stability of one person's judgment, not agreement between judgments. Recording it as kappa would be a fabricated integrity signal — worse than no signal, because it renders as `PASS` on a scorecard.

3. **Qualification is separate from quantity.** Per `REVIEWER_QUALIFICATION.md`, high-severity items require at least one postgraduate-trained clinician or documented subject specialist. An undergraduate medical student, however capable, is below the competency level of PG-level content by definition — the content is post-MBBS material. Authoring it and verifying it are different acts, and the benchmark's validity rests on the second.

## Permitted modes under constraint

The specification anticipates this. `REVIEWER_QUALIFICATION.md` already provides the honest fallback. This document makes the routes explicit.

### Mode 1 — Developmental evaluation (single reviewer)
- Runs any track. Produces per-track diagnostics, confidence intervals, failure matrices.
- Reliability gates are recorded `UNEVALUABLE`, never `PASS`.
- Outcome ceiling: `NOT_VALID_FOR_PRODUCTION_PASS`.
- **Genuinely useful.** It answers "is this architecture obviously broken?" — which is the question that actually matters early, and which does not require a valid PASS to answer.

### Mode 2 — Reduced-scope qualification
- Select **one or two subjects** rather than the full cross-disciplinary corpus.
- Registered `min_n` still applies per gate — reducing n is not permitted, reducing *breadth* is.
- Requires a genuine second qualified reviewer for those subjects only.
- Produces a valid PASS whose scope is explicitly narrow: "qualified for Medicine and Pathology," never "qualified."
- This is the realistic path to a real PASS for a small team.

### Mode 3 — Full qualification
- Requires a reviewer pool, budget or institutional access, and roughly 1,200–1,700 expert hours.
- Not achievable solo. Stating this in the specification is the point.

## Recording requirement

Every run manifest MUST record:

```json
{
  "review_mode": "developmental | reduced_scope | full",
  "reviewer_count": 1,
  "reviewer_qualifications": ["..."],
  "subjects_in_scope": ["..."],
  "kappa_computable": false,
  "max_attainable_outcome": "NOT_VALID_FOR_PRODUCTION_PASS"
}
```

`max_attainable_outcome` is computed from reviewer configuration **before the run starts**, and is printed at the top of the report. A run that cannot produce a PASS should say so before it consumes a budget, not after.
