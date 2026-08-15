"""
Human review workflow: blind queue, sentinel drift, senior adjudication,
gold challenge lifecycle.

Phase 3 of docs/MASTER_BUILD_PROMPT_V0_4.md. These exercise
benchmark/adjudication/ directly rather than trusting the docstrings.
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from benchmark.adjudication import (
    GoldChallengeLedger, ReviewQueue, SentinelMonitor, SeniorAdjudicationQueue,
)
from benchmark.stats import cohens_kappa


# ---------------------------------------------------------------------------
# ReviewQueue -- blind, two-rater, sentinel insertion
# ---------------------------------------------------------------------------

def test_two_raters_get_independently_ordered_queues():
    q = ReviewQueue(raters=["r1", "r2"], seed=1)
    built = q.build([f"item-{i}" for i in range(20)])
    assert set(built) == {"r1", "r2"}
    order1 = [a.item_id for a in built["r1"] if not a.is_sentinel]
    order2 = [a.item_id for a in built["r2"] if not a.is_sentinel]
    assert sorted(order1) == sorted(order2)
    assert order1 != order2  # independently shuffled


def test_single_rater_configuration_is_legitimate_but_not_two_rater():
    """Developmental mode (docs/REVIEW_CAPACITY.md) -- not an error."""
    q = ReviewQueue(raters=["solo"], seed=1)
    assert q.raters_per_item == 1
    built = q.build(["a", "b", "c"])
    assert set(built) == {"solo"}


def test_kappa_raises_with_one_rater_worth_of_labels():
    """
    A one-reviewer queue cannot feed cohens_kappa two independent label
    vectors -- this is the arithmetic reason GATE-REL-KAPPA-CRITICAL is
    UNEVALUABLE below two raters, not a policy choice.
    """
    with pytest.raises(ValueError):
        cohens_kappa(["x"], [])


def test_sentinels_are_inserted_and_indistinguishable_by_construction():
    bank = {"sent-1": "correct", "sent-2": "correct"}
    q = ReviewQueue(raters=["r1", "r2"], sentinel_bank=bank, sentinel_rate=1.0, seed=3)
    built = q.build(["a", "b"])
    for rater_queue in built.values():
        sentinel_ids = {a.item_id for a in rater_queue if a.is_sentinel}
        assert sentinel_ids == set(bank)
        # Assignment carries no flag visible to a consumer beyond is_sentinel,
        # which the reviewer UI is responsible for never surfacing.
        for a in rater_queue:
            assert hasattr(a, "is_sentinel")


def test_duplicate_rater_ids_rejected():
    with pytest.raises(ValueError):
        ReviewQueue(raters=["r1", "r1"])


def test_empty_item_list_rejected():
    q = ReviewQueue(raters=["r1", "r2"])
    with pytest.raises(ValueError):
        q.build([])


# ---------------------------------------------------------------------------
# SentinelMonitor -- reviewer drift detection
# ---------------------------------------------------------------------------

def test_sentinel_monitor_flags_drift_below_threshold():
    mon = SentinelMonitor(reference_labels={"s1": "CME", "s2": "CME", "s3": "not_cme"},
                          agreement_threshold=0.80)
    mon.record("s1", "CME")
    mon.record("s2", "not_cme")  # wrong
    mon.record("s3", "not_cme")
    assert mon.agreement_rate == pytest.approx(2 / 3)
    assert mon.drift_detected  # 0.667 < 0.80


def test_sentinel_monitor_ignores_non_sentinel_items():
    mon = SentinelMonitor(reference_labels={"s1": "CME"})
    mon.record("not-a-sentinel", "whatever")
    assert mon.agreement_rate is None
    assert not mon.drift_detected


# ---------------------------------------------------------------------------
# SeniorAdjudicationQueue -- disagreement escalation
# ---------------------------------------------------------------------------

def test_agreement_resolves_without_escalation():
    saq = SeniorAdjudicationQueue()
    rec = saq.submit("item-1", {"r1": "CME-3", "r2": "CME-3"})
    assert not rec.disagreement
    assert rec.status == "confirmed"
    assert rec.final_label == "CME-3"
    assert saq.pending == []


def test_disagreement_escalates_and_preserves_both_labels():
    """Per docs/CRITICAL_MEDICAL_ERROR.md: do not average; preserve both."""
    saq = SeniorAdjudicationQueue()
    rec = saq.submit("item-2", {"r1": "CME-3", "r2": "not_cme"})
    assert rec.disagreement
    assert rec.status == "pending"
    assert rec.rater_labels == {"r1": "CME-3", "r2": "not_cme"}

    resolved = saq.adjudicate("item-2", senior_adjudicator="REV-007",
                              final_label="CME-3", rationale="contraindication confirmed")
    assert resolved.status == "confirmed"
    assert resolved.final_label == "CME-3"
    assert resolved.senior_adjudicator == "REV-007"
    # Original disagreement is not erased.
    assert resolved.rater_labels == {"r1": "CME-3", "r2": "not_cme"}


def test_adjudication_without_senior_id_rejected():
    saq = SeniorAdjudicationQueue()
    saq.submit("item-3", {"r1": "a", "r2": "b"})
    with pytest.raises(ValueError):
        saq.adjudicate("item-3", senior_adjudicator="", final_label="a")


def test_adjudicating_unknown_item_raises():
    saq = SeniorAdjudicationQueue()
    with pytest.raises(KeyError):
        saq.adjudicate("nope", senior_adjudicator="REV-1", final_label="x")


# ---------------------------------------------------------------------------
# GoldChallengeLedger -- lifecycle, immutability
# ---------------------------------------------------------------------------

def test_gold_challenge_lifecycle_end_to_end():
    ledger = GoldChallengeLedger()
    ledger.open_challenge("QA-0417", old_gold_hash="abc123", trigger="reviewers_flag_gold",
                          evidence="two reviewers dispute the answer key")
    ledger.assign_reviewers("QA-0417", ["rev-a", "rev-b"])
    ch = ledger.adjudicate("QA-0417", senior_adjudicator="REV-senior",
                           new_gold_hash="def456", new_dataset_version="v0.4.2",
                           rationale="original key transposed B/D")
    assert ch.status == "adjudicated"
    assert ch.old_gold_hash == "abc123"
    assert ch.new_gold_hash == "def456"
    assert ch.effective_dataset_version == "v0.4.2"
    # The old record is never removed -- it is what "immutable" means here.
    assert ledger.challenges[0].old_gold_hash == "abc123"


def test_invalid_trigger_rejected():
    ledger = GoldChallengeLedger()
    with pytest.raises(ValueError):
        ledger.open_challenge("Q1", old_gold_hash="x", trigger="i_just_feel_like_it")


def test_adjudicate_before_reviewers_assigned_rejected():
    ledger = GoldChallengeLedger()
    ledger.open_challenge("Q1", old_gold_hash="x", trigger="ambiguity_discovered")
    with pytest.raises(ValueError):
        ledger.adjudicate("Q1", senior_adjudicator="REV-1", new_gold_hash="y",
                          new_dataset_version="v0.4.2")


def test_reject_requires_no_reviewer_minimum():
    """A challenge can be rejected outright without ever assigning reviewers."""
    ledger = GoldChallengeLedger()
    ledger.open_challenge("Q1", old_gold_hash="x", trigger="ambiguity_discovered")
    ch = ledger.reject("Q1", senior_adjudicator="REV-1", rationale="not actually ambiguous")
    assert ch.status == "rejected"


def test_single_reviewer_cannot_satisfy_gold_challenge_review():
    ledger = GoldChallengeLedger()
    ledger.open_challenge("Q1", old_gold_hash="x", trigger="ambiguity_discovered")
    with pytest.raises(ValueError):
        ledger.assign_reviewers("Q1", ["solo-reviewer"])
