"""
The Quintek Router: which model serves this call, and why.

`benchmark/router.py` selects on benchmark evidence alone. That is the right
answer for a qualification run and the wrong one for a live product, because
it cannot see that an endpoint is currently down, that a candidate has four
observations rather than four hundred, or that a task is interactive and the
best-scoring candidate takes 103 seconds.

This router is the live one. It runs a pipeline of filters, each of which can
only ever REMOVE candidates, and then chooses among what is left:

    task
      |
      v
    capability filter      does it claim to do this at all?
      |
      v
    health filter          is the endpoint reachable right now?
      |
      v
    fitness scoring        capability x performance x task fit
      |
      v
    exploration policy     exploit the leader, or spend a call learning?
      |
      v
    model + a written reason

TWO MODES, DELIBERATELY DIFFERENT
---------------------------------
**Production** wants the best answer now: it exploits, and explores only at a
small controlled rate.

**Evaluation** wants to learn: it ignores the ranking and routes by who has
the least evidence, filling the quota matrix in `benchmark/evaluation.py`.

Sharing one policy between them is what produces "Model A got 80 questions,
Model C got 2, therefore A wins". They are separated here so that never
happens by accident.

EVERY DECISION IS EXPLAINED
---------------------------
`RoutingDecision.reason` is a sentence a human can read, and `considered`
lists every candidate that was looked at with why it was dropped. A router
that cannot say why it picked something cannot be debugged, and this one gets
its inputs from a ledger that changes hourly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .evaluation import ExplorationPolicy
from .fitness import (MIN_OBSERVATIONS, ModelFitness, PerformanceScore,
                      profile_for, score_fitness)

PRODUCTION, EVALUATION = "production", "evaluation"


class NoRoutableCandidate(RuntimeError):
    """Nothing can serve this task, with the reason each candidate was dropped."""


@dataclass
class Candidate:
    """
    A routable option. `capabilities` is what it CLAIMS; the benchmark is
    what it has SHOWN. Both are kept, because a claim is what lets a model be
    considered for a task it has never been measured on.
    """

    key: str
    provider: str
    model: str
    capabilities: set[str] = field(default_factory=set)
    model_family: str = ""
    declared_status: str = ""

    def as_dict(self) -> dict:
        return {"key": self.key, "provider": self.provider, "model": self.model,
                "capabilities": sorted(self.capabilities),
                "model_family": self.model_family,
                "declared_status": self.declared_status}


@dataclass
class RoutingDecision:
    task_type: str
    mode: str
    selected: str | None
    reason: str
    profile: str
    considered: list[dict] = field(default_factory=list)
    fallback_from: str | None = None
    evidence_sufficient: bool = False
    # What survived each layer, so "could I use it" and "should I use it" stay
    # readable apart in the record as well as in the code.
    layer1_eligible: list[str] = field(default_factory=list)
    layer2_ranked: list[str] = field(default_factory=list)

    @property
    def environmental_exclusions(self) -> list[dict]:
        """Candidates dropped for reasons that are not about the model."""
        return [c for c in self.considered if c.get("environmental")]

    def as_dict(self) -> dict:
        return {"task_type": self.task_type, "mode": self.mode, "selected": self.selected,
                "reason": self.reason, "profile": self.profile,
                "considered": list(self.considered), "fallback_from": self.fallback_from,
                "evidence_sufficient": self.evidence_sufficient,
                "layer1_eligible": list(self.layer1_eligible),
                "layer2_ranked": list(self.layer2_ranked),
                "environmental_exclusions": [c["key"] for c in self.environmental_exclusions]}


class QuintekRouter:
    """
    `performance_for(key, task_type) -> PerformanceScore` and
    `health_for(key) -> dict` are injected so this class has no opinion about
    where evidence is stored -- in practice they come from
    `benchmark/inference_log.py` and `benchmark/health.py`.
    """

    def __init__(self, candidates: list[Candidate], *, performance_for=None,
                 health_for=None, capability_for=None,
                 exploration: ExplorationPolicy | None = None,
                 required_capabilities=None, provider_registry=None):
        # LAYER 1's source of truth, when one is supplied: reachability,
        # credentials, model existence and declared capability all come from
        # the provider registry rather than being inferred from call history.
        # A provider blocked by egress policy has no call history to infer
        # from, which is precisely when guessing goes wrong.
        self.provider_registry = provider_registry
        self.candidates = {c.key: c for c in candidates}
        self.performance_for = performance_for or (lambda key, task: PerformanceScore(key))
        self.health_for = health_for or (lambda key: {"usable_now": True})
        self.capability_for = capability_for or (lambda key: None)
        self.exploration = exploration or ExplorationPolicy()
        self.required_capabilities = required_capabilities or {}

    # ---------- the filters ----------

    def _capability_filter(self, task_type: str) -> tuple[list[Candidate], list[dict]]:
        """
        LAYER 1 -- "can I use this at all?"

        Never consults quality or speed. A provider that merely works must not
        become the preferred provider by passing this filter; that decision
        belongs to layer 2, and keeping them apart is what stops "it is the
        only one reachable" turning into "it is the best".
        """
        required = set(self.required_capabilities.get(task_type, ()))
        kept, dropped = [], []

        registry_status = {}
        if self.provider_registry is not None:
            eligible, registry_dropped = self.provider_registry.eligible(
                required_capabilities=required)
            allowed = {m.key for m in eligible}
            for entry in registry_dropped:
                registry_status[entry["key"]] = entry
        else:
            allowed = None

        for candidate in self.candidates.values():
            if allowed is not None and candidate.key not in allowed:
                entry = registry_status.get(candidate.key, {})
                dropped.append({
                    "key": candidate.key, "dropped_at": "layer1_provider", "layer": 1,
                    "reason": entry.get("reason", "not eligible in the provider registry"),
                    "status": entry.get("status", ""),
                    # The flag that keeps an environmental block out of every
                    # downstream quality judgement.
                    "environmental": entry.get("environmental", False)})
                continue
            missing = required - candidate.capabilities
            if missing:
                dropped.append({"key": candidate.key, "dropped_at": "capability", "layer": 1,
                                "reason": f"does not claim {', '.join(sorted(missing))}"})
            else:
                kept.append(candidate)
        return kept, dropped

    def _health_filter(self, candidates: list[Candidate]) -> tuple[list[Candidate], list[dict]]:
        kept, dropped = [], []
        for candidate in candidates:
            health = self.health_for(candidate.key)
            if health.get("usable_now") is False:
                dropped.append({
                    "key": candidate.key, "dropped_at": "health", "layer": 1,
                    "reason": health.get("circuit", {}).get("last_error")
                              or "circuit open",
                    "status": health.get("last_status", ""),
                    "environmental": bool(health.get("environmental_failures"))})
            else:
                kept.append(candidate)
        return kept, dropped

    def _score(self, candidates: list[Candidate], task_type: str,
               profile: str | None) -> list[ModelFitness]:
        scored = []
        for candidate in candidates:
            scored.append(score_fitness(
                candidate.key, task_type=task_type,
                performance=self.performance_for(candidate.key, task_type),
                capability=self.capability_for(candidate.key),
                health=self.health_for(candidate.key), profile=profile))
        return scored

    # ---------- routing ----------

    def route(self, task_type: str, *, mode: str = PRODUCTION, roll: float = 0.5,
              profile: str | None = None, exclude: set[str] | None = None) -> RoutingDecision:
        exclude = exclude or set()
        chosen_profile = profile_for(task_type, profile)

        candidates, considered = self._capability_filter(task_type)
        candidates = [c for c in candidates if c.key not in exclude]
        for key in sorted(exclude):
            if key in self.candidates:
                considered.append({"key": key, "dropped_at": "excluded",
                                   "reason": "excluded by the caller"})

        candidates, health_dropped = self._health_filter(candidates)
        considered.extend(health_dropped)

        if not candidates:
            raise NoRoutableCandidate(
                f"no candidate can serve {task_type}: " +
                "; ".join(f"{d['key']} ({d['reason']})" for d in considered)
                or "no candidates are registered")

        scored = self._score(candidates, task_type, profile)
        eligible = [s for s in scored if s.eligible]
        for s in scored:
            if not s.eligible:
                considered.append({"key": s.key, "dropped_at": "fitness", "layer": 2,
                                   "reason": "; ".join(s.reasons) or "not eligible"})

        if not eligible:
            raise NoRoutableCandidate(
                f"no candidate is eligible for {task_type} under the {chosen_profile} "
                "profile: " + "; ".join(f"{d['key']} ({d['reason']})" for d in considered))

        # Unscored candidates sort last but are NOT dropped: a candidate with
        # no evidence is exactly what exploration exists to fix, and dropping
        # it would make that impossible.
        eligible.sort(key=lambda s: (s.fitness is None, -(s.fitness or 0.0), s.key))
        ranked = [s.key for s in eligible]
        observations = {s.key: s.performance.n for s in eligible}
        by_key = {s.key: s for s in eligible}

        if mode == EVALUATION:
            # Ignore the ranking entirely: route to whoever knows least.
            selected = min(ranked, key=lambda k: (observations.get(k, 0), k))
            reason = (f"evaluation mode: {selected} has {observations.get(selected, 0)} "
                      f"observation(s) on {task_type}, the fewest of any eligible candidate")
        else:
            selected, reason = self.exploration.decide(
                ranked=ranked, observations=observations, roll=roll)

        for s in eligible:
            considered.append({
                "key": s.key, "dropped_at": None if s.key == selected else "not_selected",
                "fitness": s.fitness, "observations": s.performance.n,
                "reason": "selected" if s.key == selected else "ranked lower"})

        return RoutingDecision(
            task_type=task_type, mode=mode, selected=selected, reason=reason,
            profile=chosen_profile, considered=considered,
            layer1_eligible=[c.key for c in candidates],
            layer2_ranked=ranked,
            evidence_sufficient=by_key[selected].performance.evidence_sufficient)

    def route_with_fallback(self, task_type: str, *, mode: str = PRODUCTION,
                            roll: float = 0.5, failed: set[str] | None = None
                            ) -> RoutingDecision:
        """
        Route, excluding candidates that already failed this task.

        The fallback is recorded on the decision rather than silently
        substituted: an answer produced by the second choice after the first
        timed out is a different provenance from one produced by the first,
        and a report that cannot tell them apart cannot explain a latency
        spike or a quality dip.
        """
        failed = failed or set()
        decision = self.route(task_type, mode=mode, roll=roll, exclude=failed)
        if failed:
            decision.fallback_from = sorted(failed)[0]
            decision.reason += (f" (fallback: {', '.join(sorted(failed))} already failed "
                                "this task)")
        return decision

    # ---------- reporting ----------

    def scoreboard(self, task_type: str, *, profile: str | None = None) -> dict:
        """
        Every candidate for one task type, ranked, with what is missing.

        The `evidence` block is the guard against the bias this whole design
        is aimed at: it says outright when a ranking rests on too little data
        to mean anything.
        """
        scored = self._score(list(self.candidates.values()), task_type, profile)
        scored.sort(key=lambda s: (not s.eligible, s.fitness is None,
                                   -(s.fitness or 0.0), s.key))
        under = [s.key for s in scored if not s.performance.evidence_sufficient]
        return {
            "task_type": task_type,
            "profile": profile_for(task_type, profile),
            "ranking": [s.as_dict() for s in scored],
            "evidence": {
                "min_observations": MIN_OBSERVATIONS,
                "under_measured": under,
                "trustworthy": not under,
                "note": ("" if not under else
                         f"{len(under)} candidate(s) have fewer than {MIN_OBSERVATIONS} "
                         f"observations on {task_type} ({', '.join(under)}). This ranking "
                         "is provisional and must not be used to retire a candidate."),
            },
        }
