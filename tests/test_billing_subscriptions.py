"""
Tests for the gateway boundary, webhooks and the subscription lifecycle.

Two properties dominate: a payment is real only when the gateway says so, and
a retried webhook must not grant a second period.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from billing.entitlements import EntitlementEngine
from billing.gateway import (ACTIVE, ALL_STATES, CANCEL_AT_PERIOD_END, CANCELLED, EXPIRED,
                             PAST_DUE, PAYMENT_FAILED, PENDING, TRIALING, GatewayError,
                             RazorpayAdapter, SignatureInvalid, signature_for)
from billing.plans import PlanStore
from billing.subscriptions import VALID_TRANSITIONS, SubscriptionService

SECRET = "whsec_test"


@pytest.fixture()
def env(tmp_path):
    conn = sqlite3.connect(tmp_path / "b.db")
    conn.row_factory = sqlite3.Row
    conn.executescript(open("billing/schema.sql").read())
    plans = PlanStore(conn)
    plans.seed_from_config()
    # Checkout needs the GATEWAY's plan id, so a fixture that omits it is
    # testing a deployment that would fail on its first sale.
    for plan in plans.all_active():
        if plan.price_minor > 0:
            plans.set_gateway_ref(plan.id, "razorpay", "plan_rzp_" + plan.id)
    gateway = RazorpayAdapter(key_id="rzp_test", webhook_secret=SECRET,
                              transport=lambda m, p, b, a: {"id": "sub_rzp_1"})
    service = SubscriptionService(conn, gateway=gateway, plans=plans)
    return conn, plans, service, EntitlementEngine(conn, plans)


def webhook(event_type, status, *, event_id="evt_1", sub="sub_rzp_1"):
    payload = {"id": event_id, "event": event_type,
               "payload": {"subscription": {"entity": {
                   "id": sub, "status": status,
                   "current_start": 1787200000, "current_end": 1789800000}}}}
    body = json.dumps(payload).encode()
    return body, signature_for(body, SECRET)


# ---------------------------------------------------------------------------
# Gateway states never leak
# ---------------------------------------------------------------------------

def test_every_gateway_status_maps_into_quintek_vocabulary():
    for mapped in RazorpayAdapter.STATUS_MAP.values():
        assert mapped in ALL_STATES
    for mapped in RazorpayAdapter.EVENT_MAP.values():
        assert mapped in ALL_STATES


def test_an_unknown_gateway_status_does_not_become_active():
    """A gateway can add a status in a release. Granting access on one nobody
    has read is how a free month happens."""
    assert RazorpayAdapter.map_status("some_new_state") == PENDING
    assert RazorpayAdapter.map_status("") == PENDING


def test_the_event_type_wins_over_a_lagging_entity_status():
    adapter = RazorpayAdapter(webhook_secret=SECRET)
    event = adapter.parse_event({"id": "e", "event": "subscription.halted",
                                 "payload": {"subscription": {"entity": {
                                     "id": "s", "status": "active"}}}})
    assert event.mapped_status == PAYMENT_FAILED


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------

def test_a_valid_signature_is_accepted_and_a_tampered_body_is_not():
    adapter = RazorpayAdapter(webhook_secret=SECRET)
    body = b'{"id":"e"}'
    signature = signature_for(body, SECRET)
    assert adapter.verify_signature(body, signature) is True
    assert adapter.verify_signature(body + b" ", signature) is False
    assert adapter.verify_signature(body, "deadbeef") is False


def test_no_webhook_secret_means_nothing_can_be_trusted():
    with pytest.raises(GatewayError, match="no webhook secret"):
        RazorpayAdapter().verify_signature(b"{}", "x")


def test_an_unsigned_webhook_is_refused_and_not_stored(env):
    conn, _, service, _ = env
    body, _ = webhook("subscription.charged", "active")
    with pytest.raises(SignatureInvalid):
        service.handle_webhook(body, "not-a-signature")
    assert conn.execute("SELECT COUNT(*) n FROM webhook_events").fetchone()["n"] == 0


# ---------------------------------------------------------------------------
# The frontend cannot grant access
# ---------------------------------------------------------------------------

def test_checkout_creates_a_pending_subscription_and_grants_nothing(env):
    conn, plans, service, entitlements = env
    service.begin_checkout("u1", plans.active("pro", "monthly").id)

    assert conn.execute("SELECT status FROM subscriptions").fetchone()["status"] == PENDING
    # Still on Free until the gateway confirms.
    assert entitlements.snapshot("u1")["plan"] == "free"
    assert conn.execute("SELECT COUNT(*) n FROM entitlements").fetchone()["n"] == 0


def test_a_verified_webhook_activates_and_grants(env):
    conn, plans, service, entitlements = env
    service.begin_checkout("u1", plans.active("pro", "monthly").id)
    result = service.handle_webhook(*webhook("subscription.charged", "active"))

    assert result.status == "PROCESSED"
    assert result.new_state == ACTIVE
    snapshot = entitlements.snapshot("u1")
    assert snapshot["plan"] == "pro"
    assert snapshot["monthly_allowance"] == 5000
    assert snapshot["daily_limit"] == 300


def test_the_checkout_payload_never_contains_the_key_secret(env):
    _, plans, service, _ = env
    payload = service.begin_checkout("u1", plans.active("pro", "monthly").id)
    assert "secret" not in json.dumps(payload).lower()


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_a_replayed_webhook_does_not_grant_a_second_period(env):
    conn, plans, service, _ = env
    service.begin_checkout("u1", plans.active("pro", "monthly").id)
    first = service.handle_webhook(*webhook("subscription.charged", "active"))
    second = service.handle_webhook(*webhook("subscription.charged", "active"))

    assert first.status == "PROCESSED"
    assert second.status == "IGNORED"
    assert "already been processed" in second.reason
    assert conn.execute("SELECT COUNT(*) n FROM entitlements").fetchone()["n"] == 1


def test_a_different_event_id_is_processed_normally(env):
    _, plans, service, _ = env
    service.begin_checkout("u1", plans.active("pro", "monthly").id)
    service.handle_webhook(*webhook("subscription.charged", "active", event_id="e1"))
    second = service.handle_webhook(*webhook("subscription.charged", "active",
                                             event_id="e2"))
    assert second.status == "PROCESSED"


def test_an_event_without_an_id_is_refused(env):
    _, plans, service, _ = env
    service.begin_checkout("u1", plans.active("pro", "monthly").id)
    payload = {"event": "subscription.charged",
               "payload": {"subscription": {"entity": {"id": "sub_rzp_1",
                                                       "status": "active"}}}}
    body = json.dumps(payload).encode()
    result = service.handle_webhook(body, signature_for(body, SECRET))
    assert result.status == "FAILED"
    assert "replay could not be detected" in result.reason


def test_every_processed_event_is_stored_with_its_outcome(env):
    conn, plans, service, _ = env
    service.begin_checkout("u1", plans.active("pro", "monthly").id)
    service.handle_webhook(*webhook("subscription.charged", "active"))
    row = conn.execute("SELECT * FROM webhook_events").fetchone()
    assert row["processing_status"] == "PROCESSED"
    assert row["signature_valid"] == 1
    assert row["processed_at"]


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------

def test_a_cancelled_subscription_cannot_be_resurrected_by_a_late_event(env):
    conn, plans, service, entitlements = env
    service.begin_checkout("u1", plans.active("pro", "monthly").id)
    service.handle_webhook(*webhook("subscription.charged", "active", event_id="e1"))
    service.handle_webhook(*webhook("subscription.cancelled", "cancelled", event_id="e2"))

    late = service.handle_webhook(*webhook("subscription.charged", "active", event_id="e3"))
    assert late.status == "IGNORED"
    assert "must not resurrect" in late.reason
    assert entitlements.snapshot("u1")["plan"] == "free"


def test_terminal_states_have_no_outward_transitions():
    assert VALID_TRANSITIONS[CANCELLED] == set()
    assert VALID_TRANSITIONS[EXPIRED] == set()


def test_a_failed_payment_moves_to_past_due_and_keeps_access(env):
    """A failed renewal should not cut a paying customer off mid-study while
    the gateway retries."""
    _, plans, service, entitlements = env
    service.begin_checkout("u1", plans.active("pro", "monthly").id)
    service.handle_webhook(*webhook("subscription.charged", "active", event_id="e1"))
    service.handle_webhook(*webhook("subscription.pending", "pending", event_id="e2"))

    snapshot = entitlements.snapshot("u1")
    assert snapshot["status"] == PAST_DUE
    assert snapshot["plan"] == "pro"
    assert snapshot["can_generate_now"] is True


def test_an_event_for_an_unknown_subscription_fails_loudly(env):
    _, _, service, _ = env
    result = service.handle_webhook(*webhook("subscription.charged", "active",
                                             sub="sub_nobody"))
    assert result.status == "FAILED"
    assert "cannot be attributed to a customer" in result.reason


# ---------------------------------------------------------------------------
# Cancellation and downgrade
# ---------------------------------------------------------------------------

def test_cancelling_retains_access_until_the_period_ends(env):
    conn, plans, service, entitlements = env
    service.begin_checkout("u1", plans.active("pro", "monthly").id)
    service.handle_webhook(*webhook("subscription.charged", "active"))

    result = service.cancel("u1")
    assert result["status"] == CANCEL_AT_PERIOD_END
    assert "keep full access until" in result["message"]
    # Still Pro today.
    assert entitlements.snapshot("u1")["plan"] == "pro"


def test_expiry_moves_a_cancelled_subscription_to_free(env):
    from datetime import datetime, timezone

    conn, plans, service, entitlements = env
    service.begin_checkout("u1", plans.active("pro", "monthly").id)
    service.handle_webhook(*webhook("subscription.charged", "active"))
    service.cancel("u1")

    far_future = datetime(2030, 1, 1, tzinfo=timezone.utc)
    assert service.expire_due(at=far_future) == 1
    assert entitlements.snapshot("u1")["plan"] == "free"
    assert entitlements.snapshot("u1")["monthly_allowance"] == 100


def test_a_downgrade_is_scheduled_not_applied(env):
    """The user paid for this period; taking the allowance early takes back
    something already bought."""
    conn, plans, service, entitlements = env
    service.begin_checkout("u1", plans.active("pro", "monthly").id)
    service.handle_webhook(*webhook("subscription.charged", "active"))

    result = service.schedule_downgrade("u1", plans.active("student", "monthly").id)
    assert result["scheduled_plan"] == "Student"
    assert "remains active until" in result["message"]
    # Unchanged today.
    assert entitlements.snapshot("u1")["monthly_allowance"] == 5000
    row = conn.execute("SELECT scheduled_plan_id FROM subscriptions").fetchone()
    assert row["scheduled_plan_id"].startswith("student_")


def test_a_superseded_entitlement_is_closed_not_deleted(env):
    """What someone was entitled to last month must stay answerable."""
    conn, plans, service, _ = env
    service.begin_checkout("u1", plans.active("pro", "monthly").id)
    service.handle_webhook(*webhook("subscription.charged", "active", event_id="e1"))
    service.handle_webhook(*webhook("subscription.cancelled", "cancelled", event_id="e2"))

    rows = conn.execute("SELECT * FROM entitlements WHERE user_id='u1'").fetchall()
    assert len(rows) == 1
    assert rows[0]["effective_until"] is not None


# ---------------------------------------------------------------------------
# A plan the gateway has never heard of
# ---------------------------------------------------------------------------

def test_checkout_refuses_a_plan_with_no_gateway_id(tmp_path):
    """
    Sending Quintek's own plan id to Razorpay names nothing on its side. The
    call fails, or worse succeeds against an unrelated record. Refusing here
    keeps the failure at checkout -- one person seeing an error -- rather than
    at renewal, where it is silent.
    """
    from billing.gateway import GatewayError

    conn = sqlite3.connect(tmp_path / "unlinked.db")
    conn.row_factory = sqlite3.Row
    conn.executescript(open("billing/schema.sql").read())
    plans = PlanStore(conn)
    plans.seed_from_config()
    gateway = RazorpayAdapter(key_id="rzp_test", webhook_secret=SECRET,
                              transport=lambda m, p, b, a: {"id": "sub_rzp_x"})
    service = SubscriptionService(conn, gateway=gateway, plans=plans)

    plan = plans.active("pro", "monthly")
    with pytest.raises(GatewayError) as exc:
        service.begin_checkout("u1", plan.id)
    assert "gateway plan id" in str(exc.value)
    assert "tools_razorpay_sync" in str(exc.value)


def test_the_gateway_receives_its_own_plan_id_not_quinteks(env):
    conn, plans, service, _ = env
    seen = {}

    def transport(method, path, body, auth):
        seen.update(body or {})
        return {"id": "sub_rzp_1"}

    service.gateway.transport = transport
    plan = plans.active("pro", "monthly")
    service.begin_checkout("u1", plan.id)
    assert seen["plan_id"] == "plan_rzp_" + plan.id
    assert seen["plan_id"] != plan.id
