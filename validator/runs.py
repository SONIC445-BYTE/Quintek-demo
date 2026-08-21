"""
The record of what has actually been run, and the rule that keeps it honest.

WHY THIS FILE EXISTS
--------------------
A project slides from "76 tests pass" to "our medical validator is validated"
one sentence at a time, and never in a commit anybody would refuse. The way to
stop it is to make the claim derivable rather than writable: every status this
repository reports about the validator is computed from records on disk, and a
status nobody earned has nowhere to come from.

THE ONE DISTINCTION EVERYTHING RESTS ON
---------------------------------------
    A CEILING RUN uses ground-truth oracles in place of models. It measures
    whether the DESIGN carries enough information to detect a defect class. It
    is free, it is useful, and it is not a measurement of any validator.

    A REAL RUN uses models. It measures what the validator actually does.

`is_oracle` travels on every provider record, `real_runs()` excludes them, and
`development_metrics()` returns NOT_RUN when the only runs on disk are ceilings.
The 100/100 ceiling this design reached cannot be reported as performance
because there is no code path that would let it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

RUNS_DIR = Path("reports/validator_runs")

KIND_CEILING = "ceiling"
KIND_DEVELOPMENT = "development"
KIND_HOLDOUT = "holdout"

NOT_RUN = "NOT_RUN"


@dataclass
class ProviderRecord:
    role: str
    provider: str
    model: str
    model_family: str = ""
    is_oracle: bool = False
    is_model: bool = True

    def as_dict(self) -> dict:
        return {"role": self.role, "provider": self.provider, "model": self.model,
                "model_family": self.model_family, "is_oracle": self.is_oracle,
                "is_model": self.is_model}


@dataclass
class Run:
    at: str
    kind: str
    corpus: str
    corpus_hash: str
    validator_version: str
    config: str
    providers: list[ProviderRecord] = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    sensitivity: float | None = None
    specificity: float | None = None
    gate: str = ""
    outages: int = 0
    analysis: dict = field(default_factory=dict)
    note: str = ""
    freeze: str = ""
    completeness: str = ""
    items_expected: int = 0
    items_decided: int = 0
    path: str = ""

    @property
    def used_an_oracle(self) -> bool:
        return any(p.is_oracle for p in self.providers)

    @property
    def used_a_test_double(self) -> bool:
        return any(not p.is_model for p in self.providers)

    @property
    def is_real(self) -> bool:
        """
        A run that measures a validator rather than a design.

        Both exclusions are needed and they are different. An oracle answers
        from ground truth; a replay provider answers from a fixture. Neither
        was asked anything, and a record that counted either would say a
        validator had been evaluated when no model had seen an item.
        """
        return (bool(self.providers) and not self.used_an_oracle
                and not self.used_a_test_double)

    def as_dict(self) -> dict:
        return {"at": self.at, "kind": self.kind, "corpus": self.corpus,
                "corpus_hash": self.corpus_hash,
                "validator_version": self.validator_version, "config": self.config,
                "providers": [p.as_dict() for p in self.providers],
                "used_an_oracle": self.used_an_oracle,
                "used_a_test_double": self.used_a_test_double, "is_real": self.is_real,
                "counts": self.counts, "sensitivity": self.sensitivity,
                "specificity": self.specificity, "gate": self.gate,
                "outages": self.outages, "analysis": self.analysis, "note": self.note,
                "freeze": self.freeze, "completeness": self.completeness,
                "items_expected": self.items_expected,
                "items_decided": self.items_decided}


def describe_provider(role: str, provider) -> ProviderRecord:
    return ProviderRecord(
        role=role, provider=str(getattr(provider, "name", "unknown")),
        model=str(getattr(provider, "model", "unknown")),
        model_family=str(getattr(provider, "model_family", "") or ""),
        is_oracle=bool(getattr(provider, "is_oracle", False)),
        is_model=bool(getattr(provider, "is_model", True)))


def record(run: Run, *, runs_dir: str | Path = RUNS_DIR) -> Path:
    runs_dir = Path(runs_dir)
    runs_dir.mkdir(parents=True, exist_ok=True)
    stamp = run.at.replace(":", "-")
    name = f"{stamp}_{run.kind}_{run.config.replace('/', '-')}.json"
    path = runs_dir / name
    path.write_text(json.dumps(run.as_dict(), indent=2, default=str), encoding="utf-8")
    run.path = str(path)
    return path


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_all(runs_dir: str | Path = RUNS_DIR) -> list[Run]:
    runs_dir = Path(runs_dir)
    if not runs_dir.exists():
        return []
    out = []
    for path in sorted(runs_dir.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        out.append(Run(
            at=raw.get("at", ""), kind=raw.get("kind", ""), corpus=raw.get("corpus", ""),
            corpus_hash=raw.get("corpus_hash", ""),
            validator_version=raw.get("validator_version", ""),
            config=raw.get("config", ""),
            providers=[ProviderRecord(**p) for p in (raw.get("providers") or [])],
            counts=raw.get("counts") or {}, sensitivity=raw.get("sensitivity"),
            specificity=raw.get("specificity"), gate=raw.get("gate", ""),
            outages=int(raw.get("outages") or 0), analysis=raw.get("analysis") or {},
            note=raw.get("note", ""), freeze=raw.get("freeze", ""),
            completeness=raw.get("completeness", ""),
            items_expected=int(raw.get("items_expected") or 0),
            items_decided=int(raw.get("items_decided") or 0), path=str(path)))
    return out


def real_runs(kind: str = "", runs_dir: str | Path = RUNS_DIR) -> list[Run]:
    """Runs that used models. Ceiling runs are excluded and cannot be reinstated."""
    return [r for r in load_all(runs_dir) if r.is_real and (not kind or r.kind == kind)]


def development_metrics(runs_dir: str | Path = RUNS_DIR) -> dict:
    """
    The most recent REAL development run, or NOT_RUN.

    Deliberately not "the best run". A status report that quotes the best of
    several attempts is reporting the selection, not the validator.
    """
    runs = real_runs(KIND_DEVELOPMENT, runs_dir)
    if not runs:
        return {"status": NOT_RUN,
                "why": "no development run using real models has been recorded; the runs on "
                       "disk used ground-truth oracles or scripted fixtures, and neither "
                       "asked a model anything"}
    latest = max(runs, key=lambda r: r.at)
    counts = latest.counts or {}
    return {"status": "RUN", "at": latest.at, "config": latest.config,
            "models": [p.as_dict() for p in latest.providers],
            "sensitivity": latest.sensitivity, "specificity": latest.specificity,
            "fp": counts.get("false_positive"), "fn": counts.get("false_negative"),
            "gate": latest.gate, "outages": latest.outages,
            "runs_recorded": len(runs), "path": latest.path}
