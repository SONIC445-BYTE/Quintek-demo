"""
The frozen experiment configuration, and what happens when it moves.

WHY A FREEZE
------------
An ablation compares three runs. If anything about the validator changes
between them -- a prompt version, a temperature, a corpus edit, a threshold --
then the difference between the runs is partly that change, and the comparison
answers a question nobody asked. The failure looks like this and is invisible
afterwards:

    A+B+D    -> v0.4
    C        -> v0.4.1
    A+B+C+D  -> v0.4.2

Nothing in the numbers says it happened. So the configuration is captured
before the first run, hashed, stamped onto every run in the set, and checked
before each subsequent one. A run whose freeze digest differs from the set's is
not part of the set.

WHAT IS PINNED
--------------
Everything that could change an answer: the validator source itself, the
corpus content, the three prompt versions, the gate thresholds, the judge's
confidence floor, the model ids and families, the endpoint, the sampling
parameters, and the experiment definitions. Not the wall clock, and not which
machine ran it.

NO SECRETS
----------
An endpoint URL is recorded; an API key is not, is not hashed into the digest,
and has no field to live in. A manifest is an artifact that gets committed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from validator import conformance, grounding, judge, metrics, pipeline

FREEZE_DIR = Path("reports/validator_runs")
FREEZE_NAME = "freeze.json"

# Keys that must never appear in a manifest, whatever a caller passes.
FORBIDDEN_KEYS = ("api_key", "apikey", "key", "token", "secret", "password",
                  "authorization", "auth")


class FreezeViolation(RuntimeError):
    """The configuration moved during an experiment set, naming what moved."""


@dataclass
class Freeze:
    validator_version: str
    validator_fingerprint: str
    corpus: str
    corpus_hash: str
    prompt_versions: dict
    thresholds: dict
    models: list[dict]
    sampling: dict
    experiments: list[dict]
    note: str = ""
    created_at: str = ""
    extra: dict = field(default_factory=dict)

    def payload(self) -> dict:
        """Everything the digest covers. Deliberately excludes note and time."""
        return {
            "validator_version": self.validator_version,
            "validator_fingerprint": self.validator_fingerprint,
            "corpus": self.corpus, "corpus_hash": self.corpus_hash,
            "prompt_versions": self.prompt_versions, "thresholds": self.thresholds,
            "models": self.models, "sampling": self.sampling,
            "experiments": self.experiments, "extra": self.extra,
        }

    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps(self.payload(), sort_keys=True).encode("utf-8")).hexdigest()

    def as_dict(self) -> dict:
        return {**self.payload(), "note": self.note, "created_at": self.created_at,
                "digest": self.digest()}

    def differences(self, other: "Freeze") -> list[str]:
        mine, theirs = self.payload(), other.payload()
        out = []
        for key in sorted(set(mine) | set(theirs)):
            if mine.get(key) != theirs.get(key):
                out.append(f"{key}: frozen {theirs.get(key)!r} -> now {mine.get(key)!r}")
        return out


def _scrub(value):
    """Refuse a manifest that would carry a credential into a committed file."""
    if isinstance(value, dict):
        for key in value:
            if str(key).strip().lower() in FORBIDDEN_KEYS:
                raise FreezeViolation(
                    f"refusing to freeze a configuration containing {key!r}. A manifest is "
                    "committed; a credential is not, and hashing one into an experiment "
                    "digest would put it in the record permanently.")
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def describe_model(role: str, provider, *, seat: str = "candidate",
                   endpoint: str = "", temperature: float = 0.0) -> dict:
    """
    One model in the manifest, identified by its seat as well as its layer.

    `seat` is the experimental role -- the model under evaluation, or the
    independent judge. `role` is the layer it was called for. A manifest that
    records only the layer cannot answer "which model was being evaluated",
    which is the first question anyone asks of a benchmark record.
    """
    return _scrub({
        "seat": seat,
        "role": role,
        "provider": str(getattr(provider, "name", "unknown")),
        "model": str(getattr(provider, "model", "unknown")),
        "model_version": str(getattr(provider, "model_version", "") or ""),
        "model_family": str(getattr(provider, "model_family", "") or ""),
        "is_model": bool(getattr(provider, "is_model", True)),
        "is_oracle": bool(getattr(provider, "is_oracle", False)),
        "endpoint": endpoint,
        "temperature": temperature,
    })


def build(*, corpus: str, corpus_hash: str, models: list[dict],
          experiments: list[dict], sampling: dict | None = None,
          note: str = "", created_at: str = "", extra: dict | None = None) -> Freeze:
    from validator.holdout import validator_fingerprint
    return Freeze(
        validator_version=pipeline.VALIDATOR_VERSION,
        validator_fingerprint=validator_fingerprint(pipeline.Config().label()),
        corpus=corpus, corpus_hash=corpus_hash,
        prompt_versions={
            "grounding": grounding.PROMPT_VERSION,
            "judge": judge.PROMPT_VERSION,
            "conformance": conformance.PROMPT_VERSION,
        },
        thresholds={
            "min_sensitivity": metrics.MIN_SENSITIVITY,
            "min_specificity": metrics.MIN_SPECIFICITY,
            "min_items_per_arm": metrics.MIN_ITEMS_PER_ARM,
            "judge_confidence_floor": judge.CONFIDENCE_FLOOR,
        },
        models=_scrub(models), sampling=_scrub(sampling or {"temperature": 0.0}),
        experiments=_scrub(experiments), note=note, created_at=created_at,
        extra=_scrub(extra or {}))


def path_for(corpus: str, directory: str | Path = FREEZE_DIR) -> Path:
    slug = str(corpus).strip("/").replace("/", "_")
    return Path(directory) / f"{slug}_{FREEZE_NAME}"


def write(freeze: Freeze, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(freeze.as_dict(), indent=2), encoding="utf-8")
    return path


def load(path: str | Path) -> Freeze | None:
    path = Path(path)
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    return Freeze(
        validator_version=raw["validator_version"],
        validator_fingerprint=raw["validator_fingerprint"],
        corpus=raw["corpus"], corpus_hash=raw["corpus_hash"],
        prompt_versions=raw["prompt_versions"], thresholds=raw["thresholds"],
        models=raw["models"], sampling=raw["sampling"], experiments=raw["experiments"],
        note=raw.get("note", ""), created_at=raw.get("created_at", ""),
        extra=raw.get("extra") or {})


def assert_unchanged(current: Freeze, path: str | Path) -> Freeze:
    """
    Compare the current configuration against the frozen one.

    Returns the freeze in force. Raises when anything the digest covers has
    moved, naming each field, because "the configuration changed" is not
    actionable and "the judge prompt went from judge/0.1.0 to judge/0.2.0" is.
    """
    frozen = load(path)
    if frozen is None:
        return current
    if frozen.digest() == current.digest():
        return frozen
    raise FreezeViolation(
        "the experiment configuration has moved since it was frozen, so runs made now "
        "cannot be compared with runs already in this set:\n  "
        + "\n  ".join(current.differences(frozen))
        + f"\n\nEither restore the frozen configuration, or start a new set: delete "
          f"{path} and rerun every experiment in it. Comparing across a change is how an "
          "ablation reports a prompt edit as a layer's contribution.")
