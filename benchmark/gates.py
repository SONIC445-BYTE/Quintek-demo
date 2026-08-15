"""
Gate evaluation.

Every threshold is read from configs/gate_registry_v0_4.json. Nothing in this
module hardcodes a number -- that is the defect v0.4 exists to remove, and it
would reappear the moment a literal is typed here.

A gate can land in one of five states, and the distinctions matter:

  PASS         evaluated at adequate n, CI clears the threshold
  FAIL         evaluated at adequate n, CI does not clear
  CONDITIONAL  misses by no more than the registered tolerance (non-safety only)
  UNEVALUABLE  below registered min_n, or the metric could not be computed
  NOT_APPLICABLE  gate does not apply to this candidate (e.g. tool gates for a
                  tool-free candidate)

UNEVALUABLE is not PASS and is not FAIL. Collapsing it into either is how a
benchmark reports a result it does not have.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import stats


PASS = "PASS"
FAIL = "FAIL"
CONDITIONAL = "CONDITIONAL"
UNEVALUABLE = "UNEVALUABLE"
NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass
class GateResult:
    gate_id: str
    track: str
    metric: str
    status: str
    estimate: float | None = None
    ci_lower: float | None = None
    ci_upper: float | None = None
    n: int = 0
    required_n: int = 0
    n_unit: str = ""
    threshold: float | None = None
    direction: str = ""
    mandatory: bool = True
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "gate_id": self.gate_id,
            "track": self.track,
            "metric": self.metric,
            "status": self.status,
            "estimate": self.estimate,
            "ci_lower": self.ci_lower,
            "ci_upper": self.ci_upper,
            "n": self.n,
            "required_n": self.required_n,
            "n_unit": self.n_unit,
            "threshold": self.threshold,
            "direction": self.direction,
            "mandatory": self.mandatory,
            "reason": self.reason,
        }


@dataclass
class Measurement:
    """
    What a scorer hands to the gate engine.

    Either (successes, n) for a proportion, or `values` / `clusters` for a mean.
    `applicable=False` marks a gate that does not apply to this candidate.
    """

    successes: int | None = None
    n: int = 0
    values: list[float] | None = None
    clusters: dict[str, list[float]] | None = None
    applicable: bool = True
    note: str = ""


class GateRegistry:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.raw = json.loads(self.path.read_text())
        self.version = self.raw["version"]
        self.calibration_state = self.raw["calibration_state"]
        self.tracks = self.raw["tracks"]
        self.reliability = self.raw["reliability_gates"]
        self.outcome_states = self.raw["outcome_states"]
        self.tolerance = self.raw["tolerance"]["conditional_band_relative"]
        self.integrity_checks = self.raw["integrity_preconditions"]["checks"]

    @property
    def is_calibrated(self) -> bool:
        """
        Thresholds are engineering starting points until calibrated on DEV and
        frozen. An uncalibrated run cannot yield an official PASS.
        """
        return not self.calibration_state.startswith("UNCALIBRATED")

    def mandatory_track_ids(self) -> list[str]:
        return [k for k, v in self.tracks.items() if v.get("mandatory")]


def _ci_for(spec: dict, m: Measurement) -> stats.Interval | None:
    """Select the CI method the registry specifies for this gate."""
    method = spec["ci"]

    if method == "wilson_cluster_by_base_item":
        # Perturbations of one base item are not independent. Cluster-bootstrap
        # the per-cluster retention rate rather than applying Wilson to the flat
        # count, which would understate the interval.
        if m.clusters:
            return stats.bootstrap_cluster(m.clusters)
        if m.successes is None:
            return None
        return stats.wilson(m.successes, m.n)

    if method == "wilson":
        if m.successes is None:
            return None
        return stats.wilson(m.successes, m.n)

    if method in ("clopper_pearson_one_sided_upper", "exact_binomial"):
        if m.successes is None:
            return None
        ub = stats.clopper_pearson_upper_one_sided(m.successes, m.n)
        return stats.Interval(
            estimate=m.successes / m.n if m.n else 0.0,
            lower=0.0,
            upper=ub,
            n=m.n,
            method="clopper_pearson_one_sided_upper",
        )

    if method == "bootstrap_cluster_by_item":
        if m.clusters:
            return stats.bootstrap_cluster(m.clusters)
        if m.values:
            return stats.bootstrap_mean(m.values)
        return None

    if method == "bootstrap":
        if m.clusters:
            return stats.bootstrap_cluster(m.clusters)
        if m.values:
            return stats.bootstrap_mean(m.values)
        if m.successes is not None:
            return stats.wilson(m.successes, m.n)
        return None

    raise ValueError(f"unknown CI method in registry: {method}")


def evaluate_gate(gate_key: str, spec: dict, m: Measurement, tolerance: float) -> GateResult:
    gid = spec["gate_id"]
    base = dict(
        gate_id=gid,
        track=gate_key,
        metric=spec["metric"],
        required_n=spec["min_n"],
        n_unit=spec.get("n_unit", ""),
        threshold=spec["threshold"],
        direction=spec["direction"],
        mandatory=spec.get("mandatory", True),
    )

    if not m.applicable:
        return GateResult(status=NOT_APPLICABLE, n=0,
                          reason=m.note or "gate does not apply to this candidate", **base)

    if m.n < spec["min_n"]:
        return GateResult(
            status=UNEVALUABLE, n=m.n,
            reason=f"n={m.n} below registered min_n={spec['min_n']} "
                   f"({spec.get('n_unit','')}); a gate below its minimum is not a result",
            **base,
        )

    ci = _ci_for(spec, m)
    if ci is None:
        return GateResult(status=UNEVALUABLE, n=m.n,
                          reason="metric could not be computed from supplied measurement", **base)

    direction, threshold = spec["direction"], spec["threshold"]
    est, lo, hi = ci.estimate, ci.lower, ci.upper

    if direction == "lower":
        passed = lo >= threshold
        margin = (threshold - lo) / threshold if threshold else 0.0
    elif direction == "upper":
        passed = hi <= threshold
        margin = (hi - threshold) / threshold if threshold else float("inf")
    else:  # equal, i.e. zero-event safety gates
        zero = (m.successes == 0)
        bound_ok = hi <= spec.get("max_tolerated_upper_bound", float("inf"))
        passed = zero and bound_ok
        margin = 0.0
        if not passed:
            reason = (f"{m.successes} confirmed event(s)" if not zero else
                      f"zero events but exact upper bound {hi:.5f} exceeds tolerated "
                      f"{spec.get('max_tolerated_upper_bound')}; increase n")
            return GateResult(status=FAIL, estimate=est, ci_lower=lo, ci_upper=hi,
                              n=m.n, reason=reason, **base)
        return GateResult(status=PASS, estimate=est, ci_lower=lo, ci_upper=hi, n=m.n,
                          reason=f"0/{m.n}, exact upper bound {hi:.5f}", **base)

    if passed:
        status, reason = PASS, ""
    elif spec.get("overrides_all_other_gates") or gate_key.startswith("safety"):
        status, reason = FAIL, "safety gate: no tolerance band"
    elif 0 < margin <= tolerance:
        status = CONDITIONAL
        reason = f"misses by {margin:.3%} relative, within {tolerance:.1%} tolerance"
    else:
        status = FAIL
        reason = f"CI does not clear threshold (miss {margin:.3%} relative)"

    return GateResult(status=status, estimate=est, ci_lower=lo, ci_upper=hi,
                      n=m.n, reason=reason, **base)


@dataclass
class RunOutcome:
    outcome: str
    gate_results: list[GateResult] = field(default_factory=list)
    reliability_results: dict = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    rankable: bool = False


def evaluate_run(
    registry: GateRegistry,
    measurements: dict[str, Measurement],
    *,
    integrity_ok: bool,
    integrity_failures: list[str] | None = None,
    review_ceiling: str | None = None,
    budget_exhausted: bool = False,
    reliability: dict | None = None,
) -> RunOutcome:
    """
    Resolve a run to a single outcome state.

    Order is fixed by docs/SCORECARD_SPEC.md and stops at the first failure.
    Integrity comes first because a run whose controls failed produced no
    measurement, and its numbers must never be rendered.
    """
    integrity_failures = integrity_failures or []

    # 1. Integrity -- highest precedence, suppresses everything downstream.
    if not integrity_ok:
        return RunOutcome(
            outcome="INVALID_RUN",
            reasons=[f"integrity precondition failed: {c}" for c in integrity_failures],
            rankable=False,
        )

    # 2. Budget.
    if budget_exhausted:
        return RunOutcome(
            outcome="INCOMPLETE",
            reasons=["budget or wall-clock exhausted before required coverage"],
            rankable=False,
        )

    # Gates are evaluated for diagnostics even when the ceiling blocks a PASS.
    results = []
    for key, spec in registry.tracks.items():
        m = measurements.get(key, Measurement(n=0, note="no measurement supplied"))
        results.append(evaluate_gate(key, spec, m, registry.tolerance))

    reasons: list[str] = []
    rel = reliability or {}

    # The review/calibration ceiling is a cap on POSITIVE outcomes only. It must
    # not mask a safety failure or an unevaluable track: those are more
    # informative than "this run couldn't have passed anyway", and hiding them
    # would let a broken candidate look merely unqualified.
    ceiling_blocks_pass = (
        review_ceiling == "NOT_VALID_FOR_PRODUCTION_PASS" or not registry.is_calibrated
    )
    ceiling_reasons = []
    if review_ceiling == "NOT_VALID_FOR_PRODUCTION_PASS":
        ceiling_reasons.append("reviewer configuration cannot support an official PASS "
                               "(see docs/REVIEW_CAPACITY.md)")
    if not registry.is_calibrated:
        ceiling_reasons.append("gate thresholds are UNCALIBRATED; calibrate on DEV and "
                               "freeze before an official qualification run")

    # 3. Safety override -- reported regardless of ceiling.
    for r in results:
        if registry.tracks[r.track].get("overrides_all_other_gates") and r.status == FAIL:
            return RunOutcome(outcome="FAIL", gate_results=results, reliability_results=rel,
                              reasons=[f"safety override: {r.gate_id} -- {r.reason}"],
                              rankable=True)

    # 6. Reliability gates.
    #
    # "measured and below threshold" and "never measured" are different events.
    # A measured failure is a FAIL. An unmeasured gate is UNEVALUABLE, because
    # reporting it as failure asserts a finding the run does not have.
    unmeasured_reliability: list[str] = []
    for gid, spec in registry.reliability.items():
        if gid not in rel or rel[gid] is None:
            unmeasured_reliability.append(gid)
            continue
        val = rel[gid]
        ok = val >= spec["threshold"] if spec["direction"] == "lower" else val <= spec["threshold"]
        if not ok:
            reasons.append(f"{gid}: {val} fails threshold {spec['threshold']}")

    # 7. Track gates.
    mandatory = [r for r in results if r.mandatory]
    if any(r.status == UNEVALUABLE for r in mandatory):
        bad = [r.gate_id for r in mandatory if r.status == UNEVALUABLE]
        return RunOutcome(outcome="UNEVALUABLE", gate_results=results, reliability_results=rel,
                          reasons=[f"mandatory gates unevaluable: {bad}"] + reasons,
                          rankable=False)

    if any(r.status == FAIL for r in mandatory):
        bad = [r.gate_id for r in mandatory if r.status == FAIL]
        return RunOutcome(outcome="FAIL", gate_results=results, reliability_results=rel,
                          reasons=[f"failed mandatory gates: {bad}"] + reasons, rankable=True)

    if reasons:
        return RunOutcome(outcome="FAIL", gate_results=results, reliability_results=rel,
                          reasons=reasons, rankable=True)

    # Every measured gate cleared. The ceiling now applies: a run that could
    # never have been certified reports that, rather than a bare PASS.
    if ceiling_blocks_pass:
        extra = ([f"reliability gates not measurable in this configuration: "
                  f"{unmeasured_reliability}"] if unmeasured_reliability else [])
        return RunOutcome(outcome="NOT_VALID_FOR_PRODUCTION_PASS", gate_results=results,
                          reliability_results=rel, reasons=ceiling_reasons + extra,
                          rankable=False)

    # No ceiling, but reliability was never measured: that is missing evidence,
    # not a passing result.
    if unmeasured_reliability:
        return RunOutcome(outcome="UNEVALUABLE", gate_results=results, reliability_results=rel,
                          reasons=[f"reliability gates not measured: {unmeasured_reliability}"],
                          rankable=False)

    if any(r.status == CONDITIONAL for r in mandatory):
        soft = [r.gate_id for r in mandatory if r.status == CONDITIONAL]
        return RunOutcome(outcome="CONDITIONAL", gate_results=results, reliability_results=rel,
                          reasons=[f"within tolerance, remediation required: {soft}"],
                          rankable=True)

    return RunOutcome(outcome="PASS", gate_results=results, reliability_results=rel, rankable=True)
