"""
The labelled sets a validator is measured on, and the rule about which set is which.

THREE LABELS, TWO ARMS
----------------------
    CLEAN      the item is usable. A validator that flags it is wrong.
    DEFECTIVE  the item is broken, in a known way. A validator that passes it is wrong.
    EDGE       competent reviewers disagree about whether it is usable.

Only CLEAN and DEFECTIVE form the two arms of the pass gate. EDGE items are
scored separately and contribute to neither rate, because folding them in
corrupts both: counted as clean they inflate the false-flag rate of a validator
that was arguably right, and counted as defective they credit a validator for
catching something that may not be a defect. What the edge set measures is
different and still useful -- whether the validator knows it is on uncertain
ground, which shows up as abstention rather than as accuracy.

DEV AND HOLDOUT
---------------
The development set is what the validator is built against: read it, argue with
it, tune against it. Every look at it spends a little of its value as evidence,
which is fine, because it was never the evidence the gate rests on.

The holdout set is. Its labels live sealed in `validator/holdout.py`, which
records every scoring run. Nothing here loads them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from benchmark.corpus import CorpusItem, DEFECT_CLASSES, parse_item

CLEAN = "CLEAN"
DEFECTIVE = "DEFECTIVE"
EDGE = "EDGE"
LABELS = (CLEAN, DEFECTIVE, EDGE)

# The two arms of the gate. EDGE is deliberately absent.
ARM_LABELS = (CLEAN, DEFECTIVE)

# Where a reviewer's label stands.
UNREVIEWED = "unreviewed"
AGREED = "agreed"
ADJUDICATED = "adjudicated"
DISPUTED = "disputed"

DEV_ROOT = Path("corpus/validator_dev")
FILES = {CLEAN: "clean.jsonl", DEFECTIVE: "defects.jsonl", EDGE: "edge.jsonl"}


class DevsetError(ValueError):
    """The labelled set violates its contract, with the reason."""


@dataclass
class Case:
    """One labelled item: the question, and what is true about it."""
    item: CorpusItem
    label: str
    defect_class: str = ""
    edge_reason: str = ""
    derived_from: str = ""
    mutation: str = ""
    label_status: str = UNREVIEWED
    reviewers: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.item.id

    @property
    def in_arm(self) -> bool:
        return self.label in ARM_LABELS


def _case(raw: dict, label: str, where: str) -> Case:
    item = parse_item(raw, where=where)
    defect = (raw.get("defect_class") or "").strip()

    if label == DEFECTIVE:
        if not defect:
            raise DevsetError(
                f"{where}: an item in the defective arm carries no defect_class. Ground truth "
                "for this arm is the named defect; without it there is nothing to be right about.")
        if not (raw.get("defect_note") or "").strip():
            raise DevsetError(
                f"{where}: defective item has no defect_note. The note is what a reviewer "
                "adjudicates against and what a false negative is explained by.")
    elif defect:
        raise DevsetError(
            f"{where}: item is labelled {label} but carries defect_class {defect!r}. An item "
            "cannot be both the thing a validator must pass and the thing it must catch.")

    if label == EDGE and not (raw.get("edge_reason") or "").strip():
        raise DevsetError(
            f"{where}: an edge case must say why it is an edge case. Without the reason it is "
            "an unlabelled item, and it will be silently treated as clean by whoever reads it next.")

    return Case(item=item, label=label, defect_class=defect,
                edge_reason=(raw.get("edge_reason") or "").strip(),
                derived_from=(raw.get("derived_from") or "").strip(),
                mutation=(raw.get("mutation") or "").strip(),
                label_status=(raw.get("label_status") or UNREVIEWED).strip(),
                reviewers=[str(r) for r in (raw.get("reviewers") or [])])


def load_file(path: str | Path, label: str) -> list[Case]:
    path = Path(path)
    if label not in LABELS:
        raise DevsetError(f"{label!r} is not one of {', '.join(LABELS)}")
    if not path.exists():
        raise DevsetError(f"no {label} file at {path}")
    cases = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DevsetError(f"{path}:{lineno}: not valid JSON -- {exc}")
        cases.append(_case(raw, label, where=f"{path}:{lineno}"))
    return cases


@dataclass
class Devset:
    cases: list[Case]
    root: Path

    def by_label(self, label: str) -> list[Case]:
        return [c for c in self.cases if c.label == label]

    @property
    def arms(self) -> list[Case]:
        return [c for c in self.cases if c.in_arm]

    def labels(self) -> list[str]:
        """Ground-truth labels for the two arms, in `arms` order."""
        return [c.label for c in self.arms]

    def summary(self) -> dict:
        from collections import Counter
        defects = Counter(c.defect_class for c in self.by_label(DEFECTIVE))
        return {
            "root": str(self.root),
            "total": len(self.cases),
            "clean": len(self.by_label(CLEAN)),
            "defective": len(self.by_label(DEFECTIVE)),
            "edge": len(self.by_label(EDGE)),
            "arm_total": len(self.arms),
            "defect_classes": dict(sorted(defects.items())),
            "defect_classes_missing": [d for d in DEFECT_CLASSES if d not in defects],
            "subjects": len({c.item.subject for c in self.cases}),
        }


def load(root: str | Path = DEV_ROOT) -> Devset:
    """
    Load the three files and check the invariants that make the set measurable.

    Fails rather than warns. A set that quietly contains a duplicated id, or a
    defective item whose clean twin is missing, still produces a confusion
    matrix -- one that is wrong in a way no downstream number reveals.
    """
    root = Path(root)
    cases: list[Case] = []
    for label, name in FILES.items():
        cases.extend(load_file(root / name, label))

    seen: dict[str, str] = {}
    for case in cases:
        if case.id in seen:
            raise DevsetError(
                f"{root}: id {case.id!r} appears in both the {seen[case.id]} and {case.label} "
                "files. A duplicated id is counted twice and, when the labels differ, counted "
                "twice with opposite ground truth.")
        seen[case.id] = case.label

    clean_ids = {c.id for c in cases if c.label == CLEAN}
    for case in cases:
        if case.label == DEFECTIVE:
            if not case.derived_from:
                raise DevsetError(
                    f"{case.id}: defective item does not record the clean item it was derived "
                    "from, so no matched-pair comparison can be made against it.")
            if case.derived_from not in clean_ids:
                raise DevsetError(
                    f"{case.id}: derived_from {case.derived_from!r} is not in the clean set")

    stems: dict[str, str] = {}
    for case in cases:
        key = case.item.stem.strip().lower()
        if case.label == DEFECTIVE:
            continue  # a defective twin legitimately shares its stem with its source
        if key in stems:
            raise DevsetError(
                f"{case.id}: its stem is identical to {stems[key]!r}. Two copies of one question "
                "in the arms make n look larger than the number of distinct things measured.")
        stems[key] = case.id

    return Devset(cases=cases, root=root)


def assert_disjoint(a: Devset, b: Devset) -> None:
    """
    Refuse to let a development set and a holdout set share material.

    Shared ids are the obvious leak. Shared stems and shared source passages are
    the ones that survive a rename: an item tuned against in development, moved
    into the holdout under a new id, measures the tuning rather than the method.
    """
    def index(devset, attr):
        out = {}
        for case in devset.cases:
            value = getattr(case.item, attr).strip().lower()
            if value:
                out.setdefault(value, case.id)
        return out

    shared_ids = {c.id for c in a.cases} & {c.id for c in b.cases}
    if shared_ids:
        raise DevsetError(
            f"development and holdout sets share {len(shared_ids)} id(s), "
            f"including {sorted(shared_ids)[:5]}")

    for attr, what in (("stem", "question stem"), ("source_passage", "source passage")):
        left, right = index(a, attr), index(b, attr)
        shared = set(left) & set(right)
        if shared:
            example = sorted(shared)[0]
            raise DevsetError(
                f"development and holdout sets share {len(shared)} {what}(s), for example "
                f"{left[example]!r} and {right[example]!r}. A holdout item that also appears "
                "in development measures how well the validator was tuned, not how well it works.")
