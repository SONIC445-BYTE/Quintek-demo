"""
Dataset loading and validation.

Enforces every rule in docs/DATASET_SPEC.md plus the v0.4 additions:
  - safety-holdout distribution constraints (docs/SEVERITY_TAXONOMY.md)
  - split isolation
  - unit-correct counting per track

A dataset that fails validation does not produce a low score. It produces a
refusal to run, because scoring an invalid corpus manufactures a number.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .integrity import sha256_file

VALID_SPLITS = {"dev", "validation", "holdout", "adversarial", "regression"}
VALID_SEVERITY = {"low", "medium", "high", "critical"}
VALID_TRACKS = {
    "medical_qa", "concept_extraction", "concept_resolution", "relationships",
    "generation", "validation", "cross_subject", "fake_mastery", "robustness",
    "injection",
}
CME_CATEGORIES = {"CME-1", "CME-2", "CME-3", "CME-4", "CME-5", "CME-6"}

# From docs/SEVERITY_TAXONOMY.md. A safety holdout of uniformly similar items
# satisfies min_n while measuring almost nothing.
MAX_SUBJECT_SHARE = 0.30
MAX_CME_CATEGORY_SHARE = 0.35
MIN_SUBJECTS = 5
MIN_CME_CATEGORIES = 4
MIN_CME_CATEGORY_SHARE = 0.10


@dataclass
class ValidationReport:
    ok: bool
    n_items: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    by_track: dict = field(default_factory=dict)
    by_split: dict = field(default_factory=dict)
    dataset_hash: str = ""

    def as_dict(self) -> dict:
        return {
            "ok": self.ok, "n_items": self.n_items,
            "errors": self.errors, "warnings": self.warnings,
            "by_track": self.by_track, "by_split": self.by_split,
            "dataset_hash": self.dataset_hash,
        }


@dataclass
class Item:
    id: str
    track: str
    split: str
    prompt: str
    gold: dict
    provenance: dict
    adjudication: dict
    severity: str = "medium"
    subject: str = ""
    topic: str = ""
    difficulty: str = ""
    competency: str = ""
    cme_category_targeted: str | None = None
    concept_id: str | None = None
    base_item_id: str | None = None
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "Item":
        return cls(
            id=d.get("id", ""), track=d.get("track", ""), split=d.get("split", ""),
            prompt=d.get("prompt", ""), gold=d.get("gold", {}) or {},
            provenance=d.get("provenance", {}) or {},
            adjudication=d.get("adjudication", {}) or {},
            severity=d.get("severity", "medium"), subject=d.get("subject", ""),
            topic=d.get("topic", ""), difficulty=d.get("difficulty", ""),
            competency=d.get("competency", ""),
            cme_category_targeted=d.get("cme_category_targeted"),
            concept_id=d.get("concept_id"), base_item_id=d.get("base_item_id"),
            raw=d,
        )


def load(path: str | Path) -> list[Item]:
    items = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            items.append(Item.from_dict(json.loads(line)))
    return items


def validate(path: str | Path) -> ValidationReport:
    path = Path(path)
    rep = ValidationReport(ok=True, dataset_hash=sha256_file(path))

    seen: set[str] = set()
    items: list[Item] = []
    prompts_by_split: dict[str, set[str]] = defaultdict(set)

    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError as exc:
            rep.errors.append(f"line {lineno}: malformed JSON: {exc}")
            continue

        for key in ("id", "track", "split", "prompt", "gold", "provenance", "adjudication"):
            if key not in d:
                rep.errors.append(f"line {lineno}: missing required field '{key}'")

        item = Item.from_dict(d)
        items.append(item)

        if item.id in seen:
            rep.errors.append(f"line {lineno}: duplicate id '{item.id}'")
        seen.add(item.id)

        if item.split not in VALID_SPLITS:
            rep.errors.append(f"line {lineno}: invalid split '{item.split}'")
        if item.track not in VALID_TRACKS:
            rep.errors.append(f"line {lineno}: unknown track '{item.track}'")
        if item.severity not in VALID_SEVERITY:
            rep.errors.append(f"line {lineno}: invalid severity '{item.severity}'")

        if not item.provenance.get("type"):
            rep.errors.append(f"line {lineno}: provenance.type is required")

        # Medical gold must be adjudicated before it can gate anything.
        if item.track == "medical_qa":
            if item.adjudication.get("status") != "verified":
                rep.errors.append(
                    f"line {lineno}: medical_qa item '{item.id}' has adjudication status "
                    f"'{item.adjudication.get('status')}', must be 'verified'")
            if int(item.adjudication.get("reviewers", 0)) < 2:
                rep.errors.append(
                    f"line {lineno}: medical_qa item '{item.id}' has "
                    f"{item.adjudication.get('reviewers', 0)} reviewer(s), 2 required")
            if not item.gold.get("answer"):
                rep.errors.append(f"line {lineno}: medical_qa item '{item.id}' missing gold.answer")
            if not item.gold.get("rationale"):
                rep.errors.append(f"line {lineno}: '{item.id}' missing gold.rationale")
            if not item.gold.get("concept_ids"):
                rep.errors.append(f"line {lineno}: '{item.id}' missing gold.concept_ids")

        if item.severity in ("high", "critical"):
            if item.cme_category_targeted and item.cme_category_targeted not in CME_CATEGORIES:
                rep.errors.append(
                    f"line {lineno}: unknown CME category '{item.cme_category_targeted}'")

        if item.track == "robustness" and not item.base_item_id:
            rep.errors.append(
                f"line {lineno}: robustness item '{item.id}' has no base_item_id; "
                "clustered CIs are impossible without it")
        if item.track == "fake_mastery" and not item.concept_id:
            rep.errors.append(f"line {lineno}: fake_mastery item '{item.id}' has no concept_id")

        prompts_by_split[item.split].add(item.prompt.strip())

    # Split isolation: a holdout item duplicated into dev is a contamination path.
    holdout = prompts_by_split.get("holdout", set())
    for other in ("dev", "validation"):
        overlap = holdout & prompts_by_split.get(other, set())
        if overlap:
            rep.errors.append(
                f"HOLDOUT contamination: {len(overlap)} prompt(s) appear in both "
                f"holdout and {other}")

    # Safety-holdout distribution (docs/SEVERITY_TAXONOMY.md)
    safety = [i for i in items if i.severity in ("high", "critical") and i.split == "holdout"]
    if safety:
        n = len(safety)
        subjects = Counter(i.subject for i in safety)
        if len(subjects) < MIN_SUBJECTS:
            rep.errors.append(
                f"safety holdout spans {len(subjects)} subject(s), minimum {MIN_SUBJECTS}: "
                "a narrow safety set yields a zero-CME result that means little")
        for subj, count in subjects.items():
            if count / n > MAX_SUBJECT_SHARE:
                rep.errors.append(
                    f"safety holdout subject '{subj}' is {count/n:.1%} of items, "
                    f"maximum {MAX_SUBJECT_SHARE:.0%}")
        cats = Counter(i.cme_category_targeted for i in safety if i.cme_category_targeted)
        substantive = [c for c, k in cats.items() if k / n >= MIN_CME_CATEGORY_SHARE]
        if len(substantive) < MIN_CME_CATEGORIES:
            rep.errors.append(
                f"safety holdout substantively covers {len(substantive)} CME categories "
                f"(>={MIN_CME_CATEGORY_SHARE:.0%} each), minimum {MIN_CME_CATEGORIES}")
        for cat, count in cats.items():
            if count / n > MAX_CME_CATEGORY_SHARE:
                rep.errors.append(
                    f"CME category '{cat}' targeted by {count/n:.1%} of safety items, "
                    f"maximum {MAX_CME_CATEGORY_SHARE:.0%}")

    rep.n_items = len(items)
    rep.by_track = dict(Counter(i.track for i in items))
    rep.by_split = dict(Counter(i.split for i in items))
    rep.ok = not rep.errors
    return rep
