"""
Tests for free-plan abuse signals.

The design constraint: hostels, colleges, carriers and families share IP
addresses, and a medical student in a hostel is the core user. Most of these
tests are therefore about who must NOT be punished.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from billing.abuse import (ALLOW, BLOCK, REVIEW, SIGNALS, THRESHOLDS, THROTTLE, VERIFY,
                           AbuseAssessor)


@pytest.fixture()
def assessor(tmp_path):
    conn = sqlite3.connect(tmp_path / "b.db")
    conn.row_factory = sqlite3.Row
    conn.executescript(open("billing/schema.sql").read())
    return AbuseAssessor(conn)


# ---------------------------------------------------------------------------
# Who must not be punished
# ---------------------------------------------------------------------------

def test_a_shared_ip_alone_never_restricts_anyone(assessor):
    """The hostel case. This is the whole reason the weights are shaped so."""
    result = assessor.assess("u1", observations={"shared_ip_many_accounts": True})
    assert result.action == ALLOW
    assert result.blocking is False


def test_no_single_signal_can_block(assessor):
    """
    BLOCK needs 80 and the heaviest signal is 30, so blocking requires several
    independent observations rather than one loud one.
    """
    assert max(s.weight for s in SIGNALS.values()) < 80
    for key in SIGNALS:
        result = assessor.assess("u1", observations={key: True})
        assert result.action != BLOCK, key


def test_an_ordinary_new_user_is_not_restricted(assessor):
    """Every legitimate account is new once, and most have not opened the
    verification email yet."""
    result = assessor.assess("u1", observations={"account_minutes_old": True,
                                                 "email_unverified": True})
    assert result.action in (ALLOW, VERIFY)
    assert result.blocking is False


def test_a_hostel_user_with_a_new_unverified_account_is_asked_to_verify_not_blocked(assessor):
    result = assessor.assess("u1", observations={
        "shared_ip_many_accounts": True, "email_unverified": True,
        "account_minutes_old": True})
    assert result.action == VERIFY
    assert "confirm their email" in result.as_dict()["response"]


def test_a_paying_user_is_never_assessed(assessor):
    """Payment is a stronger identity signal than any heuristic here."""
    everything = {key: True for key in SIGNALS}
    result = assessor.assess("u1", observations=everything, plan_family="pro")
    assert result.action == ALLOW
    assert result.score == 0


def test_exam_week_bursting_alone_does_not_block(assessor):
    """Revision before an exam is genuinely bursty."""
    result = assessor.assess("u1", observations={"burst_generation": True,
                                                 "immediate_exhaustion": True})
    assert result.action != BLOCK


# ---------------------------------------------------------------------------
# Who is caught
# ---------------------------------------------------------------------------

def test_the_scraping_pattern_is_caught(assessor):
    """
    Disposable email, many accounts on one device, never answers a question,
    generates in bursts. No innocent reading covers all four.
    """
    result = assessor.assess("u1", observations={
        "disposable_email": True, "many_accounts_one_device": True,
        "no_attempts_recorded": True, "burst_generation": True})
    assert result.action == BLOCK
    assert result.score >= 80


def test_generating_without_ever_answering_is_weighted_heavily(assessor):
    """The clearest scraping shape: the questions are being taken, not used."""
    assert SIGNALS["no_attempts_recorded"].weight >= 25


def test_a_device_shared_by_many_accounts_weighs_more_than_a_shared_ip(assessor):
    """A device id is not shared the way a carrier NAT address is."""
    assert (SIGNALS["many_accounts_one_device"].weight
            > SIGNALS["shared_ip_many_accounts"].weight * 3)


# ---------------------------------------------------------------------------
# Graduated response
# ---------------------------------------------------------------------------

def test_a_throttled_account_keeps_working(assessor):
    """Friction, not denial: a false positive must be recoverable without a
    support ticket."""
    # 25 + 20 = 45, inside the THROTTLE band (40-59).
    result = assessor.assess("u1", observations={
        "disposable_email": True, "burst_generation": True})
    assert result.action == THROTTLE, f"scored {result.score}"
    limit = assessor.effective_daily_limit(20, result)
    assert 0 < limit < 20


def test_only_a_block_reduces_the_limit_to_zero(assessor):
    blocked = assessor.assess("u1", observations={
        "disposable_email": True, "many_accounts_one_device": True,
        "no_attempts_recorded": True, "burst_generation": True})
    assert assessor.effective_daily_limit(20, blocked) == 0

    allowed = assessor.assess("u2", observations={})
    assert assessor.effective_daily_limit(20, allowed) == 20


def test_a_review_action_does_not_restrict_meanwhile(assessor):
    result = assessor.assess("u1", observations={
        "disposable_email": True, "many_accounts_one_device": True,
        "email_unverified": True})
    assert result.action == REVIEW
    assert assessor.effective_daily_limit(20, result) == 20


@pytest.mark.parametrize("score, expected", [
    (0, ALLOW), (19, ALLOW),
    (20, VERIFY), (39, VERIFY),
    (40, THROTTLE), (59, THROTTLE),
    (60, REVIEW), (79, REVIEW),
    (80, BLOCK), (200, BLOCK),
])
def test_each_score_band_maps_to_its_action(score, expected):
    """The bands themselves, independent of which signals produce a score."""
    action = ALLOW
    for threshold, candidate in THRESHOLDS:
        if score >= threshold:
            action = candidate
            break
    assert action == expected


def test_the_thresholds_are_ordered_from_severe_to_mild():
    scores = [threshold for threshold, _ in THRESHOLDS]
    assert scores == sorted(scores, reverse=True)
    assert THRESHOLDS[0][1] == BLOCK


# ---------------------------------------------------------------------------
# Explaining a decision
# ---------------------------------------------------------------------------

def test_every_signal_carries_an_innocent_reading():
    """
    Shown to whoever reviews a flagged account. Every signal here has an
    innocent explanation, and forgetting that is how a hostel gets banned.
    """
    for signal in SIGNALS.values():
        assert signal.innocent_reading, signal.key
        assert len(signal.innocent_reading) > 20, signal.key


def test_an_assessment_reports_what_triggered_it(assessor):
    result = assessor.assess("u1", observations={
        "disposable_email": True, "many_accounts_one_device": True})
    payload = result.as_dict()
    assert {s["key"] for s in payload["signals"]} == {"disposable_email",
                                                     "many_accounts_one_device"}
    assert all(s["innocent_reading"] for s in payload["signals"])
    assert "Weigh them together" in payload["review_note"]


# ---------------------------------------------------------------------------
# Observation gathering
# ---------------------------------------------------------------------------

def test_observing_records_the_account_without_storing_an_ip_address(assessor):
    assessor.observe("u1", device_id="dev-1", ip_hash="hashed-value")
    row = assessor.conn.execute("SELECT * FROM abuse_observations").fetchone()
    assert row["ip_hash"] == "hashed-value"
    assert set(row.keys()) == {"user_id", "device_id", "ip_hash", "seen_at"}


def test_a_shared_ip_needs_many_accounts_before_it_even_registers(assessor):
    """A household or a small hostel floor must not trip it."""
    for index in range(10):
        assessor.observe(f"u{index}", ip_hash="shared")
    observations = assessor.observe("u-new", ip_hash="shared")
    assert observations.get("shared_ip_many_accounts") is False


def test_a_device_shared_by_several_accounts_registers_sooner(assessor):
    for index in range(5):
        assessor.observe(f"u{index}", device_id="one-device")
    observations = assessor.observe("u-new", device_id="one-device")
    assert observations["many_accounts_one_device"] is True


def test_a_brand_new_account_is_observed_as_such(assessor):
    now = datetime.now(timezone.utc)
    created = (now - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    observations = assessor.observe("u1", account_created_at=created, at=now)
    assert observations["account_minutes_old"] is True

    old = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    observations = assessor.observe("u2", account_created_at=old, at=now)
    assert observations["account_minutes_old"] is False


def test_a_verified_email_is_not_a_signal(assessor):
    observations = assessor.observe("u1", email_verified=True)
    assert observations["email_unverified"] is False
