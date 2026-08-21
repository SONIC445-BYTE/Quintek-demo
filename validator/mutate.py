"""
Controlled defect injection: how the negative arm of the validator corpus is built.

WHY DEFECTS ARE DERIVED RATHER THAN WRITTEN
-------------------------------------------
A negative item has to carry ground truth. If a broken question is written
from scratch, "what is wrong with it" is an opinion, and two reviewers can
reasonably disagree about whether it is broken at all. If instead a defect is
INTRODUCED into an item that was already agreed to be clean, then the item is
defective by construction, the defect is exactly the edit, and there is
nothing left to have an opinion about.

That is the asymmetry `benchmark/corpus.py` already documents: a positive
claim needs medical authority, a "this is broken, in this way" claim does not
when the break was performed deliberately.

THE INVARIANT THIS MODULE ENFORCES
----------------------------------
One controlled failure at a time. Each mutation declares the operation it
performs; `apply` computes which fields actually changed and refuses if that
set is not exactly what the operation is allowed to touch. A mutation that
quietly rewrites the stem while claiming to move the key would produce an item
with two defects and one label, and every false-negative attributed to it
afterwards would be attributed to the wrong cause.

The source passage is the evidence an item is graded against. Changing it
changes what "correct" means, so only the `ungrounded` operation may touch it,
and no operation may touch it as a side effect.

WHAT THIS DESIGN BUYS AND WHAT IT COSTS
---------------------------------------
Buys: matched pairs. The clean item and its defective twin differ in one
respect, so a validator that flags one and passes the other is responding to
the defect and not to the subject, the phrasing or the length.

Costs: the two arms are not independent in content. Real production defects
do not arrive with a clean twin, and a validator that scores well here has
been measured on a easier discrimination than production will ask of it. The
holdout set does not fix that; only production items will.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from benchmark.corpus import DEFECT_CLASSES, parse_item

# Operations. Each names the fields it is permitted to change.
SHIFT_KEY = "shift_key"
REPLACE_DISTRACTOR = "replace_distractor"
SET_STEM = "set_stem"
SET_EXPLANATION = "set_explanation"
SET_REFERENCE = "set_reference"
SET_PASSAGE = "set_passage"
REWRITE = "rewrite"

TOUCHES = {
    SHIFT_KEY: {"correct_index"},
    REPLACE_DISTRACTOR: {"options"},
    SET_STEM: {"stem"},
    SET_EXPLANATION: {"explanation"},
    SET_REFERENCE: {"reference"},
    SET_PASSAGE: {"source_passage"},
    REWRITE: {"stem", "options", "correct_index", "explanation"},
}

# What each operation must change. For most operations that is exactly what it
# is permitted to change. A rewrite replaces the whole question, and whether the
# key index happens to land on the same number as before is an accident of
# option order, not evidence that the rewrite did nothing.
MUST_CHANGE = dict(TOUCHES)
MUST_CHANGE[REWRITE] = {"stem", "options", "explanation"}

# The evidence an item is graded against. Only one operation may touch it.
PASSAGE_FIELD = "source_passage"

COMPARED_FIELDS = ("stem", "options", "correct_index", "explanation", "reference",
                   "source_passage", "difficulty", "question_type", "subject",
                   "topic", "concept", "facet")


class MutationError(ValueError):
    """A mutation is malformed, or changed more than it declared."""


@dataclass(frozen=True)
class Mutation:
    """One authored defect: which clean item, which class, which edit."""
    target_id: str
    source_id: str
    defect_class: str
    operation: str
    note: str
    payload: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.defect_class not in DEFECT_CLASSES:
            raise MutationError(
                f"{self.target_id}: defect_class {self.defect_class!r} is not one of "
                f"{', '.join(DEFECT_CLASSES)}")
        if self.operation not in TOUCHES:
            raise MutationError(
                f"{self.target_id}: operation {self.operation!r} is not one of "
                f"{', '.join(sorted(TOUCHES))}")
        if not self.note.strip():
            raise MutationError(
                f"{self.target_id}: a mutation needs a note saying what is now wrong. "
                "The note is the ground truth a reviewer adjudicates against.")


def _shift_key(item: dict, payload: dict) -> dict:
    by = int(payload.get("by", 1))
    n = len(item["options"])
    if n < 2:
        raise MutationError(f"{item['id']}: cannot move the key among {n} option(s)")
    if by % n == 0:
        raise MutationError(
            f"{item['id']}: shifting the key by {by} among {n} options leaves it where it "
            "was, which produces a clean item labelled defective")
    return {"correct_index": (item["correct_index"] + by) % n}


def _replace_distractor(item: dict, payload: dict) -> dict:
    text = str(payload.get("text", "")).strip()
    if not text:
        raise MutationError(f"{item['id']}: replace_distractor needs the replacement text")
    options = list(item["options"])
    key = item["correct_index"]
    which = next((i for i in range(len(options)) if i != key), None)
    if which is None:
        raise MutationError(f"{item['id']}: no distractor to replace")
    if text in options:
        raise MutationError(
            f"{item['id']}: the replacement text already appears among the options, which "
            "would make this a duplicate-option defect rather than the declared one")
    options[which] = text
    return {"options": options}


def _set_field(name):
    def op(item: dict, payload: dict) -> dict:
        text = str(payload.get("text", "")).strip()
        if not text:
            raise MutationError(f"{item['id']}: {name} needs replacement text")
        if text == item.get(name):
            raise MutationError(
                f"{item['id']}: the replacement {name} is identical to the original, so "
                "nothing was planted")
        return {name: text}
    return op


def _rewrite(item: dict, payload: dict) -> dict:
    for key in ("stem", "options", "correct_index", "explanation"):
        if key not in payload:
            raise MutationError(f"{item['id']}: rewrite needs {key!r}")
    return {"stem": payload["stem"], "options": list(payload["options"]),
            "correct_index": int(payload["correct_index"]),
            "explanation": payload["explanation"]}


OPERATIONS = {
    SHIFT_KEY: _shift_key,
    REPLACE_DISTRACTOR: _replace_distractor,
    SET_STEM: _set_field("stem"),
    SET_EXPLANATION: _set_field("explanation"),
    SET_REFERENCE: _set_field("reference"),
    SET_PASSAGE: _set_field(PASSAGE_FIELD),
    REWRITE: _rewrite,
}


def apply(source: dict, mutation: Mutation) -> dict:
    """
    Return the defective item, or raise if the edit exceeded what it declared.

    The returned item carries `derived_from`, so any finding about it can be
    compared against its clean twin without a lookup table.
    """
    if source["id"] != mutation.source_id:
        raise MutationError(
            f"{mutation.target_id}: declares source {mutation.source_id!r} but was given "
            f"item {source['id']!r}")

    changes = OPERATIONS[mutation.operation](source, mutation.payload)

    item = dict(source)
    item.update(changes)
    item["id"] = mutation.target_id
    item["defect_class"] = mutation.defect_class
    item["defect_note"] = mutation.note
    item["derived_from"] = mutation.source_id
    item["mutation"] = mutation.operation
    item["gold_standard"] = False
    item["tags"] = ["validator_dev", "defect", mutation.defect_class]

    changed = {f for f in COMPARED_FIELDS if item.get(f) != source.get(f)}
    allowed = TOUCHES[mutation.operation]
    required = MUST_CHANGE[mutation.operation]
    unexpected = sorted(changed - allowed)
    missing = sorted(required - changed)
    if unexpected or missing:
        raise MutationError(
            f"{mutation.target_id}: operation {mutation.operation!r} may change "
            f"{sorted(allowed)} and must change {sorted(required)}, but changed "
            f"{sorted(changed)}"
            + (f"; unexpected {unexpected}" if unexpected else "")
            + (f"; required but unchanged {missing}" if missing else "")
            + ". One controlled failure at a time -- an item with two defects and one "
              "label misattributes every miss recorded against it.")

    if mutation.operation != SET_PASSAGE and item.get(PASSAGE_FIELD) != source.get(PASSAGE_FIELD):
        raise MutationError(
            f"{mutation.target_id}: the source passage is the evidence the item is graded "
            "against, and only the ungrounded operation may change it")

    parse_item(item, where=mutation.target_id)
    return item


def load_mutations(path: str | Path) -> list[Mutation]:
    """Read the authored mutation spec. Fails on the first bad entry."""
    path = Path(path)
    if not path.exists():
        raise MutationError(f"no mutation spec at {path}")
    out, seen = [], set()
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise MutationError(f"{path}:{lineno}: not valid JSON -- {exc}")
        try:
            mutation = Mutation(
                target_id=raw["target_id"], source_id=raw["source_id"],
                defect_class=raw["defect_class"], operation=raw["operation"],
                note=raw["note"], payload=raw.get("payload") or {})
        except KeyError as exc:
            raise MutationError(f"{path}:{lineno}: missing field {exc.args[0]!r}")
        if mutation.target_id in seen:
            raise MutationError(f"{path}:{lineno}: duplicate target_id {mutation.target_id!r}")
        seen.add(mutation.target_id)
        out.append(mutation)
    return out


def build(clean_rows: list[dict], mutations: list[Mutation]) -> list[dict]:
    """Apply every mutation to its named source. Refuses to reuse a source twice."""
    by_id = {row["id"]: row for row in clean_rows}
    used: dict[str, str] = {}
    out = []
    for mutation in mutations:
        source = by_id.get(mutation.source_id)
        if source is None:
            raise MutationError(
                f"{mutation.target_id}: source {mutation.source_id!r} is not in the clean set")
        if mutation.source_id in used:
            raise MutationError(
                f"{mutation.target_id}: source {mutation.source_id!r} was already mutated by "
                f"{used[mutation.source_id]!r}. Two defects derived from one clean item make "
                "the negative arm narrower than its count suggests.")
        used[mutation.source_id] = mutation.target_id
        out.append(apply(source, mutation))
    return out
