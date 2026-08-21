# Validator v0.2 — what exists, what it measures, what it has not shown

## The problem this is for

A measured single-model validator (an 8B checkpoint, one prompt, "is this
question good") produced:

- **11 of 20** planted defects caught — sensitivity 55%
- **9 of 10** clean items false-flagged — specificity 10%

A validator with a 90% false-flag rate rejects most of the good questions the
generator produces. It cannot be improved by trying a bigger model until there
is something to measure improvement against, which is what Track D builds.

## What is here

| Piece | File | What it is |
|---|---|---|
| Development corpus | `corpus/validator_dev/` | 100 cases: 40 clean, 40 controlled defects, 20 ambiguous/edge |
| Holdout corpus | `corpus/validator_holdout/` | 93 cases: 53 clean, 30 controlled defects, 10 edge |
| Defect injection | `validator/mutate.py` | One controlled failure at a time, enforced |
| Layer A — structural | `validator/structural.py` | Deterministic. No model. No false-flag rate |
| Layer B — grounding | `validator/grounding.py` | Is the key supported by the supplied passage |
| Layer C — judge | `validator/judge.py` | An independent answer, from a model that did not write the item |
| Layer D — conformance | `validator/conformance.py` | Is this the question that was asked for |
| Pipeline | `validator/pipeline.py` | The four layers, every flag attributable to a layer |
| Metrics and gate | `validator/metrics.py` | Two-armed gate on the lower bound of both intervals |
| Error analysis | `validator/analysis.py` | False positives by check, false negatives by defect class, matched pairs |
| Two-reviewer protocol | `validator/review.py` | Blind sheets, Cohen's kappa, adjudication |
| Holdout discipline | `validator/holdout.py` | One score per validator, a budget, an append-only ledger |

Runners: `tools_validator_eval.py` (ceiling / layers / run) and
`tools_validator_review.py` (sheet / merge / apply).

## The corpus

**Clean items** are passage-grounded: each carries a `source_passage` that
supports its key. That makes the grounding question — "is the key supported by
the evidence?" — well defined and checkable by a person in a minute.

**Defective items** are derived, not written. Each is one clean item with one
edit, and the mutation declares which fields it may touch; `mutate.apply`
computes what actually changed and refuses if the edit exceeded its
declaration. The source passage may be changed only by the `ungrounded`
operation, because the passage is what "correct" means.

This buys matched pairs: the clean item and its defective twin differ in one
respect, so a validator that flags one and passes the other is responding to
the defect and not to the subject, the phrasing or the length. It costs
external validity: real production defects do not arrive with a clean twin, and
a validator that scores well here has been measured on an easier
discrimination than production will ask of it.

**Edge cases** are scored in neither arm. Folding them in corrupts both:
counted as clean they inflate the false-flag rate of a validator that was
arguably right, and counted as defective they credit it for catching something
that may not be a defect. What they measure is calibration — whether the
validator abstains where people disagree.

**Provenance.** Every item is `model_authored`, `gold_standard: false`,
`label_status: unreviewed`. That is what the corpus module permits for a
development set and it is all this corpus claims. Two named clinicians running
`tools_validator_review.py` is what would change it; nothing else does.

## Which layers must be independent of the generator, and which need not be

Only Layer C. It asks the model for a free answer, and a model asked to answer
a question it wrote will reproduce its own answer — so `judge.assert_independent`
refuses when the judge is the item's author or a sibling from the same family,
and it raises rather than warning.

Layers B and D are different. Neither asks for an opinion: B asks which options
the passage supports **and must quote the passage for each**, D asks what
concept and cognitive level the item demands **and must quote the cue it
claims**. Every quotation is searched for in the material, and a span that is
not there abstains the layer rather than deciding. A model cannot agree with
itself past a check that requires it to produce evidence it did not write.

That is the argument for allowing B and D to share a provider with the
generator, and it is an argument, not a measurement. If the quote checks turn
out to be weaker than this claims, the fix is to require independence there
too — the parameters already exist.

## The gate

```
sensitivity >= 80%   AND   specificity >= 90%
```

judged on the **lower bound of the 95% Wilson interval**, not the point
estimate, with three outcomes: `PASS`, `FAIL`, `INSUFFICIENT_EVIDENCE`.

Two consequences that are easy to miss and expensive to find late:

1. **A detector that flags everything scores 100% sensitivity.** It fails here,
   on the specificity arm, which is why both arms are required.

2. **An arm can be too small for its own threshold.** Thirty clean items, every
   one correctly passed, give a lower bound of 88% — which does not establish
   90% at *any* level of performance. That is not a FAIL, it is insufficient
   evidence, and `metrics.min_items_for` reports the number that would settle
   it:

   | threshold | perfect run | tolerating 1 mistake | tolerating 2 |
   |---|---|---|---|
   | sensitivity 80% | 16 | 25 | 33 |
   | specificity 90% | 35 | 53 | 69 |

   The holdout's clean arm is 53 and its defective arm 30, so it can certify a
   validator that makes at most one false positive and one miss. The
   development set's clean arm is 40, which tolerates zero false positives —
   it is not the gate, and this is why.

## What has been measured

**The design's ceiling**, from `tools_validator_eval.py ceiling`. Ground-truth
oracles stand in for the models, so this says what the design could do if every
layer were flawless. It is not a result and the runner withholds the gate
outcome on such a run.

| layers | sensitivity | specificity | matched pairs |
|---|---|---|---|
| A alone | 10% | 100% | 10% |
| B alone | 50% | 100% | 50% |
| C alone | 30% | 100% | 30% |
| D alone | 30% | 100% | 30% |
| A+B | 60% | 100% | 60% |
| A+B+C | 70% | 100% | 70% |
| **A+B+C+D** | **100%** | **100%** | **100%** |

Every layer earns its place; a test fails if turning any one off leaves
sensitivity unchanged.

**v0.1 had a ceiling of 60%.** Layers A, B and C had no check at all for four
of the ten defect classes — `out_of_syllabus`, `poor_reasoning`, `giveaway`,
`trivial` — because nothing about those items is wrong in isolation. The
missing input was the *request*: the concept, difficulty and type that were
asked for. That is what Layer D takes, and it is why it exists. Finding a
ceiling below the pass threshold cost nothing here; finding it from a model
bill would have been the expensive way.

## What has NOT been measured

**No validator has been scored against the holdout.** Its ledger
(`corpus/validator_holdout/ledger.jsonl`) contains one `inspection` entry and
no `score` entries, and that entry records the one thing that has already
leaked: a ceiling run showed the deterministic locator check missing one
planted `hallucinated_reference` item because the pattern does not include the
word "clause". The pattern was **not** widened in response — a check widened to
catch an item the holdout revealed is a check tuned on the holdout.

**No real model has been run through the pipeline.** `tools_validator_eval.py
run --provider <name> --judge <name>` does that and refuses when the two are
the same model.

**The corpus has not been reviewed by anyone.** See provenance above.

## Two findings worth keeping

**A deterministic giveaway check was built, measured, and removed.** Distinctive
words shared between the stem and the keyed option and absent from every
distractor flagged 5 of 40 clean items and caught 2 of 4 planted giveaways —
rejecting good questions at eight times the rate it caught bad ones.
Deterministic word overlap cannot distinguish a cue from a topic. The question
is asked of a model in Layer D instead, phrased as "could this be answered
without medical knowledge", with the cue quoted and checked.

**Layer A's "no false-flag rate" claim was untrue and is now true.** The option
normaliser stripped every non-alphanumeric character, so `Na - (Cl + HCO3)` and
`(Na + Cl) - HCO3` both reduced to `na cl hco3` and were reported as the same
answer written twice. It fired on a real item in `corpus/development.jsonl`.
Operators are now preserved in option comparison; word-internal hyphens are
still ignored.

## Running it

```bash
python3 tools_validator_eval.py ceiling                    # the design's ceiling, free
python3 tools_validator_eval.py layers                     # per-layer contribution
python3 tools_validator_eval.py run --provider X --judge Y  # a real measurement
python3 tools_validator_review.py sheet --reviewer "Dr A" --out a.jsonl
python3 tools_validator_review.py merge --a a.jsonl --b b.jsonl
```

## Next, in order

1. Run a real provider pair through `run` on the development set, and read the
   error analysis rather than the headline rate.
2. Iterate the prompts against the **development** set only, until the
   development numbers clear both arms with room to spare.
3. Score **once** against the holdout, with a note saying what changed. Five
   scoring runs exist, ever.
4. Two clinicians through `tools_validator_review.py`, and report the kappa
   before quoting any validator number that rests on these labels.
5. Only then freeze a benchmark corpus and start the 355-model funnel.
