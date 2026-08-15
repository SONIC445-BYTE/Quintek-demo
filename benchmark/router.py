"""
Deterministic Model Router.

Task -> registry -> benchmark evidence -> policy -> candidate. No step in
this module calls a model to decide which model to call -- selection is
arithmetic over already-computed benchmark evidence (benchmark/analytics.py)
and already-enforced eligibility (benchmark/registry.py), which is what
makes a routing decision auditable: "why did Quintek use this model" always
resolves to a stored number and a stored rule, never to another model's
opinion.

Safety overrides performance structurally, not by convention: a candidate
whose latest run is not `production_eligible` (see analytics.py -- exactly
`outcome in {PASS, CONDITIONAL}` and `rankable=True`) is filtered out before
any scoring happens, so a high raw score can never compensate for a failed
mandatory gate. This is enforced in `_eligible_for_task`, not left to the
caller to remember to check.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import Enum

from . import analytics as an
from .registry import Registry, ModelCandidate
from .tasks import TaskType, gate_ids_for, required_capabilities_for


class RoutingPolicy(str, Enum):
    QUALITY_FIRST = "QUALITY_FIRST"
    COST_OPTIMIZED = "COST_OPTIMIZED"
    LATENCY_OPTIMIZED = "LATENCY_OPTIMIZED"
    BALANCED = "BALANCED"
    EXPERIMENTAL = "EXPERIMENTAL"


@dataclass
class RouterResult:
    task: TaskType
    policy: RoutingPolicy
    selected_candidate: str | None
    eligible_candidates: list[str]
    scores: dict[str, float | None]
    reason: str

    def as_dict(self) -> dict:
        return dict(
            task=self.task.value, policy=self.policy.value,
            selected_candidate=self.selected_candidate,
            eligible_candidates=self.eligible_candidates,
            scores=self.scores, reason=self.reason,
        )


class Router:
    def __init__(self, registry: Registry, archive: an.RunArchive):
        self.registry = registry
        self.archive = archive

    def _eligible_for_task(
        self, task: TaskType, extra_capabilities: tuple[str, ...], exclude: set[str],
    ) -> list[ModelCandidate]:
        needed = set(required_capabilities_for(task)) | set(extra_capabilities)
        pool = [c for c in self.registry.eligible_candidates() if c.candidate_id not in exclude]
        if needed:
            pool = [c for c in pool if needed <= set(c.capabilities)]

        result = []
        for c in pool:
            run = self.archive.latest_run_for_candidate(c.candidate_id)
            if run is None or not run.production_eligible:
                continue  # safety overrides performance -- no exceptions here
            gate_ids = gate_ids_for(task)
            relevant = [t for t in run.tracks if t.gate_id in gate_ids]
            if not relevant:
                continue  # no evidence for this task at all
            if any(t.student_status == "fail" for t in relevant):
                continue  # this candidate specifically fails a gate this task needs
            result.append(c)
        return result

    def _task_scores(self, task: TaskType, candidates: list[ModelCandidate]) -> dict[str, float | None]:
        gate_ids = set(gate_ids_for(task))
        scores: dict[str, float | None] = {}
        for c in candidates:
            run = self.archive.latest_run_for_candidate(c.candidate_id)
            relevant = [t for t in run.tracks if t.gate_id in gate_ids]
            normalized = [an.normalized_track_score(t) for t in relevant]
            normalized = [n for n in normalized if n is not None]
            scores[c.candidate_id] = (sum(normalized) / len(normalized)) if normalized else None
        return scores

    def select(
        self,
        task: TaskType,
        *,
        policy: RoutingPolicy = RoutingPolicy.QUALITY_FIRST,
        required_capabilities: tuple[str, ...] = (),
        cost_hint: dict[str, float] | None = None,
        latency_hint: dict[str, float] | None = None,
        exclude: set[str] | None = None,
        seed: int | None = None,
    ) -> RouterResult:
        exclude = exclude or set()
        eligible = self._eligible_for_task(task, required_capabilities, exclude)

        if not eligible:
            return RouterResult(
                task=task, policy=policy, selected_candidate=None,
                eligible_candidates=[], scores={},
                reason="no candidate is capability-matched, benchmark-eligible, "
                      "and free of a failing gate for this task",
            )

        scores = self._task_scores(task, eligible)
        eligible_ids = [c.candidate_id for c in eligible]

        selected, reason = self._apply_policy(
            policy, eligible_ids, scores, cost_hint, latency_hint, seed,
        )
        return RouterResult(
            task=task, policy=policy, selected_candidate=selected,
            eligible_candidates=eligible_ids, scores=scores, reason=reason,
        )

    def _apply_policy(self, policy, eligible_ids, scores, cost_hint, latency_hint, seed):
        scored = [(cid, scores.get(cid)) for cid in eligible_ids]
        scored_with_evidence = [(cid, s) for cid, s in scored if s is not None]

        def best_by_score(pool):
            if not pool:
                return None
            # Deterministic tie-break: highest score, then candidate_id for
            # a stable, reproducible order rather than dict/insertion order.
            return sorted(pool, key=lambda cs: (-cs[1], cs[0]))[0][0]

        if policy == RoutingPolicy.QUALITY_FIRST:
            pick = best_by_score(scored_with_evidence) or sorted(eligible_ids)[0]
            return pick, "highest task-relevant benchmark score among eligible candidates"

        if policy == RoutingPolicy.COST_OPTIMIZED:
            if not cost_hint:
                pick = best_by_score(scored_with_evidence) or sorted(eligible_ids)[0]
                return pick, "no cost_hint supplied; fell back to QUALITY_FIRST"
            priced = [(cid, cost_hint[cid]) for cid in eligible_ids if cid in cost_hint]
            if not priced:
                pick = best_by_score(scored_with_evidence) or sorted(eligible_ids)[0]
                return pick, "cost_hint had no entries for any eligible candidate; fell back to QUALITY_FIRST"
            pick = sorted(priced, key=lambda cp: (cp[1], cp[0]))[0][0]
            return pick, "lowest cost among candidates that already cleared the benchmark"

        if policy == RoutingPolicy.LATENCY_OPTIMIZED:
            if not latency_hint:
                pick = best_by_score(scored_with_evidence) or sorted(eligible_ids)[0]
                return pick, "no latency_hint supplied; fell back to QUALITY_FIRST"
            timed = [(cid, latency_hint[cid]) for cid in eligible_ids if cid in latency_hint]
            if not timed:
                pick = best_by_score(scored_with_evidence) or sorted(eligible_ids)[0]
                return pick, "latency_hint had no entries for any eligible candidate; fell back to QUALITY_FIRST"
            pick = sorted(timed, key=lambda ct: (ct[1], ct[0]))[0][0]
            return pick, "lowest latency among candidates that already cleared the benchmark"

        if policy == RoutingPolicy.BALANCED:
            if not scored_with_evidence:
                return sorted(eligible_ids)[0], "no benchmark evidence at all; arbitrary deterministic pick"
            worst, best = min(s for _, s in scored_with_evidence), max(s for _, s in scored_with_evidence)
            span = (best - worst) or 1.0

            def combined(cid, s):
                quality_component = (s - worst) / span
                cost_component = 0.0
                if cost_hint and cid in cost_hint:
                    costs = [cost_hint[c] for c in eligible_ids if c in cost_hint]
                    c_worst, c_best = max(costs), min(costs)
                    c_span = (c_worst - c_best) or 1.0
                    cost_component = (c_worst - cost_hint[cid]) / c_span
                weight_quality = 0.6 if (cost_hint or latency_hint) else 1.0
                weight_other = 1.0 - weight_quality
                return weight_quality * quality_component + weight_other * cost_component

            pick = sorted(scored_with_evidence, key=lambda cs: (-combined(*cs), cs[0]))[0][0]
            return pick, "balanced score/cost composite among eligible candidates"

        if policy == RoutingPolicy.EXPERIMENTAL:
            rng = random.Random(seed)
            pick = rng.choice(sorted(eligible_ids))
            return pick, "EXPERIMENTAL policy: randomly sampled to collect new production evidence"

        raise ValueError(f"unknown routing policy {policy!r}")
