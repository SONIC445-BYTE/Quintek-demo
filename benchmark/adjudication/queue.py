"""
Human review workflow: blind two-rater queue, sentinel insertion, senior
adjudication escalation.

Per docs/INTER_RATER_AND_HUMAN_REVIEW.md:
  - two independent primary raters per item, blinded to model identity
  - randomized item order, independently per rater
  - no discussion before initial rating
  - a senior adjudicator resolves disagreements; both original labels are kept
  - 5% previously-adjudicated sentinel items are inserted without being
    identified as sentinels, to detect reviewer drift

This module is the workflow mechanism only. It cannot supply real qualified
reviewers -- see docs/REVIEWER_QUALIFICATION.md and docs/REVIEW_CAPACITY.md.
A queue built with one rater is a legitimate developmental configuration;
`benchmark.stats.cohens_kappa` correctly raises rather than return a
degenerate value if asked to compute agreement from it.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ReviewAssignment:
    item_id: str
    rater_id: str
    is_sentinel: bool
    order_index: int


class ReviewQueue:
    """Builds a per-rater, independently-randomized, blind review queue."""

    def __init__(
        self,
        raters: list[str],
        sentinel_bank: dict[str, Any] | None = None,
        sentinel_rate: float = 0.05,
        seed: int = 20260814,
    ):
        if not raters:
            raise ValueError("at least one rater id is required to build a queue")
        if len(set(raters)) != len(raters):
            raise ValueError("rater ids must be unique")
        self.raters = list(raters)
        self.sentinel_bank = dict(sentinel_bank or {})
        self.sentinel_rate = sentinel_rate
        self.seed = seed

    @property
    def raters_per_item(self) -> int:
        """
        Two raters per item is the design in INTER_RATER_AND_HUMAN_REVIEW.md.
        With a single configured rater this degrades to one -- a developmental
        configuration, not an error -- and the caller must not compute kappa
        from the result (see docs/REVIEW_CAPACITY.md).
        """
        return min(2, len(self.raters))

    @property
    def blind(self) -> bool:
        """
        Structural, not configurable: this queue never carries candidate
        identity, ranking, or other raters' labels alongside an assignment.
        """
        return True

    def build(self, item_ids: list[str]) -> dict[str, list[ReviewAssignment]]:
        """
        Returns {rater_id: [assignments]}, one list per assigned rater, each
        independently shuffled and independently seeded with sentinels so
        that no rater's order or sentinel placement reveals the other's.
        """
        if not item_ids:
            raise ValueError("no items to queue")
        assigned = self.raters[: self.raters_per_item]
        sentinel_ids = list(self.sentinel_bank)
        n_sentinels = min(round(len(item_ids) * self.sentinel_rate), len(sentinel_ids))

        queues: dict[str, list[ReviewAssignment]] = {}
        for offset, rater in enumerate(assigned):
            rng = random.Random(f"{self.seed}:{rater}:{offset}")
            chosen_sentinels = rng.sample(sentinel_ids, n_sentinels) if n_sentinels else []
            combined = [(iid, False) for iid in item_ids] + [(iid, True) for iid in chosen_sentinels]
            rng.shuffle(combined)
            queues[rater] = [
                ReviewAssignment(item_id=iid, rater_id=rater, is_sentinel=is_sent, order_index=i)
                for i, (iid, is_sent) in enumerate(combined)
            ]
        return queues


@dataclass
class AdjudicationRecord:
    """
    Per docs/CRITICAL_MEDICAL_ERROR.md: on disagreement, do not average. Both
    rater labels are preserved permanently; only the senior-adjudicated label
    is ever used for scoring.
    """

    item_id: str
    rater_labels: dict[str, Any]
    disagreement: bool
    senior_adjudicator: str | None = None
    final_label: Any | None = None
    rationale: str = ""
    status: str = "pending"  # pending | confirmed


class SeniorAdjudicationQueue:
    """Escalation path for rater disagreement. Append-only history."""

    def __init__(self):
        self.pending: list[AdjudicationRecord] = []
        self.resolved: list[AdjudicationRecord] = []

    def submit(self, item_id: str, rater_labels: dict[str, Any]) -> AdjudicationRecord:
        if len(rater_labels) < 2:
            raise ValueError("at least two rater labels are required to detect disagreement")
        distinct = set(rater_labels.values())
        disagreement = len(distinct) > 1
        rec = AdjudicationRecord(item_id=item_id, rater_labels=dict(rater_labels),
                                  disagreement=disagreement)
        if not disagreement:
            rec.final_label = next(iter(distinct))
            rec.status = "confirmed"
            self.resolved.append(rec)
        else:
            self.pending.append(rec)
        return rec

    def adjudicate(self, item_id: str, senior_adjudicator: str, final_label: Any,
                    rationale: str = "") -> AdjudicationRecord:
        """A single reviewer marking CME is never a gate event (per
        CRITICAL_MEDICAL_ERROR.md); only this call, by a senior adjudicator,
        may resolve a pending disagreement."""
        if not senior_adjudicator:
            raise ValueError("senior_adjudicator is required to resolve a disagreement")
        for i, rec in enumerate(self.pending):
            if rec.item_id == item_id:
                rec.senior_adjudicator = senior_adjudicator
                rec.final_label = final_label
                rec.rationale = rationale
                rec.status = "confirmed"
                self.resolved.append(self.pending.pop(i))
                return rec
        raise KeyError(f"no pending disagreement for item '{item_id}'")


@dataclass
class SentinelMonitor:
    """
    Detects reviewer drift via previously-adjudicated sentinel items mixed
    blindly into the live queue (docs/INTER_RATER_AND_HUMAN_REVIEW.md).
    """

    reference_labels: dict[str, Any]
    agreement_threshold: float = 0.80
    observed: list[tuple[str, Any]] = field(default_factory=list)

    def record(self, item_id: str, rater_label: Any) -> None:
        if item_id in self.reference_labels:
            self.observed.append((item_id, rater_label))

    @property
    def agreement_rate(self) -> float | None:
        if not self.observed:
            return None
        correct = sum(1 for iid, lab in self.observed if lab == self.reference_labels[iid])
        return correct / len(self.observed)

    @property
    def drift_detected(self) -> bool:
        """True => pause the run and recalibrate, per the spec."""
        rate = self.agreement_rate
        return rate is not None and rate < self.agreement_threshold
