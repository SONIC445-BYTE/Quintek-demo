"""
Gold Corpus v0.1 -- schema, provenance rules, and the loader.

WHY THIS FILE REFUSES SOMETHING YOU MIGHT WANT IT TO DO
-------------------------------------------------------
A gold corpus is the thing a benchmark measures against. Its authority comes
entirely from who wrote it. If a model authors the gold, then a benchmark run
scored against that gold measures agreement between a model and a model --
and reports it as accuracy. The number looks exactly like a real one. Every
downstream artifact in this repository, up to and including the learner-facing
transparency screen, would then be presenting a circular result as evidence.

`docs/GOLD_ERROR_PATHWAY.md` and IMPLEMENTATION_STATUS.md both already say the
corpus "cannot be built by a model" for this reason. So rather than restate it
in prose that a future contributor can skip, the rule is enforced here:

    An item whose provenance is model-authored CANNOT be marked
    gold_standard: true. `load()` raises. There is no flag to override it.

That is not the same as refusing to be useful. Model-authored items are
genuinely valuable, and this module supports them fully -- as
`provenance: "model_authored"`, `gold_standard: false`, which makes them a
DEVELOPMENT SET: good enough to exercise retrieval, prompting, normalization
and the validator end to end, not good enough to score a model against.

THE ONE ASYMMETRY WORTH KNOWING
-------------------------------
Negative items are different, and the difference is not a loophole.

A positive item claims "this is the correct answer", and that claim needs
medical authority. A negative item claims "this question is broken, in this
specific way" -- and when the item was CONSTRUCTED broken, that claim is true
by construction, not by authority. A question whose key points at option 5 of
4 is defective whoever wrote it.

So `defect_class` items may be model-authored and still carry ground truth,
which is why the adversarial battery in `corpus/adversarial.jsonl` is usable
immediately while `corpus/gold.jsonl` waits for people. Measuring whether the
validator catches deliberate defects is the one real evaluation available
before the expert corpus exists.

THE FACET MODEL
---------------
A concept is not tested by one question. `facet` splits it three ways:

    definition   what the thing is
    mechanism    why it behaves as it does
    clinical     what you do about it in front of a patient

A model that answers the definition and fails the mechanism has not understood
the concept, and a corpus that only asks definitions cannot tell. Coverage is
reported per (concept, facet) so the gap is visible.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

CORPUS_VERSION = "0.1"

FACETS = ("definition", "mechanism", "clinical")

QUESTION_TYPES = ("mcq", "vignette", "conceptual", "assertion_reason", "sequence")

DIFFICULTIES = ("foundation", "pg_entry", "pg_advanced")

# Who wrote the item, and therefore what it can be used for.
PROVENANCE = {
    # Written by a qualified human against a named reference. The only kind
    # that may carry gold_standard: true.
    "expert_authored": {"may_be_gold": True},
    # Taken verbatim from a published, attributable source with permission.
    "published_source": {"may_be_gold": True},
    # Written by a model. Usable as a development set; never gold.
    "model_authored": {"may_be_gold": False},
    # Written by a model and then checked by a named human. Promotable, but
    # only once `reviewed_by` and `reviewed_at` are filled in.
    "model_authored_expert_reviewed": {"may_be_gold": True, "requires_reviewer": True},
}

# Ground truth for the adversarial battery: what is wrong with the item.
DEFECT_CLASSES = (
    "wrong_key",              # the keyed option is not the correct answer
    "two_correct",            # more than one option is defensible
    "ambiguous_stem",         # the stem does not determine a single answer
    "hallucinated_fact",      # asserts something that is not true
    "hallucinated_reference", # cites a source that does not say this
    "out_of_syllabus",        # correct but outside the stated scope
    "poor_reasoning",         # the explanation does not support the key
    "ungrounded",             # not answerable from the supplied passage
    "giveaway",               # the stem or options reveal the answer
    "trivial",                # recall-only, below the stated difficulty
)


class CorpusError(ValueError):
    """An item or file violates the corpus contract, with the reason."""


@dataclass
class CorpusItem:
    id: str
    subject: str
    topic: str
    concept: str
    facet: str
    question_type: str
    difficulty: str
    stem: str
    options: list[str]
    correct_index: int
    explanation: str
    reference: str
    provenance: str
    gold_standard: bool = False
    source_passage: str = ""
    reviewed_by: str = ""
    reviewed_at: str = ""
    author: str = ""
    defect_class: str = ""
    defect_note: str = ""
    tags: list[str] = field(default_factory=list)

    @property
    def is_negative(self) -> bool:
        return bool(self.defect_class)

    @property
    def correct_answer(self) -> str:
        return self.options[self.correct_index] if self.options else ""

    def as_dict(self) -> dict:
        return {
            "id": self.id, "subject": self.subject, "topic": self.topic,
            "concept": self.concept, "facet": self.facet,
            "question_type": self.question_type, "difficulty": self.difficulty,
            "stem": self.stem, "options": list(self.options),
            "correct_index": self.correct_index, "correct_answer": self.correct_answer,
            "explanation": self.explanation, "reference": self.reference,
            "provenance": self.provenance, "gold_standard": self.gold_standard,
            "source_passage": self.source_passage,
            "reviewed_by": self.reviewed_by, "reviewed_at": self.reviewed_at,
            "author": self.author, "defect_class": self.defect_class,
            "defect_note": self.defect_note, "tags": list(self.tags),
        }


def _require(item: dict, key: str, where: str):
    value = item.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise CorpusError(f"{where}: missing required field {key!r}")
    return value


def parse_item(raw: dict, *, where: str = "item") -> CorpusItem:
    """
    Validate one item and return it, or raise with the reason.

    Every check here exists because the corresponding malformation would be
    silently scorable: an item with no reference cannot be challenged, an item
    whose key points outside its options can never be answered, and an item
    claiming gold status it has not earned corrupts every run that uses it.
    """
    for key in ("id", "subject", "topic", "concept", "facet", "question_type",
                "difficulty", "stem", "explanation", "provenance"):
        _require(raw, key, where)

    facet = raw["facet"]
    if facet not in FACETS:
        raise CorpusError(f"{where}: facet {facet!r} is not one of {', '.join(FACETS)}")

    qtype = raw["question_type"]
    if qtype not in QUESTION_TYPES:
        raise CorpusError(
            f"{where}: question_type {qtype!r} is not one of {', '.join(QUESTION_TYPES)}")

    difficulty = raw["difficulty"]
    if difficulty not in DIFFICULTIES:
        raise CorpusError(
            f"{where}: difficulty {difficulty!r} is not one of {', '.join(DIFFICULTIES)}")

    provenance = raw["provenance"]
    if provenance not in PROVENANCE:
        raise CorpusError(
            f"{where}: provenance {provenance!r} is not one of {', '.join(sorted(PROVENANCE))}")

    options = [str(o).strip() for o in (raw.get("options") or []) if str(o).strip()]
    if len(options) < 2:
        raise CorpusError(f"{where}: needs at least 2 options, got {len(options)}")
    try:
        correct_index = int(raw["correct_index"])
    except (KeyError, TypeError, ValueError):
        raise CorpusError(f"{where}: correct_index is missing or not an integer")
    if not 0 <= correct_index < len(options):
        raise CorpusError(
            f"{where}: correct_index {correct_index} points outside the {len(options)} "
            "options -- a key that points nowhere can never be answered")

    defect = (raw.get("defect_class") or "").strip()
    if defect and defect not in DEFECT_CLASSES:
        raise CorpusError(
            f"{where}: defect_class {defect!r} is not one of {', '.join(DEFECT_CLASSES)}")

    gold = bool(raw.get("gold_standard", False))
    rules = PROVENANCE[provenance]

    # THE RULE. A model cannot certify the gold it will be graded against.
    if gold and not rules["may_be_gold"]:
        raise CorpusError(
            f"{where}: provenance {provenance!r} cannot be marked gold_standard. A model "
            "authoring the gold it is then scored against measures agreement between a model "
            "and a model, and reports it as accuracy. Keep the item with "
            "gold_standard: false -- it is a development-set item, which is genuinely "
            "useful -- or have a qualified human review it and set provenance to "
            "'model_authored_expert_reviewed' with reviewed_by filled in.")

    if gold and rules.get("requires_reviewer") and not (raw.get("reviewed_by") or "").strip():
        raise CorpusError(
            f"{where}: provenance {provenance!r} may be gold only once reviewed_by names the "
            "person who reviewed it. An unnamed reviewer is not a reviewer.")

    if gold and not (raw.get("reference") or "").strip():
        raise CorpusError(
            f"{where}: a gold item needs a reference. An item that cannot be checked against "
            "a source cannot be challenged, and a corpus nobody can challenge is not gold.")

    if defect and gold:
        raise CorpusError(
            f"{where}: an item with a defect_class describes a BROKEN question, so it is not "
            "gold. Its ground truth is the defect, not the answer.")

    return CorpusItem(
        id=raw["id"], subject=raw["subject"], topic=raw["topic"], concept=raw["concept"],
        facet=facet, question_type=qtype, difficulty=difficulty,
        stem=raw["stem"], options=options, correct_index=correct_index,
        explanation=raw["explanation"], reference=(raw.get("reference") or "").strip(),
        provenance=provenance, gold_standard=gold,
        source_passage=(raw.get("source_passage") or "").strip(),
        reviewed_by=(raw.get("reviewed_by") or "").strip(),
        reviewed_at=(raw.get("reviewed_at") or "").strip(),
        author=(raw.get("author") or "").strip(),
        defect_class=defect, defect_note=(raw.get("defect_note") or "").strip(),
        tags=[str(t) for t in (raw.get("tags") or [])],
    )


def load(path: str | Path) -> list[CorpusItem]:
    """
    Read a JSONL corpus file. Fails on the first bad item, naming the line.

    Fail-fast rather than skip-and-warn: a corpus that silently drops the
    items it could not parse reports a smaller n than it was asked for, and
    nothing downstream can tell that from a smaller corpus.
    """
    path = Path(path)
    if not path.exists():
        raise CorpusError(f"no corpus file at {path}")
    items, seen = [], set()
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CorpusError(f"{path}:{lineno}: not valid JSON -- {exc}")
        item = parse_item(raw, where=f"{path}:{lineno}")
        if item.id in seen:
            raise CorpusError(
                f"{path}:{lineno}: duplicate id {item.id!r}. Duplicate ids double-count in "
                "every aggregate computed from this file.")
        seen.add(item.id)
        items.append(item)
    return items


def coverage(items: list[CorpusItem]) -> dict:
    """
    What this corpus actually covers, and where it is thin.

    Reported per (concept, facet) because that is the unit a claim about
    understanding rests on. A concept present only as `definition` supports
    "the model knows the name", not "the model understands the concept".
    """
    by_concept: dict[str, set] = defaultdict(set)
    by_subject: dict[str, int] = defaultdict(int)
    by_facet: dict[str, int] = defaultdict(int)
    by_type: dict[str, int] = defaultdict(int)
    by_provenance: dict[str, int] = defaultdict(int)
    for item in items:
        by_concept[item.concept].add(item.facet)
        by_subject[item.subject] += 1
        by_facet[item.facet] += 1
        by_type[item.question_type] += 1
        by_provenance[item.provenance] += 1

    complete = {c for c, facets in by_concept.items() if set(FACETS) <= facets}
    partial = {c: sorted(set(FACETS) - facets)
               for c, facets in by_concept.items() if not set(FACETS) <= facets}

    gold = [i for i in items if i.gold_standard]
    return {
        "total": len(items),
        "gold_count": len(gold),
        "development_count": len(items) - len(gold) - sum(1 for i in items if i.is_negative),
        "negative_count": sum(1 for i in items if i.is_negative),
        "subjects": dict(by_subject),
        "concepts": len(by_concept),
        "concepts_with_all_facets": sorted(complete),
        "concepts_missing_facets": {c: m for c, m in sorted(partial.items())},
        "by_facet": dict(by_facet),
        "by_question_type": dict(by_type),
        "by_provenance": dict(by_provenance),
        "scorable_as_gold": bool(gold),
        "note": ("No item in this corpus is gold. It can exercise the pipeline and measure "
                 "defect detection, but it cannot score a model's accuracy -- that needs "
                 "expert-authored items." if not gold else ""),
    }


def defect_coverage(items: list[CorpusItem]) -> dict:
    """Which defect classes the adversarial battery actually exercises."""
    counts: dict[str, int] = defaultdict(int)
    for item in items:
        if item.defect_class:
            counts[item.defect_class] += 1
    missing = [d for d in DEFECT_CLASSES if d not in counts]
    return {"by_defect_class": dict(counts), "covered": len(counts),
            "total_classes": len(DEFECT_CLASSES), "missing": missing}
