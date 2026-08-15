"""
Gold error and appeal pathway.

Per docs/GOLD_ERROR_PATHWAY.md: gold can be wrong, and the benchmark must be
able to discover and repair it without corrupting historical runs. This
module records the workflow and its audit trail; it does not adjudicate
medical content, and a model being correct does NOT by itself prove gold is
wrong -- that judgement belongs to the two independent medical reviewers and,
on persistent disagreement, a senior adjudicator.

Historical run results are immutable: correcting gold never rewrites a past
`report.json` (see docs/SCORECARD_SPEC.md). It produces a new dataset version
that only future runs pick up.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

# Per docs/GOLD_ERROR_PATHWAY.md "Challenge triggers".
VALID_TRIGGERS = {
    "reviewers_flag_gold",
    "candidate_consistently_contrary_across_systems",
    "new_authoritative_evidence",
    "ambiguity_discovered",
    "internal_inconsistency",
}


class ItemLifecycle(str, Enum):
    PROPOSED = "proposed"
    REVIEWED = "independently_reviewed"
    VERIFIED = "verified"
    RELEASED = "released"
    CHALLENGED = "challenged"
    ADJUDICATED = "adjudicated"
    CORRECTED = "corrected"
    SUPERSEDED = "superseded"


@dataclass
class GoldChallenge:
    item_id: str
    old_gold_hash: str
    trigger: str
    evidence: str = ""
    reviewer_ids: list[str] = field(default_factory=list)
    senior_adjudicator: str | None = None
    new_gold_hash: str | None = None
    effective_dataset_version: str | None = None
    rationale: str = ""
    status: str = "open"  # open | adjudicated | rejected
    opened_at: float = field(default_factory=time.time)


class GoldChallengeLedger:
    """
    Append-only. Nothing here mutates or removes a prior entry -- correction
    is always a new record referencing the old one, per the lifecycle:
    proposed -> reviewed -> verified -> released -> challenged -> adjudicated
    -> corrected/superseded.
    """

    def __init__(self):
        self.challenges: list[GoldChallenge] = []

    def open_challenge(self, item_id: str, old_gold_hash: str, trigger: str,
                        evidence: str = "") -> GoldChallenge:
        if trigger not in VALID_TRIGGERS:
            raise ValueError(f"unrecognised challenge trigger '{trigger}'; "
                             f"must be one of {sorted(VALID_TRIGGERS)}")
        if not old_gold_hash:
            raise ValueError("old_gold_hash is required: the original item must be frozen "
                             "before a challenge is opened")
        ch = GoldChallenge(item_id=item_id, old_gold_hash=old_gold_hash,
                          trigger=trigger, evidence=evidence)
        self.challenges.append(ch)
        return ch

    def assign_reviewers(self, item_id: str, reviewer_ids: list[str]) -> GoldChallenge:
        """Two independent medical reviewers, per the spec step 4."""
        if len(reviewer_ids) < 2:
            raise ValueError("gold challenge review requires at least two independent reviewers")
        ch = self._open_for(item_id)
        ch.reviewer_ids = list(reviewer_ids)
        return ch

    def adjudicate(self, item_id: str, senior_adjudicator: str, new_gold_hash: str,
                    new_dataset_version: str, rationale: str = "") -> GoldChallenge:
        """Reviewer disagreement persisted -> senior adjudicator decides (step 5)."""
        ch = self._open_for(item_id)
        if not ch.reviewer_ids:
            raise ValueError("cannot adjudicate before reviewers are assigned")
        ch.senior_adjudicator = senior_adjudicator
        ch.new_gold_hash = new_gold_hash
        ch.effective_dataset_version = new_dataset_version
        ch.rationale = rationale
        ch.status = "adjudicated"
        return ch

    def reject(self, item_id: str, senior_adjudicator: str, rationale: str = "") -> GoldChallenge:
        """The challenge did not hold; old gold stands."""
        ch = self._open_for(item_id)
        ch.senior_adjudicator = senior_adjudicator
        ch.rationale = rationale
        ch.status = "rejected"
        return ch

    def open_for_item(self, item_id: str) -> list[GoldChallenge]:
        return [c for c in self.challenges if c.item_id == item_id and c.status == "open"]

    def _open_for(self, item_id: str) -> GoldChallenge:
        for ch in reversed(self.challenges):
            if ch.item_id == item_id and ch.status == "open":
                return ch
        raise KeyError(f"no open gold challenge for item '{item_id}'")
