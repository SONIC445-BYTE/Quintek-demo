# Validator v0.2 — what exists, what it measures, what it has not shown

## State

```
Implementation          COMPLETE
Development testing     PASS
Development evidence    NOT_RUN
Holdout evaluation      NOT_RUN
Human validation        NOT_RUN
Real-model validation   NOT_RUN
Production readiness    NOT_ESTABLISHED
```

Generated, not written: `python3 tools_track_d_status.py --text`. Every field is
computed from the corpora on disk, the holdout ledger and the recorded runs, and
`validator_production_status` has one code path — `_production_status()` — whose
input is four preconditions. There is no way to write a better answer into it.

**Track D is built. It is not validated.** Those are different claims and this
repository is arranged so that the second cannot be made by summarising.

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

## Two benchmarks, and this corpus supports one of them

**Synthetic** — can the system detect deliberately planted defects? That is what
these corpora measure, and the ground truth is sound because the defects were
constructed rather than judged.

**Human** — does the system agree with qualified reviewers about the quality of
real questions? That needs reviewers. Nothing here supports it yet, and passing
the synthetic benchmark would not answer it.

Reporting a synthetic pass as "the validator works" answers a question nobody
asked. `human_review` in the status report stays `NOT_RUN` until `label_status`
on the corpus itself says otherwise.

## What has been measured

**The design's ceiling**, from `tools_validator_eval.py ceiling`. Ground-truth
oracles stand in for the models, so this says what the design could do if every
layer were flawless. It is not a result: the runner withholds the gate outcome
on such a run, records it as `kind: ceiling`, and `runs.real_runs()` excludes
it permanently.

**Read the 100/100 carefully.** It means the architecture now carries enough
information to detect all ten planted defect classes. It does not mean the
validator detects them. v0.1 reached 60% because four classes were invisible to
every layer it had — the fix was giving Layer D the generation request, which
changed what information exists in the pipeline, not how well anything reads
it.

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

## The frozen experiment set

An ablation compares three runs, so anything that changes between them
contaminates the comparison — and the contamination is invisible afterwards:

```
A+B+D    -> v0.4
C        -> v0.4.1
A+B+C+D  -> v0.4.2
```

Nothing in the numbers says that happened. So `experiments` captures the
configuration before the first run, hashes it, stamps the digest onto all three
records, and refuses to run under a changed one — naming the field that moved,
because "the configuration changed" is not actionable and "the judge prompt went
from `judge/0.1.0` to `judge/0.2.0`" is. Starting a new set needs `--refreeze`
and a `--note`.

Pinned: the validator source fingerprint, the corpus content hash, all three
prompt versions, the gate thresholds, the judge's confidence floor, model ids
and families, endpoint, sampling parameters, and the experiment definitions.
Not pinned: the clock and the note. **A credential has no field to live in** —
`freeze.build` refuses a configuration containing one, because a manifest is a
committed artifact.

## Complete and incomplete runs are not comparable

A run with outages, or one that did not reach every item, is recorded
`INCOMPLETE`, and every delta that would involve it reports `INCOMPLETE — NOT
COMPARABLE` instead of a number. Subtracting a partial run from a complete one
gives a difference that is mostly the missing items.

This is not hypothetical: the 70B stalled before its control arm finished last
time. That run's numbers are still recorded and still readable — the runner
records outages rather than smoothing them — but they cannot be differenced
against a complete 8B run, and the report says so rather than leaving it to the
reader.

## Two conclusions from one ablation

`validator/ablation.py` returns them as separate keys and prints them under
separate headings:

**Experiment conclusion** — does the independent judge add information the
other layers do not already have? Answered by A+B+C+D minus A+B+D.

**Model conclusion** — is this model fit to be the judge? `DEFERRED`, always,
from a single run. It needs the same frozen configuration against the
alternative, compared on sensitivity, specificity, FP, FN, latency, cost and
edge calibration. And a poor absolute score does not disqualify a model whose
*contribution* was positive: a judge that adds 12 points of sensitivity has
answered the experiment question yes regardless of how it scores alone.

The model conclusion is stated as deferred rather than omitted, because an
omitted conclusion is one the reader supplies.

## Running it

```bash
python3 tools_track_d_status.py --text                      # the state, derived
python3 tools_validator_eval.py ceiling                     # the design's ceiling, free
python3 tools_validator_eval.py layers                      # per-layer, still a ceiling
python3 tools_validator_eval.py experiments --provider X --judge Y
python3 tools_validator_eval.py run --provider X --judge Y  # a single real measurement
python3 tools_validator_review.py sheet --reviewer "Dr A" --out a.jsonl
python3 tools_validator_review.py merge --a a.jsonl --b b.jsonl
```

## Next, in order

Two questions are tangled together and must be separated: **can the validator
work**, and **which model should do the validating**. The first is established
while minimising dependence on the second.

1. **`experiments`, against the development set, with 8B first.** Three
   configurations in one command, under one frozen configuration, each
   recorded. 8B first because this experiment asks what each layer contributes,
   not whether 8B is a good judge — and it is cheap and completes:

   | | layers | what it isolates |
   |---|---|---|
   | 1 | A+B+D | the validator without the layer whose failure mode is agreeing with itself |
   | 2 | C | what the free-answer judge contributes alone |
   | 3 | A+B+C+D | the whole validator |

   Only Layer A is deterministic — B and D are model calls constrained to quote
   their evidence — so experiment 1 is not "the deterministic layers". The
   number that matters is row 3 against rows 1 and 2. "Every layer earns its
   place" was asserted from a ceiling; this is where it is confirmed or
   withdrawn.

2. **The same fixed development set against both model classes.** The 8B is
   fast and currently unacceptable; the 70B was better and stalled. Measure
   sensitivity, specificity, FP, FN, latency, cost and edge calibration for
   each before promoting either. The development corpus tolerates **zero**
   false positives against a 90% threshold, so read the error analysis, not the
   headline rate.

3. **Freeze the configuration.** No prompt edits after this point, and in
   particular none in response to anything the holdout shows. The locator gap
   recorded in the holdout ledger is the worked example of the rule.

4. **Score once against the holdout.** With a note saying what changed. Five
   scoring runs exist, ever.

5. **Qualified human review**, through `tools_validator_review.py`, and report
   the kappa before quoting any validator number that rests on these labels.
   Until this happens the ground truth is model-authored questions with
   injected defects — sound for the synthetic benchmark, silent about medical
   judgement.

6. **Only then** freeze a benchmark corpus and start the 355-model funnel.
   Discovery work can continue in parallel; it is no longer the critical path.
