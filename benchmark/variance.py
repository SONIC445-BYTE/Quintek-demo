"""
Re-run variance protocol.

docs/VARIANCE_PROTOCOL.md: "Most APIs are not guaranteed to be deterministic
even at temperature 0. A seed parameter, when available, is metadata -- not
proof of deterministic execution." This module actually re-executes items --
DEV characterization (>= N repeats per item) and the official-run sentinel
re-run (a random fraction re-executed in a second batch) -- rather than only
declaring the reliability gates someone else must satisfy.

Storage rule from the same doc: store every individual execution, never
overwrite an earlier stochastic result with the re-run. Every function here
returns (or, when given a path, appends) the full set of raw responses;
nothing is averaged away before it reaches disk.

Repeat counts and sentinel fraction come from `configs/v0_4.yaml`
(`variance.dev_characterization_runs`, `variance.sentinel_fraction`), not
literals in this file. The gate thresholds these values feed
(`GATE-REL-VARIANCE-ANSWER` / `-VALIDATOR` / `-GENERATION`) live in
`configs/gate_registry_v0_4.json` and are read by `benchmark.gates`, never
restated here -- this module only produces the raw metric value each gate
compares against its own registered threshold.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .providers.base import GenerationRequest, GenerationResponse, ModelProvider


def _norm(s) -> str:
    return str(s).strip().lower()


@dataclass
class VarianceConfig:
    dev_characterization_runs: int = 3
    sentinel_fraction: float = 0.10
    seed: int = 20260814

    @classmethod
    def from_yaml(cls, cfg: dict) -> "VarianceConfig":
        v = cfg.get("variance", {}) or {}
        return cls(
            dev_characterization_runs=int(v.get("dev_characterization_runs", 3)),
            sentinel_fraction=float(v.get("sentinel_fraction", 0.10)),
        )


def _append(path: str | Path | None, record: dict) -> None:
    if path is None:
        return
    with open(path, "a") as fh:
        fh.write(json.dumps(record) + "\n")


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------

def characterize(
    provider: ModelProvider, items, config: VarianceConfig,
    store_path: str | Path | None = None,
) -> dict[str, list[GenerationResponse]]:
    """
    DEV characterization: run every item `config.dev_characterization_runs`
    times. Returns {item_id: [response, ...]}. Every execution is appended
    (never overwritten) to `store_path` if given.
    """
    results: dict[str, list[GenerationResponse]] = {}
    for it in items:
        runs = []
        for rep in range(config.dev_characterization_runs):
            resp = provider.generate(GenerationRequest(
                item_id=it.id, prompt=it.prompt,
                metadata={"gold_answer": it.gold.get("answer"),
                          "options": it.gold.get("options"), "repeat_index": rep}))
            runs.append(resp)
            record = resp.as_dict()
            record["repeat_index"] = rep
            _append(store_path, record)
        results[it.id] = runs
    return results


def select_sentinel_sample(item_ids: list[str], config: VarianceConfig) -> list[str]:
    rng = random.Random(config.seed)
    n = round(len(item_ids) * config.sentinel_fraction)
    return rng.sample(item_ids, min(n, len(item_ids)))


def sentinel_rerun(
    provider: ModelProvider, items, original_responses: dict[str, GenerationResponse],
    config: VarianceConfig, store_path: str | Path | None = None,
) -> dict[str, tuple[GenerationResponse | None, GenerationResponse]]:
    """
    Re-executes a random `sentinel_fraction` of items in a SECOND execution
    batch, per docs/VARIANCE_PROTOCOL.md, and pairs each with its original
    response. The original response object is never mutated; this only ever
    appends a new record.
    """
    by_id = {it.id: it for it in items}
    sample = select_sentinel_sample(list(by_id), config)
    pairs: dict[str, tuple] = {}
    for iid in sample:
        it = by_id[iid]
        rerun = provider.generate(GenerationRequest(
            item_id=it.id, prompt=it.prompt,
            metadata={"gold_answer": it.gold.get("answer"), "options": it.gold.get("options")}))
        pairs[iid] = (original_responses.get(iid), rerun)
        record = rerun.as_dict()
        record["rerun_of"] = iid
        _append(store_path, record)
    return pairs


# --------------------------------------------------------------------------
# Disagreement metrics
# --------------------------------------------------------------------------

def _usable_pairs(pairs: dict[str, tuple]):
    for orig, rerun in pairs.values():
        if orig is None or rerun is None:
            continue
        if not orig.ok or not rerun.ok:
            continue  # a transport failure is not evidence of nondeterminism
        if orig.parsed is None or rerun.parsed is None:
            continue
        yield orig, rerun


def answer_disagreement_rate(
    pairs: dict[str, tuple], extract: Callable[[dict], object] | None = None
) -> float | None:
    """GATE-REL-VARIANCE-ANSWER. `extract` pulls the comparable field out of
    `.parsed`; defaults to the medical_qa/robustness 'answer' field."""
    extract = extract or (lambda p: p.get("answer"))
    disagree = n = 0
    for orig, rerun in _usable_pairs(pairs):
        n += 1
        if _norm(extract(orig.parsed)) != _norm(extract(rerun.parsed)):
            disagree += 1
    return (disagree / n) if n else None


def validator_decision_disagreement_rate(pairs: dict[str, tuple]) -> float | None:
    """GATE-REL-VARIANCE-VALIDATOR. Compares the validator's approve/reject
    decision, using the same criteria as scorers.deterministic's false-approval
    scorer so the two stay consistent."""
    def decision(p: dict) -> bool:
        return bool(p.get("medical_correct") and p.get("answer_key_correct")
                    and p.get("single_best") and not p.get("critical_error"))
    disagree = n = 0
    for orig, rerun in _usable_pairs(pairs):
        n += 1
        if decision(orig.parsed) != decision(rerun.parsed):
            disagree += 1
    return (disagree / n) if n else None


def generation_score_mean_absolute_difference(
    score_pairs: list[tuple[float, float]],
) -> float | None:
    """GATE-REL-VARIANCE-GENERATION. score_pairs: [(original, rerun), ...]
    on the same 0-4 rubric item across two independent scoring passes."""
    if not score_pairs:
        return None
    return sum(abs(a - b) for a, b in score_pairs) / len(score_pairs)


def reliability_from_variance(
    answer_pairs: dict[str, tuple] | None = None,
    validator_pairs: dict[str, tuple] | None = None,
    generation_score_pairs: list[tuple[float, float]] | None = None,
) -> dict[str, float | None]:
    """
    Assembles the reliability dict shape `benchmark.gates.evaluate_run`
    consumes directly (gate_id -> value; None means "not measured", which
    `evaluate_run` treats as UNEVALUABLE, never as a passing 0).

    `GATE-REL-KAPPA-CRITICAL` is NOT produced here -- that comes from human
    review (`benchmark.stats.cohens_kappa` fed by `benchmark.adjudication`),
    not from re-running a model against itself. Variance and inter-rater
    reliability are different questions and this module answers only the
    first.
    """
    return {
        "GATE-REL-VARIANCE-ANSWER": (
            answer_disagreement_rate(answer_pairs) if answer_pairs is not None else None),
        "GATE-REL-VARIANCE-VALIDATOR": (
            validator_decision_disagreement_rate(validator_pairs)
            if validator_pairs is not None else None),
        "GATE-REL-VARIANCE-GENERATION": (
            generation_score_mean_absolute_difference(generation_score_pairs)
            if generation_score_pairs is not None else None),
    }
