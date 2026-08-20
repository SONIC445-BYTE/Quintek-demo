"""
Tests for the billing HTTP surface.

Three properties: the frontend cannot decide entitlement, learners cannot see
AI economics, and a refusal is rendered as something the UI can act on rather
than as a server error.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from billing.api import BillingAPI
from billing.gateway import RazorpayAdapter, signature_for
from billing.plans import PlanStore

SECRET = "whsec_test"


@pytest.fixture()
def api(tmp_path):
    conn = sqlite3.connect(tmp_path / "b.db", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(open("billing/schema.sql").read())
    plans = PlanStore(conn)
    plans.seed_from_config()
    for plan in plans.all_active():
        if plan.price_minor > 0:
            plans.set_gateway_ref(plan.id, "razorpay", "plan_rzp_" + plan.id)
    gateway = RazorpayAdapter(key_id="rzp_test", webhook_secret=SECRET,
                              transport=lambda m, p, b, a: {"id": "sub_rzp_1"})
    return BillingAPI(conn, gateway=gateway)


# ---------------------------------------------------------------------------
# The pricing page
# ---------------------------------------------------------------------------

def test_pricing_is_public(api):
    status, body = api.handle("GET", "/pricing")
    assert status == 200
    assert {f["family"] for f in body["families"]} == {"free", "student", "pro", "power"}


def test_pricing_matches_the_commercial_table(api):
    _, body = api.handle("GET", "/pricing")
    families = {f["family"]: f for f in body["families"]}
    assert families["pro"]["intervals"]["monthly"]["price_display"] == "₹499"
    assert families["pro"]["intervals"]["annual"]["price_display"] == "₹4,990"
    assert families["pro"]["monthly_question_allowance"] == 5000
    assert families["pro"]["daily_question_limit"] == 300
    assert families["pro"]["session_question_limit"] == 500
    assert families["power"]["monthly_question_allowance"] == 10000


def test_annual_communicates_the_saving(api):
    _, body = api.handle("GET", "/pricing")
    pro = next(f for f in body["families"] if f["family"] == "pro")
    assert pro["annual_saving"]["months_free"] == 2
    assert "Save ~2 months" == pro["annual_saving"]["label"]


def test_the_pricing_page_exposes_no_ai_economics(api):
    _, body = api.handle("GET", "/pricing")
    text = json.dumps(body).lower()
    for forbidden in ("provider", "token", "model", "nvidia", "openrouter", "cerebras",
                      "compute_unit"):
        assert forbidden not in text, forbidden


# ---------------------------------------------------------------------------
# The frontend never decides
# ---------------------------------------------------------------------------

def test_a_client_supplied_remaining_count_is_ignored(api):
    """
    A frontend claiming it has 99,999 remaining changes nothing: the server
    recomputes from the ledger.
    """
    status, body = api.handle("POST", "/me/usage/reserve",
                              body={"questions": 5, "remaining": 99_999,
                                    "daily_remaining": 99_999},
                              user_id="u1")
    assert status == 201
    assert body["question_units"] == 5


def test_a_free_user_cannot_reserve_beyond_the_daily_cap(api):
    """Free is 20/day. A 500 request is partially granted at 20, not at 500."""
    status, body = api.handle("POST", "/me/usage/reserve", body={"questions": 500},
                              user_id="u1")
    assert status == 201
    assert body["question_units"] == 20


def test_exhausting_the_daily_cap_then_asking_again_is_refused_with_actions(api):
    api.handle("POST", "/me/usage/reserve", body={"questions": 20}, user_id="u1")
    status, body = api.handle("POST", "/me/usage/reserve", body={"questions": 10},
                              user_id="u1")
    assert status == 402
    assert body["available"] == 0
    assert "upgrade" in body["actions"]


def test_check_reports_a_partial_grant_without_consuming_anything(api):
    status, body = api.handle("POST", "/me/usage/check", body={"questions": 500},
                              user_id="u1")
    assert status == 200
    assert body["partial"] is True
    assert body["granted"] == 20
    # Nothing was reserved by checking.
    _, usage = api.handle("GET", "/me/usage", user_id="u1")
    assert usage["today"]["used"] == 0


# ---------------------------------------------------------------------------
# Auth boundaries
# ---------------------------------------------------------------------------

def test_learner_routes_require_a_user(api):
    for path in ("/me/entitlements", "/me/usage", "/me/subscription"):
        assert api.handle("GET", path)[0] == 401, path


def test_the_admin_surface_is_invisible_to_a_learner(api):
    """404, not 403: whether an admin surface exists is not their business."""
    status, body = api.handle("GET", "/admin/economics", user_id="u1")
    assert status == 404
    assert "economics" not in json.dumps(body).lower() or "no such endpoint" in body["error"]


def test_an_admin_sees_economics(api):
    status, body = api.handle("GET", "/admin/economics", user_id="a", is_admin=True)
    assert status == 200
    assert "contribution" in body
    assert "ai_cost" in body


def test_a_learner_cannot_reach_another_users_reservation(api):
    _, reservation = api.handle("POST", "/me/usage/reserve", body={"questions": 5},
                                user_id="u1")
    status, _ = api.handle("POST",
                           f"/me/usage/reservations/{reservation['reservation_id']}/commit",
                           user_id="u2")
    assert status == 404


def test_refunds_cannot_be_triggered_from_the_app(api):
    status, body = api.handle("POST", "/me/subscription/refund", user_id="u1")
    assert status == 403
    assert "support" in body["error"]


# ---------------------------------------------------------------------------
# The reserve -> commit lifecycle
# ---------------------------------------------------------------------------

def test_committing_less_than_reserved_returns_the_difference(api):
    _, reservation = api.handle("POST", "/me/usage/reserve", body={"questions": 10},
                                user_id="u1")
    status, body = api.handle(
        "POST", f"/me/usage/reservations/{reservation['reservation_id']}/commit",
        body={"actual_units": 7}, user_id="u1")
    assert status == 200
    assert body["committed_units"] == 7
    assert body["released_units"] == 3

    _, usage = api.handle("GET", "/me/usage", user_id="u1")
    assert usage["today"]["used"] == 7


def test_a_released_reservation_returns_the_allowance(api):
    _, reservation = api.handle("POST", "/me/usage/reserve", body={"questions": 20},
                                user_id="u1")
    _, before = api.handle("GET", "/me/usage", user_id="u1")
    assert before["today"]["remaining"] == 0

    api.handle("POST", f"/me/usage/reservations/{reservation['reservation_id']}/release",
               user_id="u1")
    _, after = api.handle("GET", "/me/usage", user_id="u1")
    assert after["today"]["remaining"] == 20


def test_the_usage_screen_reports_month_day_and_session_separately(api):
    _, body = api.handle("GET", "/me/usage", user_id="u1")
    assert set(body["this_month"]) == {"used", "allowance", "remaining"}
    assert set(body["today"]) == {"used", "limit", "remaining"}
    assert set(body["session"]) == {"limit", "available_now"}


# ---------------------------------------------------------------------------
# The Billing screen under More
# ---------------------------------------------------------------------------

def test_a_free_user_sees_a_billing_screen_that_offers_plans(api):
    status, body = api.handle("GET", "/me/subscription", user_id="u1")
    assert status == 200
    assert body["plan"] == "free"
    assert body["manageable"] is False
    assert "view_plans" in body["actions"]


def test_a_paid_user_sees_their_plan_and_can_manage_it(api):
    plans = PlanStore(api.conn)
    api.handle("POST", "/me/subscription/checkout",
               body={"plan_id": plans.active("pro", "monthly").id}, user_id="u1")
    payload = {"id": "evt_1", "event": "subscription.charged",
               "payload": {"subscription": {"entity": {
                   "id": "sub_rzp_1", "status": "active",
                   "current_start": 1787200000, "current_end": 1789800000}}}}
    raw = json.dumps(payload).encode()
    api.handle("POST", "/webhooks/razorpay", raw_body=raw,
               signature=signature_for(raw, SECRET))

    status, body = api.handle("GET", "/me/subscription", user_id="u1")
    assert status == 200
    assert body["plan"] == "pro"
    assert body["price_display"] == "₹499"
    assert body["manageable"] is True
    assert body["usage"]["monthly_allowance"] == 5000


def test_the_billing_screen_shows_no_ai_economics(api):
    _, body = api.handle("GET", "/me/subscription", user_id="u1")
    text = json.dumps(body).lower()
    for forbidden in ("provider", "cost_micro", "nvidia", "openrouter"):
        assert forbidden not in text


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------

def test_a_replayed_webhook_answers_200_so_the_gateway_stops_retrying(api):
    plans = PlanStore(api.conn)
    api.handle("POST", "/me/subscription/checkout",
               body={"plan_id": plans.active("pro", "monthly").id}, user_id="u1")
    payload = {"id": "evt_1", "event": "subscription.charged",
               "payload": {"subscription": {"entity": {"id": "sub_rzp_1",
                                                       "status": "active"}}}}
    raw = json.dumps(payload).encode()
    signature = signature_for(raw, SECRET)

    first = api.handle("POST", "/webhooks/razorpay", raw_body=raw, signature=signature)
    second = api.handle("POST", "/webhooks/razorpay", raw_body=raw, signature=signature)
    assert first[0] == 200 and first[1]["status"] == "PROCESSED"
    assert second[0] == 200 and second[1]["status"] == "IGNORED"


def test_an_unattributable_webhook_returns_500_so_the_gateway_retries(api):
    payload = {"id": "evt_x", "event": "subscription.charged",
               "payload": {"subscription": {"entity": {"id": "sub_unknown",
                                                       "status": "active"}}}}
    raw = json.dumps(payload).encode()
    status, body = api.handle("POST", "/webhooks/razorpay", raw_body=raw,
                              signature=signature_for(raw, SECRET))
    assert status == 500
    assert body["status"] == "FAILED"
