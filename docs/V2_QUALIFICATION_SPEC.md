# V2 Qualification Specification

The next valid qualification experiment. **Not implemented, not scheduled, not
run.** This document exists so that when one is authorized it is a new
experiment with adequate evidence, rather than a repeat of a protocol already
established as incomplete.

Written from what V1 actually failed on. Every requirement below traces to a
specific finding, cited inline.

## The governing rule

**A model may not generate the labels it is then graded against.**

V1's corpus is `provenance: model_authored`, `gold_standard: false`,
`reviewed_by: ""` on all 100 items. That is why `below_declared_difficulty`
could not be adjudicated in either direction: measuring a candidate's
difficulty judgement against unreviewed model-authored labels compares two
models and calls the disagreement a defect. No V2 gold may be produced this
way, and no model — including a stronger one — may substitute for the reviewer.

## 1. What constitutes a gold-standard item

`gold_standard: true` requires **all** of:

| Field | Requirement |
|---|---|
| `provenance` | `expert_authored` or `expert_reviewed`; never `model_authored` alone |
| `reviewed_by` | a named qualified reviewer; empty is disqualifying |
| `reviewed_at` | timestamp of the actual review |
| `difficulty_rationale` | free text stating why this difficulty, citing the rubric clause |
| `reference` | a real source, not "authored for validator development" |
| `review_agreement` | present where two reviewers were required |

An item failing any of these is `gold_standard: false` and **excluded from
gated arms**. It may still be carried for diagnostics.

## 2. Who reviews difficulty

Two independent reviewers meeting `docs/REVIEWER_QUALIFICATION.md`, blind to
each other and to any model output. Disagreements go to senior adjudication
per `docs/CRITICAL_MEDICAL_ERROR.md`: **both original labels are preserved and
never averaged**, and only a senior-adjudicated label becomes gold.

## 3. Difficulty rubric

The rubric must exist as a written artifact **before** any item is labelled,
and each label must cite the clause it rests on. At minimum it must make
`foundation` vs `pg_entry` decidable by a reviewer without seeing the options,
since a rubric that needs the answer key is measuring the key.

`RECALL_IS_ACCEPTABLE_AT` stays as written (`("foundation",)`) unless the
rubric itself is re-specified — deliberately, in advance, and recorded as a
specification change, never in reaction to a failing result.

## 4. Corpus size — derived, not chosen

From V1's own gate arithmetic: **35** clean items are needed for a flawless
run to clear a 90% specificity lower bound, and **53** to clear it while
tolerating a single false positive. V1 supplied 40, which made the gate
reachable only by a perfect arm.

    clean items    >= 60     tolerates 2 false positives with margin
    defective items >= 60    matched pairs, one per clean item
    per defect family >= 8   so a family's miss rate is not one item's noise

Attrition must be budgeted, not hoped away: V1 lost 12–14 items per arm to
unparseable replies. **Size the corpus so the gate is still reachable after
20% attrition**, and abort an arm that exceeds it rather than reporting a
number computed on what survived.

## 5. Defect families

Every family V1 planted must remain, each with >= 8 items: `wrong_key`,
`ambiguous_stem`, `giveaway`, `poor_reasoning`, `two_correct`, `trivial`,
`out_of_syllabus`, `hallucinated_fact`, `hallucinated_reference`,
`ungrounded`. Each item states its mechanism in `defect_note`, as V1's did —
`vd-def-009`'s note is what let the wording-cue defect be diagnosed at all.

## 6. Adjudication and reviewer agreement

Cohen's kappa on the difficulty label and on the clean/defective judgement,
computed by `benchmark/stats.py`, which raises rather than returning a
degenerate number below two raters.

    kappa <  0.67    BLOCKED      the corpus is not usable as gold
    0.67 <= k < 0.80 ACCEPTABLE   usable, recorded as a limitation
    kappa >= 0.80    STRONG

The kappa gate is on the **reviewers**, before any model is evaluated. A
corpus that cannot clear it cannot qualify anything.

## 7. What makes a conformance check evaluable

A check may gate a decision only if **both** hold:

1. **It has gold.** The property it tests is independently established in the
   corpus by the review process above.
2. **It tests the claim it states.** Its detail string and its implementation
   assert the same thing.

Requirement 2 is not hypothetical: `answerable_from_wording_alone` claimed a
cue "selects the keyed option" while verifying only that the cue appeared
somewhere in stem + all options, and 4 of its 8 clean-item flags were
consequently circular or self-contradictory.

A check failing (1) is **UNEVALUABLE**: it runs, it reports, it is excluded
from the gate, and its exclusion is recorded in the run manifest — never
silently dropped to obtain a pass.

For wording-cue checks specifically, V2 needs items whose giveaway is
**designed and annotated** (which span, why it gives the answer away), so the
check has something to be right or wrong against.

## 8. Insufficient evidence on an item

`ABSTAINED`. Excluded from both sensitivity and specificity, counted and
reported. Never silently passed, never counted as a finding. V1's behaviour
here was correct and is retained.

## 9. What permits qualification

**All** of:

- corpus kappa >= 0.67, with < 0.80 recorded as a limitation
- every gated check evaluable per §7
- every arm complete: each reached every item, attrition within budget
- `ABCD - ABD` computed on comparable arms
- sensitivity lower bound >= 0.80 **and** specificity lower bound >= 0.90
- judge independence at Tier 2 or better, family **and** provider
- holdout scored at most once, within MAX_USES

## 10. What prevents it

Any of: an incomplete arm; an unevaluable gated check; kappa < 0.67; a
model-authored gold label; a retired or unqualified candidate; a threshold
changed after seeing a result; or a delta computed across non-comparable arms.

**INCOMPLETE never becomes PASS**, and a poor absolute score does not by
itself disqualify a model whose measured incremental contribution was
positive — those remain two separate conclusions.

## 11. Estimated cost, stated honestly

V1's three-arm set was 765 logical calls at ~31–75 s/call. A corpus of 60/60
roughly doubles that. The expensive input is not compute: it is **two
qualified reviewers adjudicating ~120 items against a written rubric**, which
is the part no model may substitute for.
