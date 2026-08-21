"""
The two-reviewer protocol: how a label stops being one person's opinion.

WHY TWO, AND WHY BLIND
----------------------
A single reviewer labelling a corpus produces a corpus that measures agreement
with that reviewer. Nobody can tell from the file whether an item is defective
or whether one tired person thought so on a Thursday, and every validator
scored against it inherits that without a way to see it.

Two reviewers who cannot see each other's answers give a number that says how
much of the labelling is the material and how much is the labeller. That number
is Cohen's kappa, and it is reported before any validator is scored, because a
corpus whose reviewers agree barely more than chance cannot support a claim
about a validator no matter how carefully the validator was measured.

WHAT THIS MODULE REFUSES
------------------------
    The same person as both reviewers. It is not two opinions.
    An adjudicator who is one of the two reviewers. Someone breaking a tie in
        favour of their own earlier answer is not adjudication.
    Merging before both sheets exist. A second reviewer who can see the first
        reviewer's labels is not independent, and the cheapest way to guarantee
        that is to refuse to produce a merged view until both are in.
    An unnamed reviewer. "Reviewed" without a name is not a reviewable claim.

WHAT IT DOES NOT DO
-------------------
It does not review anything. It is the bookkeeping around people doing that,
and its whole value is that the bookkeeping is refused when it would be
misleading. The development corpus in this repository is model-authored and
carries `label_status: unreviewed`; running it through here with two named
clinicians is what changes that, and nothing else does.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from benchmark.corpus import DEFECT_CLASSES
from validator.devset import ADJUDICATED, AGREED, DISPUTED, CLEAN, DEFECTIVE, EDGE

# What a reviewer may say. Deliberately not the same vocabulary as a validator
# verdict: a person saying "I am not sure" is evidence about the item, and a
# validator abstaining is evidence about the validator.
USABLE = "USABLE"
BROKEN = "BROKEN"
UNSURE = "UNSURE"
REVIEW_LABELS = (USABLE, BROKEN, UNSURE)

# How a settled review maps onto the corpus label.
SETTLED_LABEL = {USABLE: CLEAN, BROKEN: DEFECTIVE, UNSURE: EDGE}


class ReviewError(ValueError):
    """The review record violates the protocol, with the reason."""


@dataclass
class Judgement:
    item_id: str
    label: str
    defect_class: str = ""
    note: str = ""

    def __post_init__(self):
        if self.label not in REVIEW_LABELS:
            raise ReviewError(
                f"{self.item_id}: {self.label!r} is not one of {', '.join(REVIEW_LABELS)}")
        if self.label == BROKEN and not self.defect_class:
            raise ReviewError(
                f"{self.item_id}: a reviewer calling an item broken must say in what way. "
                "'Broken' without a defect class cannot be adjudicated and cannot be fixed.")
        if self.defect_class and self.defect_class not in DEFECT_CLASSES:
            raise ReviewError(
                f"{self.item_id}: defect_class {self.defect_class!r} is not one of "
                f"{', '.join(DEFECT_CLASSES)}")


@dataclass
class Sheet:
    """One reviewer's answers. A reviewer never sees another's sheet."""
    reviewer: str
    judgements: dict[str, Judgement] = field(default_factory=dict)

    def __post_init__(self):
        if not self.reviewer.strip():
            raise ReviewError(
                "a review sheet must name its reviewer. An unnamed reviewer cannot be asked "
                "what they meant, and 'reviewed by somebody' is not provenance.")


def template(cases, reviewer: str) -> str:
    """A blank sheet, one JSON line per item, with the labels withheld."""
    lines = []
    for case in cases:
        lines.append(json.dumps({
            "item_id": case.id, "reviewer": reviewer,
            "label": "", "defect_class": "", "note": "",
            "stem": case.item.stem, "options": list(case.item.options),
            "source_passage": case.item.source_passage,
            "explanation": case.item.explanation,
            "declared_concept": case.item.concept,
            "declared_difficulty": case.item.difficulty,
        }, ensure_ascii=True))
    return "\n".join(lines) + "\n"


def load_sheet(path: str | Path) -> Sheet:
    path = Path(path)
    if not path.exists():
        raise ReviewError(f"no review sheet at {path}")
    reviewer = ""
    judgements: dict[str, Judgement] = {}
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        raw = json.loads(line)
        name = str(raw.get("reviewer") or "").strip()
        if not name:
            raise ReviewError(f"{path}:{lineno}: no reviewer named on this row")
        if reviewer and name != reviewer:
            raise ReviewError(
                f"{path}:{lineno}: this sheet names both {reviewer!r} and {name!r}. One sheet "
                "is one reviewer; merging two people into one file loses which of them said "
                "what.")
        reviewer = name
        label = str(raw.get("label") or "").strip().upper()
        if not label:
            continue  # not yet answered
        item_id = str(raw.get("item_id") or "").strip()
        if item_id in judgements:
            raise ReviewError(f"{path}:{lineno}: {item_id!r} answered twice")
        judgements[item_id] = Judgement(
            item_id=item_id, label=label,
            defect_class=str(raw.get("defect_class") or "").strip(),
            note=str(raw.get("note") or "").strip())
    return Sheet(reviewer=reviewer, judgements=judgements)


def kappa(a: Sheet, b: Sheet) -> dict:
    """
    Cohen's kappa over the items both reviewers answered.

    Raw agreement is reported alongside it and is the number people quote; kappa
    is the number that survives a corpus where 90 per cent of items are clean,
    because two reviewers who both say USABLE to everything agree 90 per cent of
    the time and have demonstrated nothing.
    """
    shared = sorted(set(a.judgements) & set(b.judgements))
    if not shared:
        return {"n": 0, "raw_agreement": None, "kappa": None,
                "note": "the two reviewers have no items in common"}
    left = [a.judgements[i].label for i in shared]
    right = [b.judgements[i].label for i in shared]
    n = len(shared)
    observed = sum(1 for x, y in zip(left, right) if x == y) / n
    count_a, count_b = Counter(left), Counter(right)
    expected = sum((count_a[k] / n) * (count_b[k] / n) for k in REVIEW_LABELS)
    value = None if expected >= 1.0 else (observed - expected) / (1 - expected)
    return {"n": n, "raw_agreement": round(observed, 4),
            "kappa": None if value is None else round(value, 4),
            "expected_agreement": round(expected, 4),
            "by_reviewer": {a.reviewer: dict(count_a), b.reviewer: dict(count_b)},
            "note": ("kappa is undefined when both reviewers used a single label for "
                     "everything" if value is None else "")}


def disagreements(a: Sheet, b: Sheet) -> list[dict]:
    """Items the two reviewers labelled differently, for adjudication."""
    shared = sorted(set(a.judgements) & set(b.judgements))
    out = []
    for item_id in shared:
        left, right = a.judgements[item_id], b.judgements[item_id]
        if left.label == right.label and left.defect_class == right.defect_class:
            continue
        out.append({"item_id": item_id,
                    a.reviewer: {"label": left.label, "defect_class": left.defect_class,
                                 "note": left.note},
                    b.reviewer: {"label": right.label, "defect_class": right.defect_class,
                                 "note": right.note},
                    "same_label": left.label == right.label})
    return out


def merge(a: Sheet, b: Sheet, adjudications: dict[str, Judgement] | None = None,
          adjudicator: str = "") -> dict:
    """
    Combine two sheets into settled labels, refusing where the protocol is broken.

    An item both reviewers agreed on is `agreed`. An item they disagreed on is
    `disputed` until an adjudication for it exists, at which point it is
    `adjudicated` and carries all three names.
    """
    if a.reviewer.strip().lower() == b.reviewer.strip().lower():
        raise ReviewError(
            f"both sheets are by {a.reviewer!r}. One person answering twice is one opinion "
            "with a larger n.")
    adjudications = dict(adjudications or {})
    if adjudications:
        if not adjudicator.strip():
            raise ReviewError("an adjudication must name the adjudicator")
        if adjudicator.strip().lower() in {a.reviewer.strip().lower(),
                                           b.reviewer.strip().lower()}:
            raise ReviewError(
                f"{adjudicator!r} is one of the two reviewers. Breaking a tie in favour of "
                "your own earlier answer is not adjudication.")

    missing_from = {a.reviewer: sorted(set(b.judgements) - set(a.judgements)),
                    b.reviewer: sorted(set(a.judgements) - set(b.judgements))}

    settled, disputed = {}, []
    for item_id in sorted(set(a.judgements) & set(b.judgements)):
        left, right = a.judgements[item_id], b.judgements[item_id]
        if left.label == right.label and left.defect_class == right.defect_class:
            settled[item_id] = {"label": SETTLED_LABEL[left.label],
                                "defect_class": left.defect_class,
                                "label_status": AGREED,
                                "reviewers": [a.reviewer, b.reviewer]}
            continue
        ruling = adjudications.get(item_id)
        if ruling is None:
            disputed.append(item_id)
            settled[item_id] = {"label": "", "defect_class": "",
                                "label_status": DISPUTED,
                                "reviewers": [a.reviewer, b.reviewer]}
            continue
        settled[item_id] = {"label": SETTLED_LABEL[ruling.label],
                            "defect_class": ruling.defect_class,
                            "label_status": ADJUDICATED,
                            "reviewers": [a.reviewer, b.reviewer, adjudicator],
                            "adjudication_note": ruling.note}

    agreement = kappa(a, b)
    return {"agreement": agreement,
            "settled": settled,
            "disputed": disputed,
            "unanswered": missing_from,
            "counts": dict(Counter(v["label_status"] for v in settled.values())),
            "usable_for_scoring": not disputed and not any(missing_from.values())}


def render(result: dict) -> str:
    lines = []
    agreement = result["agreement"]
    lines.append(f"Two-reviewer agreement over {agreement['n']} shared item(s)")
    if agreement["raw_agreement"] is not None:
        lines.append(f"  raw agreement  {agreement['raw_agreement']:.1%}")
    value = agreement["kappa"]
    lines.append("  Cohen's kappa  " + ("undefined" if value is None else f"{value:.3f}"))
    if agreement.get("note"):
        lines.append(f"  {agreement['note']}")
    lines.append("")
    for status, count in sorted(result["counts"].items()):
        lines.append(f"  {count:>3}  {status}")
    if result["disputed"]:
        lines.append("")
        lines.append(f"awaiting adjudication: {len(result['disputed'])}")
        for item_id in result["disputed"][:10]:
            lines.append(f"  {item_id}")
    for reviewer, items in result["unanswered"].items():
        if items:
            lines.append(f"{reviewer} has not answered {len(items)} item(s)")
    lines.append("")
    lines.append("usable for scoring: " + ("yes" if result["usable_for_scoring"] else "no"))
    return "\n".join(lines)
