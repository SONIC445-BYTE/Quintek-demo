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

## Seats, layers, and the word that was removed

A run record has to answer "which model was being evaluated" without ambiguity,
so two axes are recorded separately:

| | means |
|---|---|
| **seat** | the experimental role — `candidate` (the model under evaluation) or `judge` (the independent model brought in to disagree with it) |
| **role** | the layer the provider was called for — `grounding`, `judge`, `conformance` |
| **provider** | the adapter that builds a model, and *only* that |

The candidate occupies the grounding and conformance layers; the judge occupies
one. Both appear in `ProviderRecord` and in the freeze manifest.

There is deliberately no `--provider` flag. Using the word for both "the adapter"
and "the model under evaluation" makes every record ambiguous about the thing it
exists to say. Passing `--provider` is an **error naming its replacement**, not a
silent alias — a silent alias in a permanent record is worse than a break.

Seats are written `provider:model_id`, because the usual case is two different
models on one endpoint. The credential is not part of a seat spec: the NVIDIA
builder reads its key from the environment by name, so the key stays out of the
spec, out of the manifest and out of the run record.

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

## Running a subset: --only

`experiments --only ABD,C,ABCD` (any subset of the real layer codes) runs only
the named experiments instead of all three. Two things stay true whichever
subset runs:

**The frozen configuration always describes all three.** The freeze manifest's
`experiments` field is built from the fixed set, not from `--only`, so a
`--only C` invocation and a full run produce the same digest, and a later
invocation for the remaining experiments is not refused as a changed
configuration. That is what makes `--only C` now, `--only ABD,ABCD` later, a
natural way to run the set in stages rather than a way to fork it.

**Each experiment's row is unchanged by what else runs alongside it.** The
forecast, the budget check, and the run record for `--only C` cover only the
100 judge calls that experiment needs — not the full set's 765/195 — and that
row is identical to the one a full run would print for the same experiment.
This is why the flag exists: a budget sized for one arm evaluated against the
whole set's total is exactly the trap `BUDGET TOO SMALL FOR THE MEASUREMENT`
was built to catch, and would refuse a `C`-only run that was never asking for
765 calls in the first place.

## What the run will cost, before it costs it

Layer A is deterministic and free, so the number of items that never reach a
model is knowable in advance by running it. Everything after that is a fixed
number of requests per item. `tools_validator_eval.py forecast` therefore
computes the cost exactly rather than estimating it:

```
items 100; 5 stopped by Layer A before any model call; 95 reach B/C/D

experiment      candidate   judge
1  A+B+D              285       0
2  C                    0     100
3  ABCD               285      95
TOTAL PLANNED         570     195      (765 logical calls)
```

**Experiment 1 spends nothing in the judge seat.** A judge failure cannot cost
it, which is why it runs first.

**Planned is not spendable.** The frozen configuration includes a retry policy,
so a logical call that succeeds on its third attempt sends three requests. At
`max_retries=2` the 765 planned calls are up to **2295 outbound attempts**, 585
of them in the judge seat. `--max-calls` and `--max-judge-calls` are expressed
in outbound attempts — the same unit the meter counts — because a budget
compared against the planned figure looks comfortable and is not.

The forecast prints three verdicts:

| | |
|---|---|
| `WITHIN BUDGET` | even the worst case fits |
| `WILL EXCEED` | the measurement fits, the worst case does not. The run may stop early; a stopped arm is `INCOMPLETE`, not a lower score |
| `BUDGET TOO SMALL FOR THE MEASUREMENT` | the plan alone exceeds the budget, so no arm could complete. The runner refuses to start |

A run using real models refuses to start without **both** budgets and
`--confirm-spend`. Discovering the cost halfway through is what the forecast
exists to prevent.

## The budget is counted where the money leaves

`BaseProvider.generate` retries around `self._call`, and `_call` is where the
HTTP request is made. A budget placed around `generate` would count one logical
question and permit three requests. The meter therefore sits at `_call` for any
provider that implements it, so retries are counted, and the run record says
which boundary was used (`outbound_attempt`, or `logical_call` for the test
doubles that override `generate`) rather than asserting it was the right one.

**One unit, not one per provider.** `outbound_attempt` is the canonical unit
for any real benchmark run, and `meter` **refuses** a real model that does not
implement `_call` rather than quietly counting it at `generate` — otherwise two
records could carry "300 calls" under the same heading while differing by the
retry policy. Test doubles are counted logically, which costs nothing and is
recorded as `logical_call`, and a run carrying that unit is excluded from
`real_runs()` on top of the exclusions it already fails.

**The budget is yours to choose.** The forecast prints planned and worst-case
figures for both seats and stops there. Setting `--max-calls 1710
--max-judge-calls 585` says "I will pay for the worst case"; setting 1000/300
says "I will pay this much, and if retries exhaust it the experiment becomes
INCOMPLETE". Both are defensible. What is not is a budget that looks sufficient
against 765 while ignoring retry spend — which is why the verdict compares
against the worst case.

Exhaustion introduces **no new outcome**. `spend` raises, the provider's retry
loop records an error, the layer raises its `Unavailable`, the runner counts an
outage, and the arm is `INCOMPLETE` with no delta. A deliberately stopped
experiment is still an incomplete experiment. Exhaustion also does not consume
budget: once over the ceiling `spend` raises without incrementing, so the spent
figure in the record is the number of requests that actually went out.

## A wall-clock ceiling, independent of the call budget

The candidate/judge endpoint is a shared, free-tier, queue-behind-other-tenants
one (`benchmark/providers/nvidia.py`): 0.6s for `GET /v1/models`, 72.9s and
separately 180.8s for near-identical 8-token completions on the same model,
measured on the same day. Every call-count ceiling in this document can be
respected and the run can still take hours, because none of them bound time.

`--max-wall-minutes <N>` does. Once N minutes have elapsed since the
invocation started, no new call is started — a call already in flight
finishes. Same treatment as `--max-calls`: the arm in progress becomes
`INCOMPLETE` with no delta, not a lower score, and the whole invocation stops
starting further experiments once the ceiling is crossed (the arm that was
mid-run when it tripped is still recorded, including as an incomplete one; the
NEXT experiment in the invocation does not start at all, rather than being
started only to fail every call immediately).

It is a genuinely separate mechanism from the call budget, in `validator/wallclock.py`
rather than a field on `Budget` — different question, different file. Checked
at the same choke point the meter already checks (`budget.meter`'s optional
`clock=` parameter, default `None`, so every existing call site is unaffected).
No default; the forecast prints `NO WALL-CLOCK CEILING SET` explicitly rather
than letting an unset ceiling pass as silent, unbounded running — the same
philosophy as the money verdicts, reported as a fully separate line so setting
one does not imply anything about the other.

## The credential is a name, not a value

`credential_ref` records the *environment variable name* — `NVIDIA_API_KEY` —
in both the run record and the freeze manifest. Enough to reproduce the run,
useless to anyone who takes the file.

Two layers of protection, because the schema will grow. The manifest refuses
any field named like a credential, and it also refuses any *value* shaped like
one — `nvapi-`, `sk-`, `Bearer …`, AWS keys, JWTs — so a credential arriving in
a field nobody thought to forbid is still caught.

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

## Phase 3: the kappa gate is enforced, not advisory

`review.merge(..., min_kappa=review.MIN_KAPPA_PHASE_3)` refuses
`usable_for_scoring` when Cohen's kappa is below 0.67 — the remediation-band
floor below which two reviewers are not reliably labelling the same thing, and
a corpus scored against their labels measures the labelling, not the
validator. This used to be a printed warning below 0.6; it is now a refusal at
0.67, matching the Phase 3 specification exactly:

| observed kappa | band | passes the gate |
|---|---|---|
| < 0.67 | blocked | no |
| = 0.67 | acceptable | yes |
| 0.67 – 0.80 | acceptable | yes |
| ≥ 0.80 | strong | yes |

The 0.80 band is descriptive only — nothing in the current specification
defines it as a second, separate gate, so it labels a passing kappa rather
than raising the bar.

The gate is **opt-in at the library level** (`min_kappa=None` by default, so
every existing caller of `merge()` is unaffected) and **on by default at the
CLI level** — `tools_validator_review.py merge` and `apply` both default
`--min-kappa` to 0.67. `apply` **refuses to write settled output** when kappa
is below the floor, exactly as it already refuses on a disputed or unanswered
item; pass `--min-kappa -1` to disable the gate and inspect what would have
been written anyway. The measured kappa is always preserved in the report,
whichever way the gate goes — refusing to score is not the same as hiding the
number that caused the refusal.

The gate does not override the existing dispute machinery: a fully-agreed,
high-kappa review set with one unresolved disagreement is still blocked by
that disagreement, not waived by good agreement elsewhere. And the gate never
touches `kappa()` itself, `load_sheet`, `template`, or how a disagreement is
adjudicated — it only decides whether a measured, unmodified kappa is acted on.

## Phase 0 is BLOCKED: the frozen pairing was retired

Measured 2026-08-28, against the live endpoint:

```
meta/llama-3.1-8b-instruct    HTTP 410  end of life 2026-08-26T09:00:00Z
meta/llama-3.1-70b-instruct   HTTP 410  end of life 2026-08-26T09:00:00Z
```

Both seats of "the first set is 8B candidate, 70B judge" are gone. The
credential works — a 410 is the host answering — and no experiment has been
run, no configuration has been frozen, and the holdout has not been touched.

Nothing here picks a replacement. The experiment set exists to make three rows
comparable; an arm run against a substituted model is not the arm the other
rows were measured with, so establishing a new pairing is an explicit decision
recorded with `--refreeze` and a `--note` saying which models and why. That is
the existing mechanism and it is deliberately manual.

`tools_validator_eval.py experiments` now refuses to start against a seat the
provider has withdrawn, naming the seat and the evidence, before any budget is
committed. The check reads `benchmark/discovery.py`'s registry and says nothing
when no registry exists on disk — a missing registry means discovery has not
run here, which is not evidence that the models are fine. See
`docs/MODEL_DISCOVERY.md`.

The scientific question is unchanged by the retirement: does Layer C earn its
place as the independent component of the complete validator?

## Running it

```bash
python3 tools_track_d_status.py --text                      # the state, derived
python3 tools_validator_eval.py ceiling                     # the design's ceiling, free
python3 tools_validator_eval.py layers                      # per-layer, still a ceiling
python3 tools_validator_eval.py forecast --max-calls 2400 --max-judge-calls 600
python3 tools_validator_eval.py experiments \
  --candidate 'nvidia:meta/llama-3.1-8b-instruct' \
  --judge     'nvidia:meta/llama-3.1-70b-instruct' \
  --endpoint  https://integrate.api.nvidia.com/v1 \
  --credential-env NVIDIA_API_KEY \
  --max-calls 2400 --max-judge-calls 600 --confirm-spend \
  --note "first development experiment set"
python3 tools_validator_eval.py run --candidate ... --judge ...   # one measurement
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

   **The first set is 8B candidate, 70B judge.** Not two 8B checkpoints: Layer
   C exists to supply a judgement the candidate did not produce, and "different
   checkpoint, same family" is not that. The 70B may stall again — it did last
   time, before its control arm finished. That is an acceptable outcome, and it
   is the reason the `INCOMPLETE — NOT COMPARABLE` machinery exists. A stalled
   arm is recorded with its numbers and excluded from every delta, and it is
   **not** retried selectively, because a retry that changes only the arm that
   failed turns a stall into a score.

2. **The same fixed development set against the alternative pairing.** Measure
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
