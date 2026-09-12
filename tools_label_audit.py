#!/usr/bin/env python3
"""
Find CLEAN items whose explanation makes a checkable claim about the passage.

WHAT THIS IS, AND WHAT IT IS EMPHATICALLY NOT
----------------------------------------------
This is an AUDIT. It narrows 40 items to the handful worth a clinician's
attention and isolates the exact sentence to look at. It does not rule on
anything, it does not change a label, and it has no opinion that anyone should
act on.

Adjudication is `validator/review.py`: two named reviewers who cannot see each
other's answers, kappa reported before any validator is scored. That module
refuses to record a review that would be misleading, and nothing here is a
substitute for it. The output below is a worksheet to make that review cheaper,
not a shortcut past it.

WHY IT EXISTS
-------------
The first complete ABD run flagged one clean item, `vd-clean-016`, under
`grounding/explanation_contradicts_passage`. The check was right. The passage
defines the apparent volume of distribution as the amount of drug IN THE BODY
over plasma concentration; the explanation says the passage defines it as the
ratio of DOSE to plasma concentration. Those are different quantities -- equal
only at t=0 for an IV bolus with complete bioavailability -- and the
explanation attributes the wrong one to a passage sitting right there.

So the item was not a false positive. It was a defect in an item labelled
CLEAN. And the whole development set carries:

    label_status : unreviewed        reviewers  : []
    gold_standard: false             provenance : model_authored

Every label is unverified model output. One was wrong. The clean arm must
reach ZERO false positives for the gate's specificity threshold to be
reachable at 40 items, so a second mislabelled clean item makes that
unreachable no matter how good the validator is -- and no amount of work on
the validator would show it. That is what this audit is for: find the
candidates BEFORE they cost another run, and hand them to someone qualified.

WHAT MAKES AN ITEM A CANDIDATE
------------------------------
Three constructions, all mechanical, all checkable by eye in seconds:

  attribution   the explanation says "the passage defines/states/gives X".
                That is a claim ABOUT a text in the item, so it is either
                right or wrong, and `vd-clean-016` is what wrong looks like.
  arithmetic    the explanation asserts a number the passage does not contain,
                usually by applying a formula the passage states. Often
                correct and good practice -- flagged because the grounding
                check has no category for "entailed but not present" and may
                report it as a contradiction.
  options       the explanation makes claims about numbered options, which the
                passage never mentions by number.

A flag is NOT an accusation. Most of these will be fine. The signal is
"someone should look", and the ruling column is left blank on purpose.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from validator.devset import CLEAN, load

#: "the passage <verb>" -- an assertion about the item's own passage.
ATTRIBUTION = re.compile(
    r"\bthe passage\s+(defines|states|gives|says|identifies|explains|describes|"
    r"lists|notes|reports|specifies)\b", re.I)

#: Numbers, including percentages and decimals.
NUMBER = re.compile(r"\b\d+(?:\.\d+)?\s*%?")

#: "options 2 to 4", "option 3", "options 2, 3 and 4".
OPTION_REF = re.compile(r"\boptions?\s+\d", re.I)

#: Words too common to carry meaning in an overlap comparison.
STOPWORDS = frozenset("""
a an and are as at be been being by for from has have in into is it its of on
or that the their then there these this to was were which with so than but not
""".split())

#: The one item a real run flagged and a reader confirmed. Named here so the
#: worksheet can report where the heuristic actually ranks it instead of
#: claiming the heuristic works.
WORKED_EXAMPLE = "vd-clean-016"

ATTRIBUTION_CLAIM = "attribution"
ARITHMETIC_CLAIM = "arithmetic"
OPTION_CLAIM = "options"


def sentences(text: str) -> list[str]:
    """Good enough for prose written for exam candidates."""
    parts = re.split(r"(?<=[.;])\s+", (text or "").strip())
    return [p.strip() for p in parts if p.strip()]


def stem(word: str) -> str:
    """
    Crudest possible suffix stripping, and deliberately so.

    Without it `tissues` and `tissue`, `extensively` and `extensive` read as
    different words, and every claim comes back with a list of "unbacked" words
    that are plainly in the passage in another form. That noise is worse than
    no list: a reviewer scanning 28 items stops reading a column that cries
    wolf. Only the endings that actually caused false signal here are stripped.
    """
    for suffix in ("ically", "ingly", "edly", "ies", "ing", "ely", "ed", "ly", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            base = word[:-len(suffix)]
            if suffix == "ies":
                return base + "y"
            # `tissues` -> `tissu` but `tissue` -> `tissue` would still differ,
            # so a trailing e goes too. This can over-match, which HIDES a
            # signal rather than inventing one -- the safer direction for a
            # worksheet whose job is to be believed.
            return base[:-1] if base.endswith("e") and len(base) > 4 else base
    return word[:-1] if word.endswith("e") and len(word) > 4 else word


def content_words(text: str) -> set[str]:
    """Stems, for comparison. Never for display -- see `surface_forms`."""
    return {stem(w) for w in re.findall(r"[a-z]+", (text or "").lower())
            if w not in STOPWORDS and len(w) > 2}


def surface_forms(text: str) -> dict[str, str]:
    """
    stem -> the word as it was actually written.

    Comparison happens on stems; a human reads this worksheet, and a column
    listing `defin`, `inflat` and `stat` is a column that looks broken and gets
    ignored. So the matching is stemmed and the display is not.
    """
    out: dict[str, str] = {}
    for word in re.findall(r"[a-z]+", (text or "").lower()):
        if word not in STOPWORDS and len(word) > 2:
            out.setdefault(stem(word), word)
    return out


#: The attribution verbs themselves, stemmed, so they are excluded from the
#: "words the passage does not use" column. They are the construction being
#: detected, not a claim being made.
ATTRIBUTION_VERBS = {stem(v) for v in (
    "passage", "defines", "states", "gives", "says", "identifies", "explains",
    "describes", "lists", "notes", "reports", "specifies", "option", "options")}


def closest_passage_sentence(claim: str, passage: str) -> tuple[str, float]:
    """
    The passage sentence the claim is most likely talking about, by content-word
    overlap. Crude on purpose: its job is to put the right two sentences next to
    each other on the page, not to decide anything.
    """
    claim_words = content_words(claim)
    best, best_score = "", 0.0
    for sentence in sentences(passage):
        words = content_words(sentence)
        if not words or not claim_words:
            continue
        score = len(claim_words & words) / len(claim_words | words)
        if score > best_score:
            best, best_score = sentence, score
    return best, best_score


def audit_item(case) -> list[dict]:
    """Every checkable claim in one item's explanation. May be empty."""
    item = case.item
    passage = (item.source_passage or "").strip()
    explanation = (item.explanation or "").strip()
    if not explanation:
        return []

    passage_numbers = {n.strip() for n in NUMBER.findall(passage)}
    passage_words = content_words(passage)
    found = []

    for sentence in sentences(explanation):
        if ATTRIBUTION.search(sentence):
            referent, overlap = closest_passage_sentence(sentence, passage)
            # The discriminator that caught vd-clean-016: content words the
            # claim asserts which the passage nowhere uses. "dose" was the one.
            written = surface_forms(sentence)
            unbacked = sorted(written[k] for k in
                              content_words(sentence) - passage_words - ATTRIBUTION_VERBS
                              if k in written)
            found.append({
                "kind": ATTRIBUTION_CLAIM, "claim": sentence,
                "passage_says": referent, "overlap": round(overlap, 3),
                "words_not_in_passage": unbacked,
                "why": ("the explanation attributes a definition or statement to the "
                        "passage; compare the two sentences and decide whether the "
                        "attribution is accurate"),
            })

        numbers = {n.strip() for n in NUMBER.findall(sentence)}
        unbacked_numbers = sorted(numbers - passage_numbers)
        if unbacked_numbers and not OPTION_REF.search(sentence):
            found.append({
                "kind": ARITHMETIC_CLAIM, "claim": sentence,
                "passage_says": closest_passage_sentence(sentence, passage)[0],
                "numbers_not_in_passage": unbacked_numbers,
                "why": ("the explanation asserts a figure the passage does not contain, "
                        "usually by applying a rule the passage states. Often correct "
                        "and good practice: flagged because the grounding check has no "
                        "category for a claim the passage ENTAILS but does not carry, "
                        "so it may be reported as a contradiction"),
            })

        if OPTION_REF.search(sentence):
            found.append({
                "kind": OPTION_CLAIM, "claim": sentence,
                "passage_says": "",
                "why": ("the explanation makes a claim about numbered options; the "
                        "passage never refers to options by number, so a checker "
                        "comparing the two has nothing to match against"),
            })
    return found


def build(corpus: str) -> dict:
    devset = load(corpus)
    clean = [c for c in devset.cases if c.label == CLEAN]
    rows = []
    for case in sorted(clean, key=lambda c: c.id):
        claims = audit_item(case)
        if claims:
            # Rank by how much the explanation asserts that the passage does not
            # carry. It is a reading ORDER, not a score: a reviewer with an hour
            # should spend it at the top of the list rather than alphabetically.
            claims.sort(key=lambda c: -(len(c.get("words_not_in_passage") or [])
                                        + 2 * len(c.get("numbers_not_in_passage") or [])))
            rows.append({"id": case.id, "label": case.label,
                         "label_status": case.label_status,
                         "reviewers": list(case.reviewers or []),
                         "subject": case.item.subject,
                         "passage": (case.item.source_passage or "").strip(),
                         "explanation": (case.item.explanation or "").strip(),
                         "claims": claims,
                         "ruling": "", "ruled_by": "", "note": ""})
    rows.sort(key=lambda r: -sum(len(c.get("words_not_in_passage") or [])
                                 + 2 * len(c.get("numbers_not_in_passage") or [])
                                 for c in r["claims"]))
    for rank, row in enumerate(rows, 1):
        row["reading_order"] = rank
    by_kind: dict[str, int] = {}
    for row in rows:
        for claim in row["claims"]:
            by_kind[claim["kind"]] = by_kind.get(claim["kind"], 0) + 1
    worked = next((r["reading_order"] for r in rows if r["id"] == WORKED_EXAMPLE), 0)
    return {
        "corpus": corpus,
        "worked_example": WORKED_EXAMPLE,
        "worked_rank": worked,
        "clean_items": len(clean),
        "items_flagged": len(rows),
        "claims_flagged": sum(len(r["claims"]) for r in rows),
        "by_kind": dict(sorted(by_kind.items())),
        "labels_are_unreviewed": all(c.label_status == "unreviewed" for c in clean),
        "items": rows,
    }


HEADER = """# Clean-label audit — {corpus}

**This is not an adjudication.** Nothing here rules on any item and no label has
been changed. `{items_flagged}` of `{clean_items}` clean items carry a claim worth a
clinician's eye; the rest were not flagged by any of the three tests below.

Every label in this corpus is `label_status: unreviewed`, `gold_standard: false`,
`provenance: model_authored`, with zero reviewers. That is the reason this
worksheet exists — not a suspicion about any particular item.

To record a ruling, use `validator/review.py` (two named reviewers, blind, kappa
reported). Writing in the **Ruling** boxes below is a note, not a review.

## The worked example

`vd-clean-016` was flagged by the validator as a false positive under
`grounding/explanation_contradicts_passage`. Reading it, the check was right:

> **Passage:** The apparent volume of distribution is the amount of drug **in the
> body** divided by its plasma concentration.
>
> **Explanation:** The passage defines the apparent volume of distribution as the
> ratio of **dose** to plasma concentration…

Amount-in-body over Cp and dose over Cp are different quantities; they coincide
only at t=0 for an IV bolus with complete bioavailability. The explanation also
attributes the wrong definition to a passage in the same item, which is a claim
about a text, not a matter of emphasis.

**That is one reader's view, offered for calibration, and it is not a ruling.**
It is included so a reviewer can see what the tests are aiming at and judge
whether they are aimed correctly.

### How well the reading order works — measured, not claimed

`vd-clean-016` is the only item here anyone has confirmed is wrong, and it sits
at **position {worked_rank} of {items_flagged}** in the order below.

So the order is a weak proxy and must not be read as severity. It ranks by how
many words and figures an explanation asserts that its passage does not carry,
and the thing that made `vd-clean-016` wrong was a **single** word — *dose*
where the passage says *amount of drug in the body* — in a sentence that was
otherwise well grounded. Counting cannot see that, and the order was
deliberately not tuned to put the known answer first: fitting the one case we
know would tell us nothing about the twenty-seven we do not.

Read it as a queue for an hour you have, not a ranking. A clean sweep of all
{items_flagged} is the only thing that settles the arm.

## What was tested

| Test | Meaning |
|---|---|
| `attribution` | the explanation says "the passage defines/states/gives X" — a claim about a text in the item, so it is either accurate or not |
| `arithmetic` | the explanation asserts a figure the passage does not contain, usually by applying a rule it states. Often correct; flagged because the grounding check has no category for "entailed but not present" |
| `options` | the explanation makes claims about numbered options the passage never mentions by number |

Counts: {by_kind}

---
"""


def render(report: dict) -> str:
    out = [HEADER.format(**{**report, "by_kind": json.dumps(report["by_kind"])})]
    for row in report["items"]:
        out.append(f"## {row['reading_order']}. {row['id']}  ·  "
                   f"{row['subject'] or 'no subject'}\n")
        out.append(f"`label: {row['label']}` · `label_status: {row['label_status']}` · "
                   f"`reviewers: {len(row['reviewers'])}`\n")
        out.append(f"**Passage.** {row['passage']}\n")
        out.append(f"**Explanation.** {row['explanation']}\n")
        for n, claim in enumerate(row["claims"], 1):
            out.append(f"**Claim {n} — `{claim['kind']}`**\n")
            out.append(f"> {claim['claim']}\n")
            if claim.get("passage_says"):
                out.append(f"Nearest passage sentence:\n")
                out.append(f"> {claim['passage_says']}\n")
            if claim.get("words_not_in_passage"):
                out.append(f"Words asserted that the passage nowhere uses: "
                           f"`{', '.join(claim['words_not_in_passage'])}`\n")
            if claim.get("numbers_not_in_passage"):
                out.append(f"Figures not in the passage: "
                           f"`{', '.join(claim['numbers_not_in_passage'])}`\n")
            out.append(f"*Why flagged:* {claim['why']}\n")
        out.append("**Ruling** (clean / defective / edge): ______  "
                   "**By:** ______  **Note:** ______\n")
        out.append("\n---\n")
    return "\n".join(out)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--corpus", default="corpus/validator_dev")
    parser.add_argument("--out", default="",
                        help="write the worksheet here as markdown; default is stdout")
    parser.add_argument("--json", default="",
                        help="also write the machine-readable form here")
    args = parser.parse_args(argv)

    report = build(args.corpus)
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(report, indent=2), encoding="utf-8")
    text = render(report)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"{report['items_flagged']} of {report['clean_items']} clean items flagged, "
              f"{report['claims_flagged']} claims -> {args.out}")
        print("This is an audit worksheet. No label has been changed and none may be "
              "changed on the strength of it; rulings go through validator/review.py.")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
