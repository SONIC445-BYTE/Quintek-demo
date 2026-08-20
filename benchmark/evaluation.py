"""
Evaluation mode: giving every candidate a fair, controlled shot.

Production routing and model evaluation want opposite things. Production wants
the best known answer now; evaluation wants to learn which candidate is best,
which requires deliberately spending calls on candidates that are probably not
the best. Running both through one policy produces the bias this module exists
to prevent:

    Model A -> 80 questions
    Model B -> 5 questions
    Model C -> 2 questions

after which A "wins" a comparison that never happened.

THREE RULES
-----------

**1. Rotation, not blocks.** Handing the first 20 tasks to A and the next 20
to B confounds the model with the difficulty of the tasks it happened to get.
`RotationPlan` assigns each TASK to several candidates, and rotates which
candidates, so every candidate meets a comparable spread of work and the same
item is answered by more than one of them.

**2. A quota matrix, not a quota.** "20 tasks each" hides the finding that
actually matters. Quota is per (candidate, task_type), so the result is not
"C is the best model" but "C is the best at conceptual reasoning and B is
better at clinical vignettes" -- which is a far more useful thing to know and
is invisible under a single total.

**3. Paired coverage is reported, not assumed.** Two candidates can each have
30 observations and share almost no items, in which case comparing their means
compares two different exams. `coverage()` reports how many items any two
candidates actually have in common.

WHAT THIS MODULE DOES NOT DO
----------------------------
It does not call models and it does not score them. It decides who should
answer what, and reports how much comparable evidence exists. Execution is the
caller's; scoring is `benchmark/fitness.py`'s.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field

# Below this, a per-(candidate, task_type) cell is not comparable evidence.
DEFAULT_QUOTA = 20

# How many candidates answer each task. More than one is the point: it is what
# makes the comparison paired rather than parallel.
DEFAULT_REPLICAS = 3


class EvaluationError(ValueError):
    pass


@dataclass
class Assignment:
    task_id: str
    task_type: str
    candidates: list[str]

    def as_dict(self) -> dict:
        return {"task_id": self.task_id, "task_type": self.task_type,
                "candidates": list(self.candidates)}


@dataclass
class RotationPlan:
    """
    A deterministic assignment of tasks to candidates.

    Deterministic on purpose: the same plan can be regenerated to resume an
    interrupted evaluation, and two people inspecting "who was supposed to
    answer task 17" get the same answer. The rotation offset is derived from
    the task id rather than from a counter, so adding a task in the middle
    does not reshuffle everything after it.
    """

    assignments: list[Assignment] = field(default_factory=list)

    def for_candidate(self, candidate: str) -> list[Assignment]:
        return [a for a in self.assignments if candidate in a.candidates]

    def quota_matrix(self) -> dict[str, dict[str, int]]:
        matrix: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for assignment in self.assignments:
            for candidate in assignment.candidates:
                matrix[candidate][assignment.task_type] += 1
        return {c: dict(t) for c, t in matrix.items()}

    def as_dict(self) -> dict:
        return {"assignments": [a.as_dict() for a in self.assignments],
                "quota_matrix": self.quota_matrix()}


def _offset(task_id: str, modulus: int) -> int:
    """Stable rotation offset from the task id, not from its position."""
    if modulus <= 0:
        return 0
    digest = hashlib.sha256(task_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % modulus


def build_rotation(tasks: list[tuple[str, str]], candidates: list[str], *,
                   replicas: int = DEFAULT_REPLICAS) -> RotationPlan:
    """
    `tasks` is [(task_id, task_type)]. Returns who answers what.

    Each task goes to `replicas` candidates, chosen by rotating through the
    candidate list from a per-task offset. Over many tasks every candidate
    receives a comparable share of every task type, without any candidate
    getting a contiguous block of them.
    """
    if not candidates:
        raise EvaluationError("an evaluation needs at least one candidate")
    if replicas < 1:
        raise EvaluationError("replicas must be at least 1")
    take = min(replicas, len(candidates))
    if take < 2 and len(candidates) > 1:
        # Not fatal, but worth refusing silently: replicas=1 across several
        # candidates gives an unpaired comparison, which is the confound this
        # module exists to avoid.
        raise EvaluationError(
            "replicas=1 with several candidates produces an unpaired comparison: no two "
            "candidates would answer the same task, so their scores are not comparable")

    ordered = sorted(candidates)
    assignments = []
    for task_id, task_type in tasks:
        start = _offset(task_id, len(ordered))
        chosen = [ordered[(start + i) % len(ordered)] for i in range(take)]
        assignments.append(Assignment(task_id, task_type, chosen))
    return RotationPlan(assignments)


@dataclass
class QuotaState:
    """
    How much comparable evidence each (candidate, task_type) cell has, and
    what is still owed.
    """

    quota: int
    observed: dict[str, dict[str, int]]

    def remaining(self, candidate: str, task_type: str) -> int:
        return max(0, self.quota - self.observed.get(candidate, {}).get(task_type, 0))

    def underfilled(self) -> list[tuple[str, str, int]]:
        """Cells still short of quota, emptiest first."""
        out = []
        for candidate, by_type in self.observed.items():
            for task_type, count in by_type.items():
                if count < self.quota:
                    out.append((candidate, task_type, self.quota - count))
        out.sort(key=lambda row: (-row[2], row[0], row[1]))
        return out

    def complete(self) -> bool:
        return not self.underfilled()


def quota_state(coverage_matrix: dict[str, dict[str, int]], *,
                candidates: list[str], task_types: list[str],
                quota: int = DEFAULT_QUOTA) -> QuotaState:
    """
    Fill in every (candidate, task_type) cell, including the empty ones.

    Cells with zero observations are the whole point: a matrix that only lists
    what has been run cannot show what has not.
    """
    observed = {c: {t: coverage_matrix.get(c, {}).get(t, 0) for t in task_types}
                for c in candidates}
    return QuotaState(quota=quota, observed=observed)


def next_assignments(state: QuotaState, *, limit: int = 10) -> list[tuple[str, str]]:
    """
    Which (candidate, task_type) pairs most need work.

    Emptiest cell first, so evaluation naturally spends its budget on what is
    least known rather than topping up what is already well measured.
    """
    return [(candidate, task_type) for candidate, task_type, _ in
            state.underfilled()[:limit]]


def paired_coverage(observations: list[tuple[str, str]]) -> dict:
    """
    `observations` is [(candidate, task_id)]. Reports how much of any two
    candidates' evidence is actually shared.

    Two candidates with 30 observations each and 2 items in common are not
    comparable, and their means will look comparable unless somebody checks.
    """
    by_candidate: dict[str, set] = defaultdict(set)
    for candidate, task_id in observations:
        by_candidate[candidate].add(task_id)

    candidates = sorted(by_candidate)
    pairs = {}
    for i, a in enumerate(candidates):
        for b in candidates[i + 1:]:
            shared = by_candidate[a] & by_candidate[b]
            pairs[f"{a} vs {b}"] = {
                "shared_items": len(shared),
                "a_only": len(by_candidate[a] - by_candidate[b]),
                "b_only": len(by_candidate[b] - by_candidate[a]),
                "comparable": len(shared) >= 10,
            }
    # No pairs at all is a different state from a pair with no overlap, and
    # `min(..., default=0)` collapses them into the same misleading sentence.
    weakest = min((p["shared_items"] for p in pairs.values()), default=None)
    if not pairs:
        note = ("Only one candidate has observations, so nothing is being compared. "
                "This run measures that candidate; it does not rank anything."
                if by_candidate else "No observations were recorded.")
    elif weakest is not None and weakest < 10:
        note = (f"The least-overlapping pair shares only {weakest} item(s). Comparing those "
                "two candidates' means compares two different sets of questions, not two "
                "models.")
    else:
        note = ""
    return {
        "candidates": {c: len(items) for c, items in by_candidate.items()},
        "pairs": pairs,
        "weakest_pair_overlap": weakest,
        "comparable": bool(pairs) and weakest is not None and weakest >= 10,
        "note": note,
    }


# ---------------------------------------------------------------------------
# Exploration vs exploitation
# ---------------------------------------------------------------------------

@dataclass
class ExplorationPolicy:
    """
    What fraction of production traffic is deliberately spent on learning.

    The default sends most work to the best known candidate and a slice to
    everything else, so a new or under-measured candidate accumulates evidence
    without a separate evaluation campaign. A candidate that then turns out to
    be better is promoted by the evidence rather than by somebody noticing.

    `explore_rate` is the floor, not the ceiling: while any candidate is below
    MIN_OBSERVATIONS, exploration is forced regardless of the rate, because
    "exploit the best" is meaningless when "best" rests on four calls.
    """

    explore_rate: float = 0.10
    min_observations: int = 30

    def decide(self, *, ranked: list[str], observations: dict[str, int],
               roll: float) -> tuple[str, str]:
        """
        Return `(candidate, reason)`. `roll` is a caller-supplied float in
        [0, 1) -- injected rather than drawn here so routing is reproducible
        and testable, which a hidden RNG would prevent.
        """
        if not ranked:
            raise EvaluationError("no candidates to choose between")

        starving = [c for c in ranked
                    if observations.get(c, 0) < self.min_observations]
        if starving:
            # Forced exploration. Pick the least-observed, ties broken by rank.
            chosen = min(starving, key=lambda c: (observations.get(c, 0), ranked.index(c)))
            return chosen, (
                f"forced exploration: {chosen} has {observations.get(chosen, 0)} of the "
                f"{self.min_observations} observations needed before any ranking that "
                "includes it means anything")

        if roll < self.explore_rate and len(ranked) > 1:
            # Deliberate exploration among the non-leaders.
            challengers = ranked[1:]
            index = int(roll / max(self.explore_rate, 1e-9) * len(challengers))
            chosen = challengers[min(index, len(challengers) - 1)]
            return chosen, (
                f"exploration ({self.explore_rate:.0%} of traffic): giving {chosen} a "
                "controlled opportunity to challenge the current leader")

        return ranked[0], "exploitation: current best eligible candidate for this task"

# ---------------------------------------------------------------------------
# The evaluation scheduler
# ---------------------------------------------------------------------------

@dataclass
class ScheduledCall:
    """One unit of evaluation work: this candidate, this task."""

    candidate: str
    task_id: str
    task_type: str
    reason: str

    def as_dict(self) -> dict:
        return {"candidate": self.candidate, "task_id": self.task_id,
                "task_type": self.task_type, "reason": self.reason}


class EvaluationScheduler:
    """
    Decides what to run next so the quota matrix fills evenly.

    A rotation plan says who SHOULD answer what. A scheduler is needed on top
    because reality diverges from the plan: a provider goes down mid-run, a
    candidate is added later, half the plan completes before an interruption.
    Re-running the plan from the start would then re-spend calls on the cells
    that are already full while leaving the empty ones empty.

    So the scheduler works from what has actually been observed, not from what
    was planned:

        coverage so far  ->  emptiest (candidate, task_type) cell  ->  a task
        that candidate has not already answered

    Candidates whose provider is unusable are skipped rather than scheduled
    and failed. Scheduling work for a host behind a firewall produces a queue
    of certain failures and a coverage matrix full of zeros that look like
    poor performance.
    """

    def __init__(self, *, quota: int = DEFAULT_QUOTA, usable=None):
        self.quota = quota
        # `usable(candidate) -> bool`, normally backed by the provider
        # registry. Default assumes everything is usable, which is right for
        # tests and wrong for production -- so production passes one.
        self.usable = usable or (lambda candidate: True)

    def plan(self, *, candidates: list[str], tasks: list[tuple[str, str]],
             coverage: dict[str, dict[str, int]],
             answered: dict[str, set] | None = None,
             limit: int = 20) -> list[ScheduledCall]:
        """
        `coverage` is candidate -> task_type -> count (from the inference
        ledger). `answered` is candidate -> set of task_ids already done, so
        the same candidate is not handed the same item twice.
        """
        answered = answered or {}
        task_types = sorted({t for _, t in tasks})
        live = [c for c in candidates if self.usable(c)]
        if not live:
            return []

        state = quota_state(coverage, candidates=live, task_types=task_types,
                            quota=self.quota)
        by_type: dict[str, list[str]] = defaultdict(list)
        for task_id, task_type in tasks:
            by_type[task_type].append(task_id)

        scheduled: list[ScheduledCall] = []
        # Work down the emptiest cells. Recomputing after each pick would be
        # tidier but O(n^2); instead the deficit is decremented in place.
        deficits = {(c, t): state.remaining(c, t) for c in live for t in task_types}
        while len(scheduled) < limit:
            candidates_by_need = sorted(
                ((deficit, cell) for cell, deficit in deficits.items() if deficit > 0),
                key=lambda row: (-row[0], row[1]))
            if not candidates_by_need:
                break
            placed = False
            for _, (candidate, task_type) in candidates_by_need:
                done = answered.get(candidate, set())
                available = [t for t in by_type.get(task_type, ()) if t not in done]
                if not available:
                    deficits[(candidate, task_type)] = 0   # nothing left to give it
                    continue
                task_id = available[0]
                answered.setdefault(candidate, set()).add(task_id)
                deficits[(candidate, task_type)] -= 1
                scheduled.append(ScheduledCall(
                    candidate, task_id, task_type,
                    f"{candidate} has {state.observed[candidate][task_type]} of {self.quota} "
                    f"{task_type} observations"))
                placed = True
                break
            if not placed:
                break
        return scheduled

    def progress(self, *, candidates: list[str], task_types: list[str],
                 coverage: dict[str, dict[str, int]]) -> dict:
        """How full the matrix is, and what is still missing."""
        state = quota_state(coverage, candidates=candidates, task_types=task_types,
                            quota=self.quota)
        cells = len(candidates) * len(task_types)
        filled = sum(1 for c in candidates for t in task_types
                     if state.observed[c][t] >= self.quota)
        return {
            "quota": self.quota,
            "cells": cells,
            "cells_filled": filled,
            "fraction_complete": (filled / cells) if cells else None,
            "underfilled": [{"candidate": c, "task_type": t, "still_needed": n}
                            for c, t, n in state.underfilled()],
            "complete": state.complete(),
            "note": ("" if state.complete() else
                     f"{cells - filled} of {cells} (candidate, task type) cells are below "
                     f"the {self.quota}-observation quota. Any ranking drawn from this "
                     "matrix is provisional."),
        }
