"""
Model fitness: capability, current performance, and task fit, kept apart.

    MODEL FITNESS = CAPABILITY × CURRENT PERFORMANCE × TASK FIT × PROVIDER HEALTH

The four factors answer different questions and go stale at different rates:

  * **Capability** -- what is this model able to do at all? Measured on the
    benchmark, changes only when the model or its prompt changes.
  * **Current performance** -- how is it behaving right now? Success rate,
    p95 latency, timeout rate, recent accepted quality. Changes hourly.
  * **Task fit** -- does this task want what this model is good at? A model
    with the best reasoning score is the wrong pick for a latency-bound
    interactive call.
  * **Provider health** -- can the endpoint be reached today?

Collapsing these into one number is the mistake this module exists to avoid.
It produces exactly the failure already measured on this project: `llama-3.1-70b`
caught 10/10 adversarial questions and is on that evidence the better model,
while being unusable interactively on that endpoint. One number would either
discard a good model or ship an unusable one.

WEIGHTS ARE HYPOTHESES, NOT CONSTANTS
-------------------------------------
The component weights below are a starting position, stated as data so they
can be argued with, changed, and versioned. `WEIGHTS_VERSION` travels with
every score, so a scoreboard computed under one weighting is never silently
compared with one computed under another.

Nothing here is calibrated. A weighting has to be validated against outcomes
somebody actually cared about, and Quintek does not have those outcomes yet.
Every score returned carries `calibrated: False` for exactly that reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field

WEIGHTS_VERSION = "v0.1-uncalibrated"

# Starting hypotheses. See the module docstring: these are not measured.
DEFAULT_WEIGHTS: dict[str, float] = {
    "quality": 0.40,
    "task_success": 0.20,
    "latency": 0.15,
    "reliability": 0.10,
    "cost": 0.10,
    "structured_output": 0.05,
}

# How a task type weights the same components. An interactive call and a
# batch job disagree about what "best" means, and a single ranking cannot
# serve both -- which is the whole reason this is a per-profile table.
UTILITY_PROFILES: dict[str, dict[str, float]] = {
    # A learner is waiting. A brilliant answer in 103 seconds is a failure.
    "interactive": {"quality": 0.35, "task_success": 0.15, "latency": 0.30,
                    "reliability": 0.15, "cost": 0.03, "structured_output": 0.02},
    # Nobody is waiting. Throughput and cost dominate; latency barely matters.
    "batch": {"quality": 0.40, "task_success": 0.20, "latency": 0.02,
              "reliability": 0.13, "cost": 0.20, "structured_output": 0.05},
    # Getting it right is nearly the only thing.
    "deep_reasoning": {"quality": 0.55, "task_success": 0.25, "latency": 0.05,
                       "reliability": 0.10, "cost": 0.03, "structured_output": 0.02},
    # The reply has to parse or the pipeline stops, regardless of how good it is.
    "structured": {"quality": 0.30, "task_success": 0.20, "latency": 0.10,
                   "reliability": 0.10, "cost": 0.05, "structured_output": 0.25},
}

# HARD CONSTRAINTS, not weights.
#
# Weighting latency at 30% was not enough. Scored that way, a validator with a
# 103-second p95 still beat a fast one on an INTERACTIVE task, because its
# quality advantage outweighed the latency penalty. That is arithmetically
# reasonable and practically wrong: a learner does not wait 103 seconds and
# then rate the answer, they leave. Beyond some threshold latency stops being
# a dimension of quality and becomes a disqualification.
#
# This mirrors the reasoning already in the benchmark's safety gate -- a
# candidate that fails a mandatory gate is excluded regardless of how high it
# scores elsewhere. Same shape here, so it is applied the same way: before the
# arithmetic, not by hoping the arithmetic notices.
#
# None means the profile has no ceiling: a batch job genuinely does not care.
PROFILE_CONSTRAINTS: dict[str, dict[str, float | None]] = {
    "interactive": {"max_latency_p95_ms": 15_000.0, "min_success_rate": 0.90},
    "batch": {"max_latency_p95_ms": None, "min_success_rate": 0.70},
    "deep_reasoning": {"max_latency_p95_ms": 180_000.0, "min_success_rate": 0.70},
    "structured": {"max_latency_p95_ms": 60_000.0, "min_success_rate": 0.85},
}

# Which profile each Quintek task type wants, absent an explicit override.
TASK_PROFILES: dict[str, str] = {
    "QUESTION_GENERATION": "batch",
    "QUESTION_VALIDATION": "batch",
    "CONCEPT_EXTRACTION": "batch",
    "CONCEPT_RESOLUTION": "deep_reasoning",
    "RELATIONSHIP_EXTRACTION": "structured",
    "SOURCE_PROCESSING": "batch",
    "KNOWLEDGE_GAP_EXTRACTION": "deep_reasoning",
    "REVISION_SELECTION": "interactive",
    "EXPLANATION": "interactive",
}

# Below this many comparable observations, a performance figure is reported
# but must not drive routing. See `ModelFitness.evidence_sufficient`.
MIN_OBSERVATIONS = 30


def profile_for(task_type: str, override: str | None = None) -> str:
    if override:
        if override not in UTILITY_PROFILES:
            raise KeyError(f"unknown utility profile {override!r}; "
                           f"expected one of {', '.join(sorted(UTILITY_PROFILES))}")
        return override
    return TASK_PROFILES.get(task_type, "batch")


def _normalise_latency(p95_ms: float | None, *, target_ms: float = 3000.0) -> float | None:
    """
    Latency onto a 0-1 higher-is-better scale.

    At or under target scores 1.0 and decays hyperbolically after, so 3s -> 1.0,
    6s -> 0.5, 30s -> 0.1, 103s -> 0.03. Chosen so the difference between a
    fast and a very slow endpoint stays visible rather than saturating; a
    linear scale would put 30s and 103s in almost the same place.
    """
    if p95_ms is None:
        return None
    if p95_ms <= target_ms:
        return 1.0
    return target_ms / p95_ms


def _normalise_cost(cost_per_1k: float | None, *, reference: float = 1.0) -> float | None:
    """Cheaper is better. Unpriced returns None, never 0 -- see `blend`."""
    if cost_per_1k is None:
        return None
    if cost_per_1k <= 0:
        return 1.0
    return min(1.0, reference / cost_per_1k)


@dataclass
class CapabilityScore:
    """What the model can do, from the benchmark. Slow-moving."""

    key: str
    scores: dict[str, float] = field(default_factory=dict)   # e.g. {"reasoning": 0.93}
    source_run_id: str = ""
    measured_at: str = ""

    def get(self, name: str) -> float | None:
        return self.scores.get(name)

    @property
    def overall(self) -> float | None:
        measured = [v for v in self.scores.values() if v is not None]
        return sum(measured) / len(measured) if measured else None

    def as_dict(self) -> dict:
        return {"key": self.key, "scores": dict(self.scores), "overall": self.overall,
                "source_run_id": self.source_run_id, "measured_at": self.measured_at}


@dataclass
class PerformanceScore:
    """How it is behaving right now. Fast-moving, computed from the ledger."""

    key: str
    n: int = 0
    success_rate: float | None = None
    timeout_rate: float | None = None
    latency_p50_ms: float | None = None
    latency_p95_ms: float | None = None
    accepted_rate: float | None = None
    mean_quality: float | None = None
    structured_ok_rate: float | None = None
    cost_per_1k: float | None = None

    @property
    def evidence_sufficient(self) -> bool:
        return self.n >= MIN_OBSERVATIONS

    def as_dict(self) -> dict:
        return {"key": self.key, "n": self.n, "success_rate": self.success_rate,
                "timeout_rate": self.timeout_rate, "latency_p50_ms": self.latency_p50_ms,
                "latency_p95_ms": self.latency_p95_ms, "accepted_rate": self.accepted_rate,
                "mean_quality": self.mean_quality,
                "structured_ok_rate": self.structured_ok_rate,
                "cost_per_1k": self.cost_per_1k,
                "evidence_sufficient": self.evidence_sufficient,
                "min_observations": MIN_OBSERVATIONS}


def blend(components: dict[str, float | None], weights: dict[str, float]) -> tuple[float | None, dict]:
    """
    Weighted mean over the components that were actually measured.

    Unmeasured components are DROPPED and the remaining weights renormalised,
    rather than treated as zero. Treating "we have no cost data" as "cost is
    infinitely bad" is how an unpriced model ends up last in a ranking for a
    reason that has nothing to do with the model. The returned detail records
    which components were dropped, so a score is never quoted without knowing
    what it was computed from.
    """
    used = {k: v for k, v in components.items() if v is not None and weights.get(k)}
    dropped = sorted(k for k in weights if k not in used)
    total_weight = sum(weights[k] for k in used)
    if not used or total_weight <= 0:
        return None, {"used": {}, "dropped": dropped, "weight_covered": 0.0}
    score = sum(weights[k] * v for k, v in used.items()) / total_weight
    return score, {
        "used": {k: round(v, 4) for k, v in used.items()},
        "dropped": dropped,
        # What fraction of the intended weighting was actually available. A
        # score computed from 45% of the weights is a much weaker claim.
        "weight_covered": round(total_weight / sum(weights.values()), 3),
    }


@dataclass
class ModelFitness:
    key: str
    task_type: str
    profile: str
    fitness: float | None
    capability: CapabilityScore | None
    performance: PerformanceScore
    health: dict
    detail: dict
    eligible: bool
    reasons: list[str] = field(default_factory=list)

    @property
    def evidence_sufficient(self) -> bool:
        return self.performance.evidence_sufficient

    def as_dict(self) -> dict:
        return {
            "key": self.key, "task_type": self.task_type, "profile": self.profile,
            "fitness": self.fitness, "eligible": self.eligible, "reasons": list(self.reasons),
            "capability": self.capability.as_dict() if self.capability else None,
            "performance": self.performance.as_dict(),
            "health": self.health, "detail": self.detail,
            "weights_version": WEIGHTS_VERSION,
            # Said on every score, so it cannot be quoted without the caveat.
            "calibrated": False,
        }


def score_fitness(key: str, *, task_type: str, performance: PerformanceScore,
                  capability: CapabilityScore | None = None, health: dict | None = None,
                  profile: str | None = None,
                  weights: dict[str, float] | None = None) -> ModelFitness:
    """
    Combine the four factors for one candidate on one task type.

    Eligibility is decided BEFORE the arithmetic, not by the score coming out
    low: an unreachable endpoint is not a 0.2, it is unusable, and a ranking
    that puts it at the bottom implies it could be picked if everything else
    were worse.
    """
    health = health or {}
    chosen = profile or profile_for(task_type)
    profile_weights = weights or UTILITY_PROFILES.get(chosen, DEFAULT_WEIGHTS)

    reasons, eligible = [], True
    if health.get("usable_now") is False:
        eligible = False
        reasons.append(health.get("circuit", {}).get("last_error")
                       or "the circuit for this candidate is open")
    if health.get("declared_state") == "UNAVAILABLE":
        eligible = False
        reasons.append("declared UNAVAILABLE by an operator")

    # Hard constraints, applied before the arithmetic. A candidate that
    # breaches one is excluded, not merely marked down -- see the comment on
    # PROFILE_CONSTRAINTS for why weighting was not sufficient.
    constraints = PROFILE_CONSTRAINTS.get(chosen, {})
    ceiling = constraints.get("max_latency_p95_ms")
    if (ceiling is not None and performance.latency_p95_ms is not None
            and performance.latency_p95_ms > ceiling):
        eligible = False
        reasons.append(
            f"p95 latency {performance.latency_p95_ms / 1000:.1f}s exceeds the "
            f"{ceiling / 1000:.0f}s ceiling for a {chosen} task; on this endpoint this "
            "candidate is not usable for this kind of work however well it scores")
    floor = constraints.get("min_success_rate")
    if (floor is not None and performance.success_rate is not None
            and performance.success_rate < floor):
        eligible = False
        reasons.append(
            f"success rate {performance.success_rate:.0%} is below the {floor:.0%} floor "
            f"for a {chosen} task")

    components = {
        "quality": performance.mean_quality if performance.mean_quality is not None
                   else (capability.overall if capability else None),
        "task_success": performance.accepted_rate,
        "latency": _normalise_latency(performance.latency_p95_ms),
        "reliability": performance.success_rate,
        "cost": _normalise_cost(performance.cost_per_1k),
        "structured_output": performance.structured_ok_rate,
    }
    fitness, detail = blend(components, profile_weights)

    if not performance.evidence_sufficient:
        reasons.append(
            f"only {performance.n} observation(s); {MIN_OBSERVATIONS} are required before "
            "this figure may drive routing")
    if detail.get("weight_covered", 0) < 0.5 and fitness is not None:
        reasons.append(
            f"computed from {detail['weight_covered']:.0%} of the intended weighting "
            f"(missing: {', '.join(detail['dropped'])})")

    return ModelFitness(key=key, task_type=task_type, profile=chosen, fitness=fitness,
                        capability=capability, performance=performance, health=health,
                        detail=detail, eligible=eligible, reasons=reasons)
