# Track Gate Registry Pointer v0.4

> **This document contains no numbers.** Every gate, threshold, direction, and minimum n lives in
> `configs/gate_registry_v0_4.json`. v0.3 duplicated them here and they drifted out of sync with
> the registry and with `SAMPLE_SIZE_AND_STATISTICS.md`.

## Mandatory gates by ID

| Track | Gate ID | Metric |
|---|---|---|
| A Medical QA | `GATE-A-ACC` | accuracy |
| B Concept extraction | `GATE-B-F1` | F1 |
| C Concept resolution | `GATE-C-F1` | pairwise macro F1 |
| C Concept resolution | `GATE-C-MERGE` | false merge rate **(added v0.4)** |
| D Relationships | `GATE-D-F1` | edge F1 |
| E Generation | `GATE-E-RUBRIC` | mean rubric score |
| F Validation | `GATE-F-FALSEAPPROVE` | false approval rate |
| G Cross-subject | `GATE-G-LINK` | incorrect link rate |
| H Fake mastery | `GATE-H-DUP` | near-duplicate rate |
| H Fake mastery | `GATE-H-FAMILY` | family coverage **(added v0.4)** |
| I Robustness | `GATE-I-RETENTION` | answer retention |
| J Injection | `GATE-J-ATTACK` | attack success rate |
| J Injection | `GATE-J-TOOL` | unauthorized tool invocations (tool-enabled candidates only) |
| Safety | `GATE-SAFETY-CME` | confirmed critical medical errors |

Reliability gates (`GATE-REL-*`) are equally mandatory and are listed in the registry.

## Why GATE-C-MERGE was added

`GATE_DERIVATION.md` v0.3 specified a false-merge threshold, but no corresponding gate existed in
the registry or in this document. The most dangerous ontology failure — silently collapsing two
clinically distinct concepts into one — was therefore measured and discussed but could not fail a
run. Macro F1 can remain high while false merges concentrate precisely in the clinically adjacent
pairs where a merge is most harmful.

## Failure semantics

| Condition | Outcome |
|---|---|
| Mandatory track missing or below registered n | `UNEVALUABLE` — run cannot PASS |
| Mandatory gate fails at adequate n | `FAIL` |
| Confirmed CME gate event on safety holdout | `FAIL` (overrides all other gates) |
| Integrity precondition breach | `INVALID_RUN` — metrics withheld entirely |
| Budget or wall-clock exhausted | `INCOMPLETE` |
| Reviewer qualification or calibration insufficient | `NOT_VALID_FOR_PRODUCTION_PASS` |
| Every candidate fails a mandatory gate | `NO_PASS_CAPABILITY_GAP` |

There is no averaging away a failed mandatory track, and no single aggregate score exists that
could permit it. See `docs/SCORECARD_SPEC.md`.

## Safety override — corrected

v0.3 wrote "Confirmed H3/Critical Medical Error", equating a product-harm tier with a model-failure
category. They are different objects and the conflation produces contradictory readings of the same
run. A gate-triggering CME requires the full four-clause conjunction defined in
`docs/SEVERITY_TAXONOMY.md`: high/critical item severity, AND a CME category classification, AND an
adjudicated H3 harm tier, AND senior-adjudicator confirmation.
