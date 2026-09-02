"""
The holdout set, and the one path by which it may be scored.

WHAT IS ACTUALLY GUARDED HERE, AND WHAT CANNOT BE
--------------------------------------------------
The holdout items live in this repository in plain text, and anybody who works
on the validator can read them. Encrypting them would be theatre: the same
person holds the key. So this module does not claim to hide the labels. It
guards the thing that actually destroys a holdout in practice, which is not one
person reading it -- it is scoring against it repeatedly.

A holdout scored once measures the validator. A holdout scored forty times,
with a change between each attempt, measures nothing: the fortieth result is
the best of forty draws, and the selection was made by a human reading the
holdout's own score. That is the leak, it happens without anybody intending it,
and it is stoppable by a rule a machine can enforce:

    1. ONE SCORE PER VALIDATOR. The fingerprint is a hash of the validator's
       source and its configuration. Scoring the same fingerprint twice is
       refused; the earlier result stands and is returned.

    2. A BUDGET. `MAX_USES` scoring runs in total. Not a suggestion -- the
       call raises past it, and raising the limit is a commit somebody has to
       make deliberately and defend.

    3. AN APPEND-ONLY LEDGER, committed to the repository. Every run is
       recorded with its fingerprint, the corpus hash, the counts and the
       outcome. Twelve entries against a holdout of 60 items is visible to a
       reader in a way "we validated on a holdout" is not.

    4. A CORPUS HASH. The holdout content is hashed into every entry. Editing
       the holdout after a run that failed shows up as a changed hash beside
       the earlier entries, so the edit is a fact in the record rather than an
       invisible improvement.

WHAT THE DEVELOPMENT SET IS FOR
-------------------------------
Everything else. Read it, argue with it, tune against it, score it a thousand
times. It costs nothing because it was never the evidence.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from validator import metrics
from validator.devset import CLEAN, DEFECTIVE, DevsetError, load

HOLDOUT_ROOT = Path("corpus/validator_holdout")
LEDGER_PATH = HOLDOUT_ROOT / "ledger.jsonl"
VALIDATOR_SOURCE = Path("validator")

# Total scoring runs permitted against this holdout, ever. Raising it is a
# commit, and one that should carry a reason.
MAX_USES = 5

ARM_LABEL = {CLEAN: metrics.CLEAN, DEFECTIVE: metrics.DEFECTIVE}


class HoldoutRefused(RuntimeError):
    """The holdout may not be scored again, with the reason and the record."""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def corpus_hash(root: str | Path = HOLDOUT_ROOT) -> str:
    """A hash over the holdout's content, so a later edit is visible in the ledger."""
    root = Path(root)
    parts = []
    for name in sorted(p.name for p in root.glob("*.jsonl") if p.name != LEDGER_PATH.name):
        parts.append(name)
        parts.append((root / name).read_text(encoding="utf-8"))
    return sha256_text("\n".join(parts))


#: Hashed alongside `validator/` because it decides what the validator SEES.
#: `providers/nvidia.py` read only `message.content`; against a reasoning
#: candidate that field is null, the JSON extractor was handed None, and every
#: such item became a backend outage. Phase 0 on 2026-09-02 lost items to it.
#:
#: Before this, that file was outside the fingerprint: fixing it changed what a
#: run measures while leaving the digest identical, so the repaired run and the
#: broken one would have been stamped the same and read as comparable. A
#: fingerprint that excludes the code turning an HTTP reply into a parsed
#: answer is not fingerprinting the validator.
ADAPTER_SOURCES = (Path("benchmark/providers"),)


def validator_fingerprint(config_label: str = "",
                          source: str | Path = VALIDATOR_SOURCE,
                          adapters=ADAPTER_SOURCES) -> str:
    """
    A hash of the validator's source, its provider adapters, and its
    configuration.

    Two runs with the same fingerprint are the same validator, and scoring the
    same validator twice against the holdout adds no information while spending
    a use. Changing a prompt, a threshold, which layers run, or how a reply is
    unpacked changes the fingerprint, which is the point.
    """
    parts = [config_label]
    roots = [Path(source)] + [Path(a) for a in (adapters or ())]
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            parts.append(str(path))
            parts.append(path.read_text(encoding="utf-8"))
    return sha256_text("\n".join(parts))


# What a ledger row records. A scoring run and a look are both spends, and
# recording only the first would make the ledger a flattering account.
KIND_SCORE = "score"
KIND_INSPECTION = "inspection"


@dataclass(frozen=True)
class LedgerEntry:
    at: str
    fingerprint: str
    config: str
    corpus: str
    outcome: str
    sensitivity: float | None
    specificity: float | None
    counts: dict
    note: str
    kind: str = KIND_SCORE

    def as_dict(self) -> dict:
        return {"at": self.at, "kind": self.kind, "fingerprint": self.fingerprint,
                "config": self.config, "corpus": self.corpus, "outcome": self.outcome,
                "sensitivity": self.sensitivity, "specificity": self.specificity,
                "counts": self.counts, "note": self.note}


def read_ledger(path: str | Path = LEDGER_PATH) -> list[LedgerEntry]:
    path = Path(path)
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        raw = json.loads(line)
        out.append(LedgerEntry(
            at=raw["at"], fingerprint=raw["fingerprint"], config=raw.get("config", ""),
            corpus=raw.get("corpus", ""), outcome=raw.get("outcome", ""),
            sensitivity=raw.get("sensitivity"), specificity=raw.get("specificity"),
            counts=raw.get("counts") or {}, note=raw.get("note", ""),
            kind=raw.get("kind", KIND_SCORE)))
    return out


def _append(entry: LedgerEntry, path: str | Path = LEDGER_PATH) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry.as_dict(), ensure_ascii=True) + "\n")


def score(run, *, config_label: str, note: str = "",
          root: str | Path = HOLDOUT_ROOT, ledger_path: str | Path = LEDGER_PATH,
          dev_root: str | Path = "corpus/validator_dev",
          source: str | Path = VALIDATOR_SOURCE,
          max_uses: int = MAX_USES):
    """
    Score a validator against the holdout, once.

    `run(cases)` must return one verdict object per case, each with `.item_id`
    and `.verdict`. Returns (gate, matrix, entry).

    Refuses, rather than warns, when the same validator has already been scored
    or the budget is spent. A warning would be ignored on the day it mattered.
    """
    if not note.strip():
        raise HoldoutRefused(
            "a holdout run needs a note saying what changed since the last one. Spending a "
            "use without recording why is how a ledger becomes a list of numbers nobody can "
            "interpret six months later.")

    holdout = load(root)
    from validator.devset import assert_disjoint
    try:
        assert_disjoint(load(dev_root), holdout)
    except DevsetError as exc:
        raise HoldoutRefused(
            f"refusing to score: the holdout overlaps the development set -- {exc}") from exc

    entries = [e for e in read_ledger(ledger_path) if e.kind == KIND_SCORE]
    fingerprint = validator_fingerprint(config_label, source)
    digest = corpus_hash(root)

    previous = next((e for e in entries if e.fingerprint == fingerprint), None)
    if previous is not None:
        raise HoldoutRefused(
            f"this exact validator was already scored against the holdout on {previous.at} "
            f"and the result was {previous.outcome} (sensitivity "
            f"{_fmt(previous.sensitivity)}, specificity {_fmt(previous.specificity)}). "
            "Scoring it again cannot change what it measures. Change the validator, then "
            "score it.")
    if len(entries) >= max_uses:
        raise HoldoutRefused(
            f"the holdout budget of {max_uses} scoring runs is spent ({len(entries)} used). "
            "Each additional run makes the best result a little more a property of how many "
            "attempts were made. Build a new holdout, or raise the budget in a commit that "
            "says why.")
    drifted = {e.corpus for e in entries if e.corpus and e.corpus != digest}
    if drifted:
        raise HoldoutRefused(
            "the holdout content has changed since an earlier scoring run. Comparing a new "
            "result against the ledger's older ones would be comparing two different tests. "
            "Start a new ledger alongside the new holdout.")

    verdicts = run(holdout.cases)
    by_id = {v.item_id: v for v in verdicts}
    labels, said = [], []
    for case in holdout.cases:
        if not case.in_arm:
            continue
        verdict = by_id.get(case.id)
        if verdict is None:
            continue
        labels.append(ARM_LABEL[case.label])
        said.append(verdict.verdict)
    matrix = metrics.confusion(labels, said)
    gate = metrics.gate(matrix)

    entry = LedgerEntry(
        at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        fingerprint=fingerprint, config=config_label, corpus=digest,
        outcome=gate.outcome, sensitivity=matrix.sensitivity,
        specificity=matrix.specificity,
        counts=matrix.as_dict()["counts"], note=note.strip())
    _append(entry, ledger_path)
    return gate, matrix, entry


def note_inspection(what: str, *, root: str | Path = HOLDOUT_ROOT,
                    ledger_path: str | Path = LEDGER_PATH) -> LedgerEntry:
    """
    Record that somebody looked at the holdout without scoring against it.

    Reading the holdout is not free even when no validator was measured. A
    ceiling run, a spot check, a glance at one item to settle an argument --
    each moves information out of the holdout and into the person building the
    validator, and none of it shows up in a scoring count. Recording the look
    is the only thing that makes it visible later, and it costs a line.

    It does not consume a scoring use, and it is not meant to discourage
    looking. It is meant to stop a holdout that was read fifteen times being
    described afterwards as untouched.
    """
    if not what.strip():
        raise HoldoutRefused("recording a look at the holdout needs a note saying what for")
    entry = LedgerEntry(
        at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        fingerprint="", config="", corpus=corpus_hash(root), outcome="",
        sensitivity=None, specificity=None, counts={}, note=what.strip(),
        kind=KIND_INSPECTION)
    _append(entry, ledger_path)
    return entry


def _fmt(value):
    return "n/a" if value is None else f"{value:.0%}"


def remaining(ledger_path: str | Path = LEDGER_PATH, max_uses: int = MAX_USES) -> int:
    scored = [e for e in read_ledger(ledger_path) if e.kind == KIND_SCORE]
    return max(0, max_uses - len(scored))
