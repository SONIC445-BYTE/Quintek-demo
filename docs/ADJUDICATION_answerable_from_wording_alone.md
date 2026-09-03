# Adjudication Audit — `answerable_from_wording_alone` false positives

Read-only. No external model call, no code change, no corpus change, no freeze
change, no holdout access, no Phase 0 re-run.

## What the check claims, and what it verifies

`validator/conformance.py`:

```python
if bool(parsed.get("answerable_from_wording_alone", False)):
    cue = str(parsed.get("wording_cue") or "")
    haystack = stem + " " + " ".join(str(o) for o in options)
    if quote_is_in(haystack, cue):
        checks.append(ANSWERABLE_FROM_WORDING)
        detail.append(f"the wording {cue!r} selects the keyed option without any "
                      "knowledge of the subject")
    else:
        return ABSTAINED   # cue not in the question
```

The claim in the detail string is a **relation**: this wording selects *the
keyed option*. What the code verifies is **presence**: the cue appears
somewhere in the stem *or any option*. Those are not the same test, and the
gap between them is what this audit found.

## The eight flagged CLEAN items

Recovered from the Phase 0 journal. All eight had their cue verified present,
so none abstained.

| # | Item | Concept | Cue claimed | Cue located in | Class |
|---|---|---|---|---|---|
| 1 | `vd-clean-003` | Baroreceptor reflex | `arterial pressure momentarily falls` | stem only, **not** the key | C |
| 2 | `vd-clean-007` | Von Gierke disease | `Glucose-6-phosphatase activity is absent` | stem only, **not** the key | C |
| 3 | `vd-clean-009` | Caseous necrosis | `tuberculous granuloma` | stem only, **not** the key | C |
| 4 | `vd-clean-012` | Metaplasia | `Replacement of the squamous epithelium…` (the whole stem) | stem only, **not** the key | C |
| 5 | `vd-clean-028` | Wound healing | `Occurs only in surgically sutured wounds` | **a distractor only** | **B** |
| 6 | `vd-clean-034` | Oral rehydration | `is preserved` | **the key itself** | **B** |
| 7 | `vd-clean-035` | Kawasaki disease | `Prevent progression to rheumatic carditis` | **a distractor only** | **B** |
| 8 | `vd-clean-039` | Herd immunity | `Transmission chains are interrupted` | **the key itself** | **B** |

Corpus provenance for all eight, as for all 100 items:
`provenance: model_authored`, `gold_standard: false`, `reviewed_by: ""`.
No independent gold exists for "is this item answerable from wording alone".

## Classification

| Class | Count | Basis |
|---|---|---|
| A — validator correctly identifies a wording-only cue | **0** | none of the eight demonstrates a cue that selects the key |
| B — validator implementation appears wrong | **4** | 2 cues lie only in a distractor; 2 cues *are* the key |
| C — insufficient evidence | **4** | cue is in the stem but absent from the key, so the giveaway claim rests on unverified model judgement |
| D — corpus/provenance deficiency | **8** | no gold for the underlying property, corpus-wide |

B and D both apply to the four B items; they are listed under B because the
implementation fault is establishable **without** gold.

## The implementation fault, stated precisely

Two failure modes, both demonstrable from the question text alone:

1. **A cue found only in a distractor cannot select the keyed option.** Items
   28 and 35 quote a wrong option as the giveaway. This directly contradicts
   the check's own detail string.
2. **A cue that *is* the key is circular.** Items 34 and 39 quote the answer
   as the "giveaway wording". Every key is present in its own options, so this
   condition is satisfiable for any item whatsoever.

Item 34's cue is `is preserved` — twelve characters, exactly at the
`quote_is_in` length floor, and a fragment of the key's own text.

## How this differs from the difficulty finding

This is the important distinction, and it goes the other way from the
`below_declared_difficulty` audit.

*Where the cue lies* — stem, key, or distractor — is a **pure string fact
about the question**. It needs no medical judgement, no rubric and no
reviewer. That is why class B could be established here with no gold, whereas
the difficulty dispute could not be settled at all.

*Whether a stem cue genuinely gives the answer away* remains a judgement with
no gold, which is why the four stem-only items are C rather than A.

So: the check is **partially evaluable**. Its verification step is repairable
as an ordinary implementation defect. Its substantive claim is not evaluable
on this corpus.

## Arithmetic

Computed over the 40 clean items with a usable conformance reply. Conformance
flags only — grounding's `explanation_contradicts_passage` (2 items in arm 1)
is excluded, so these are **upper bounds**.

| Configuration | Clean passing | Max specificity |
|---|---|---|
| As run | 19 / 40 | **47.5 %** |
| Remove `below_declared_difficulty` only | 32 / 40 | **80.0 %** |
| Remove both unevaluable checks | 40 / 40 | **100 %** (conformance contributes nothing) |

1. Flags audited: **8** clean items (record reports 11 in arm 1; the
   difference is items whose conformance call was an outage, so no reply
   exists to re-read).
2. A = 0, B = 4, C = 4, D = 8.
3. Max specificity removing `below_declared_difficulty` only: **80.0 %**.
   *This corrects the ~86 % estimate in the previous audit; the precise figure
   is lower and strengthens the same conclusion.*
4. Max specificity removing every genuinely unevaluable check: **100 %** from
   conformance, so roughly **95 %** once grounding's 2 flags are restored.
5. **Could a candidate theoretically satisfy the 90 % gate?** Only if BOTH
   checks are excluded. Removing `below_declared_difficulty` alone caps
   specificity at 80 %, below the gate. And excluding both means Layer D
   contributes nothing to specificity at all — the arm becomes effectively
   A+B, which is a material change to what the experiment measures, not a
   tidy-up.

## Conclusion

The qualification gate is not currently blocked by model quality. It is
blocked by two checks that this corpus cannot evaluate, one of which also has
a repairable implementation fault.

Nothing has been implemented. No gate, check, threshold, corpus or freeze has
been modified.
