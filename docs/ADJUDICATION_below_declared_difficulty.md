# Adjudication Audit — `below_declared_difficulty` false positives

Read-only. No external model call, no freeze change, no holdout access, no
Phase 0 re-run, no change to production qualification state.

## Scope

Phase 0 arm 1 (A+B+D) decided 36 clean items and flagged 22. The dominant
check was `conformance/below_declared_difficulty` with **17 items**. This
audit asks one question: are the corpus difficulty labels wrong, or is the
validator's logic wrong?

## How the disputed set was reconstructed

The run record stores only three examples per check, so the full set was
recovered from the Phase 0 journal, which holds every raw reply. For each
clean item in the arm, its `:conformance` reply was re-parsed with the
validator's own `extract_json` and its reported `cognitive_level` compared
against the item's declared `difficulty`.

**16 of the 17 were recovered.** The seventeenth item's conformance call was
one of the run's recorded outages, so no reply exists to re-read. That gap is
stated rather than filled: 16 items are evidenced below, and the conclusion
does not depend on the missing one.

## The rule being applied

`validator/conformance.py`:

```python
RECALL_IS_ACCEPTABLE_AT = ("foundation",)
...
if level == RECALL and difficulty not in recall_acceptable_at:
    checks.append(BELOW_DECLARED_DIFFICULTY)
```

The check is a direct comparison of one declared field against one reported
field. It performs no inference of its own.

## The disputed items

| # | Item | Concept | Stem (truncated) | Key | Declared | Model level | Giveaway also? | Label evidence | Class |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `vd-clean-002` | Frank-Starling mechanism | According to the Frank-Starling mechanism, an increase in ve… | Greater stretch of myocardial fibres i… | pg_entry | recall | no | **none** (model_authored, gold=false, reviewed_by empty) | **B+E** |
| 2 | `vd-clean-004` | Oxygen dissociation curve | A rightward shift of the oxyhaemoglobin dissociation curve i… | Lower affinity for oxygen, favouring u… | pg_entry | recall | no | **none** (model_authored, gold=false, reviewed_by empty) | **B+E** |
| 3 | `vd-clean-010` | Apoptosis | Apoptosis differs from necrosis in that apoptosis characteri… | Preserves plasma membrane integrity an… | pg_entry | recall | no | **none** (model_authored, gold=false, reviewed_by empty) | **B+E** |
| 4 | `vd-clean-014` | Aspirin | The antiplatelet effect of a single dose of aspirin persists… | Cyclo-oxygenase is acetylated irrevers… | pg_entry | recall | no | **none** (model_authored, gold=false, reviewed_by empty) | **B+E** |
| 5 | `vd-clean-016` | Volume of distribution | A drug with a very large apparent volume of distribution is … | Extensively sequestered in tissues rel… | pg_entry | recall | no | **none** (model_authored, gold=false, reviewed_by empty) | **B+E** |
| 6 | `vd-clean-018` | Chlamydia trachomatis | Chlamydia trachomatis cannot be cultured on ordinary bacteri… | An obligate intracellular organism tha… | pg_entry | recall | no | **none** (model_authored, gold=false, reviewed_by empty) | **B+E** |
| 7 | `vd-clean-019` | Diagnosis of typhoid | In the first week of enteric fever, the investigation with t… | Blood culture… | pg_entry | recall | no | **none** (model_authored, gold=false, reviewed_by empty) | **B+E** |
| 8 | `vd-clean-022` | Diabetic ketoacidosis | The high anion gap metabolic acidosis of diabetic ketoacidos… | Accumulation of beta-hydroxybutyrate a… | pg_entry | recall | no | **none** (model_authored, gold=false, reviewed_by empty) | **B+E** |
| 9 | `vd-clean-023` | Atrial fibrillation | Which combination of findings is most characteristic of atri… | An irregularly irregular pulse with ab… | pg_entry | recall | no | **none** (model_authored, gold=false, reviewed_by empty) | **B+E** |
| 10 | `vd-clean-024` | COPD | Spirometric confirmation of chronic obstructive pulmonary di… | A post-bronchodilator FEV1 to FVC rati… | pg_entry | recall | no | **none** (model_authored, gold=false, reviewed_by empty) | **B+E** |
| 11 | `vd-clean-030` | Magnesium sulphate toxicity | During a magnesium sulphate infusion for severe pre-eclampsi… | Loss of the deep tendon reflexes… | pg_entry | recall | no | **none** (model_authored, gold=false, reviewed_by empty) | **B+E** |
| 12 | `vd-clean-033` | Physiological jaundice | Which feature would exclude a diagnosis of physiological jau… | Jaundice appearing within the first 24… | pg_entry | recall | no | **none** (model_authored, gold=false, reviewed_by empty) | **B+E** |
| 13 | `vd-clean-034` | Oral rehydration solution | Oral rehydration solution remains effective in secretory dia… | Glucose-coupled sodium absorption in t… | pg_entry | recall | yes | **none** (model_authored, gold=false, reviewed_by empty) | **B+E** |
| 14 | `vd-clean-035` | Kawasaki disease | Intravenous immunoglobulin is given in Kawasaki disease prin… | Reduce the risk of coronary artery ane… | pg_entry | recall | yes | **none** (model_authored, gold=false, reviewed_by empty) | **B+E** |
| 15 | `vd-clean-039` | Herd immunity | Herd immunity protects unvaccinated individuals in a communi… | Transmission chains are interrupted wh… | pg_entry | recall | yes | **none** (model_authored, gold=false, reviewed_by empty) | **B+E** |
| 16 | `vd-clean-040` | Randomisation | The principal purpose of randomisation in a controlled trial… | Distribute known and unknown confounde… | pg_entry | recall | no | **none** (model_authored, gold=false, reviewed_by empty) | **B+E** |

Every row depends on the same two inputs: the item's `difficulty` field, and
the candidate's `cognitive_level` judgement. Three items were additionally
flagged for a giveaway, and those flags were separately verified — that check
abstains unless the quoted cue is literally present in the question.

## The decisive evidence

Every item in the corpus, all 100 of them, carries this provenance:

```
provenance    : model_authored
gold_standard : false
reviewed_by   : ""            (empty on all 100)
reference     : "Authored for validator development; the supplied
                 source_passage is the sole evidence against which this
                 item is graded."
```

The `difficulty` label on every disputed item was therefore **assigned by a
model when the item was written, never reviewed by a human, and explicitly
marked not gold**. There is no source, rubric or authoring rationale in the
corpus that supports `pg_entry` for any of the 16.

## Classification

| Class | Count | |
|---|---|---|
| A — LABEL SUPPORTED | **0** | no item's label has supporting evidence |
| B — LABEL UNSUPPORTED | **16** | every label is unreviewed model output |
| C — AMBIGUOUS | 0 | not ambiguous; the evidence is absent, not conflicting |
| D — IMPLEMENTATION ISSUE | **0** | the check applies its documented rule faithfully |
| E — DATA/PROVENANCE ISSUE | **16** | `gold_standard: false`, `reviewed_by` empty, corpus-wide |

B and E are the same finding seen from two directions, so every item is
counted in both rather than split arbitrarily between them.

## Root cause

**PROVENANCE.**

Not the validator: `below_declared_difficulty` does exactly what it documents,
and `answerable_from_wording_alone` abstains rather than accepting an
unverifiable claim. Not a resolvable corpus dispute either — a dispute needs
two supported positions, and only one side here has any evidence at all.

The finding is more fundamental than a mislabelling: **this corpus cannot
support a difficulty-conformance claim in either direction.** Measuring
"does the candidate judge difficulty correctly" against labels that are
themselves unreviewed model output compares one model's judgement with
another's and calls the disagreement a defect. Whichever way that comparison
came out, it would not have been evidence.

That the corpus says so itself — `gold_standard: false` on all 100 items — is
the system being honest about its own limits, and it was recorded before this
run.

## One recommended corrective action

**Exclude `below_declared_difficulty` from the qualification gate as
UNEVALUABLE-on-this-corpus, and record the exclusion as a corpus limitation
rather than a validator change.**

Reasons this is the recommendation and not one of the alternatives:

* It removes a measurement the corpus cannot support, instead of moving a
  threshold to obtain a passing number. `RECALL_IS_ACCEPTABLE_AT` stays as
  written; the check keeps running and keeps reporting; it simply stops
  gating a decision it has no gold to gate against.
* It does not edit the corpus, so `corpus_hash` is unchanged.
* It is honest about direction: excluding the check does NOT make the
  candidate qualified. Even with all 17 flags removed, specificity reaches
  roughly (36 − 5) / 36 ≈ 86%, still short of the required 90%, and the
  remaining `answerable_from_wording_alone` flags would need their own
  adjudication.

**This is a recommendation, not a change. Nothing has been implemented.**

## Insufficient evidence, stated plainly

For the underlying question — are these 16 items genuinely `pg_entry` or
genuinely recall — the honest answer is:

**INSUFFICIENT EVIDENCE — NO CHANGE AUTHORIZED.**

Establishing it needs a qualified reviewer applying an explicit difficulty
rubric to each item. Neither the rubric nor the reviewer exists in V1, and no
model may supply either for gold it will be graded against.
