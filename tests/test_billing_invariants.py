"""
Billing invariants.

Not unit tests of a module -- properties of the SYSTEM that must hold however
the code inside is rearranged. Each one is either a rule somebody could break
by refactoring, or a mistake already made once on this project.

The list is deliberately short and each entry is deliberately blunt. If one of
these fails, something is wrong with the business logic, not with a detail.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from billing.api import BillingAPI
from billing.costs import CostLedger, ModelPrice, OperationCost
from billing.entitlements import EntitlementEngine, period_start_for, today_iso
from billing.gateway import (ACTIVE, CANCEL_AT_PERIOD_END, PAST_DUE, PENDING,
                             RazorpayAdapter, signature_for)
from billing.money import MICRO, Money, micro_to_money, token_cost_micro
from billing.plans import PlanStore, now_iso
from billing.subscriptions import SubscriptionService
from billing.usage import InsufficientAllowance, UsageService

SECRET = "whsec_test"
SCHEMA = open("billing/schema.sql").read()


def fresh(tmp_path, name="b.db"):
    conn = sqlite3.connect(tmp_path / name, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    plans = PlanStore(conn)
    plans.seed_from_config()
    # Every paid plan is linked to a gateway plan, as it must be before a sale
    # can happen at all. A fixture without this tests a deployment that would
    # fail on its first checkout.
    for plan in plans.all_active():
        if plan.price_minor > 0:
            plans.set_gateway_ref(plan.id, "razorpay", "plan_rzp_" + plan.id)
    return conn, plans


def subscribe(conn, plans, user_id, family="pro", interval="monthly"):
    """Put a user on a paid plan via the webhook path, as production does."""
    gateway = RazorpayAdapter(key_id="k", webhook_secret=SECRET,
                              transport=lambda m, p, b, a: {"id": f"sub_{user_id}"})
    service = SubscriptionService(conn, gateway=gateway, plans=plans)
    service.begin_checkout(user_id, plans.active(family, interval).id)
    payload = {"id": f"evt_{user_id}", "event": "subscription.charged",
               "payload": {"subscription": {"entity": {
                   "id": f"sub_{user_id}", "status": "active",
                   "current_start": 1787200000, "current_end": 1789800000}}}}
    raw = json.dumps(payload).encode()
    service.handle_webhook(raw, signature_for(raw, SECRET))
    return service


# ---------------------------------------------------------------------------
# 1. No per-call monetary rounding
# ---------------------------------------------------------------------------

def test_invariant_no_per_call_monetary_rounding(tmp_path):
    """
    10,000 tiny-cost calls, aggregated, compared against the mathematical
    expectation. Per-row rounding inflated this 3.3x once; this is the
    regression guard.
    """
    conn, _ = fresh(tmp_path)
    ledger = CostLedger(conn)
    price = ModelPrice.from_usd_per_million("p", "m", 0.03, 0.03)
    ledger.set_price(price)

    calls, tokens_in = 10_000, 1_000
    for _ in range(calls):
        ledger.record(OperationCost("p", "m", input_tokens=tokens_in, output_tokens=0,
                                    questions_produced=1, questions_accepted=1))

    expected_micro = calls * token_cost_micro(tokens_in, price.input_per_million_micro)
    actual_micro = ledger.totals()["spend_micro"]
    assert actual_micro == expected_micro, (
        f"aggregate cost {actual_micro} != mathematical expectation {expected_micro}; "
        "something is rounding per row")

    # And the inflation this prevents is real, not hypothetical.
    per_row_rounded = calls * micro_to_money(expected_micro // calls).minor
    aggregated_once = micro_to_money(expected_micro).minor
    assert per_row_rounded > aggregated_once


def test_invariant_aggregation_rounds_only_once(tmp_path):
    conn, _ = fresh(tmp_path)
    ledger = CostLedger(conn)
    ledger.set_price(ModelPrice.from_usd_per_million("p", "m", 0.05, 0.05))
    for _ in range(500):
        ledger.record(OperationCost("p", "m", input_tokens=700, output_tokens=300,
                                    questions_produced=1, questions_accepted=1))

    totals = ledger.totals()
    # The stored figure is in micro units and is NOT a whole number of paise.
    assert totals["spend_micro"] % MICRO != 0, (
        "the aggregate landed exactly on a paise boundary, which suggests rounding "
        "happened before aggregation")
    # Presentation rounds; storage does not.
    assert totals["spend_display"].startswith("₹")


def test_invariant_contribution_is_computed_from_unrounded_aggregates(tmp_path):
    from billing.economics import EconomicsService

    conn, plans = fresh(tmp_path)
    ledger = CostLedger(conn)
    ledger.set_price(ModelPrice.from_usd_per_million("p", "m", 0.03, 0.03))
    subscribe(conn, plans, "u1")
    for _ in range(1_000):
        ledger.record(OperationCost("p", "m", user_id="u1", plan_family="pro",
                                    input_tokens=1000, output_tokens=500,
                                    questions_produced=1, questions_accepted=1))

    economics = EconomicsService(conn, plans=plans, costs=ledger)
    daily = economics.daily()
    # Contribution is derived from Money objects built from the micro total,
    # not from a re-parsed display string.
    assert isinstance(daily["contribution_minor"], int)
    assert daily["contribution"].startswith("₹")


# ---------------------------------------------------------------------------
# 2. Session cap may exceed daily cap
# ---------------------------------------------------------------------------

def test_invariant_session_cap_may_exceed_daily_cap(tmp_path):
    """
    Every shipped plan has this relationship. It means "the largest request we
    will accept", not "what you may generate now".
    """
    conn, plans = fresh(tmp_path)
    for family in ("free", "student", "pro"):
        interval = "none" if family == "free" else "monthly"
        plan = plans.active(family, interval)
        assert plan.session_question_limit > plan.daily_question_limit, family

    # And the engine offers the daily figure, not the session one.
    engine = EntitlementEngine(conn, plans)
    decision = engine.authorize("free-user", 50)
    assert decision.allowed is True
    assert decision.granted == 20
    assert decision.partial is True


# ---------------------------------------------------------------------------
# 3. Caps cannot be bypassed
# ---------------------------------------------------------------------------

def test_invariant_daily_cap_survives_concurrent_requests(tmp_path):
    """Section 25: two 200s against a 300 limit must not both pass."""
    conn, plans = fresh(tmp_path)
    subscribe(conn, plans, "u1")           # Pro: 300/day
    path = tmp_path / "b.db"

    outcomes = []

    def attempt():
        own = sqlite3.connect(path, timeout=30, isolation_level=None)
        own.row_factory = sqlite3.Row
        service = UsageService(own, EntitlementEngine(own, PlanStore(own)))
        try:
            reservation = service.reserve("u1", 200, allow_partial=False)
            outcomes.append(reservation.question_units)
        except InsufficientAllowance:
            outcomes.append(0)
        own.close()

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(outcomes) <= 300, f"authorised {sum(outcomes)} against a 300 daily cap"


def test_invariant_monthly_allowance_cannot_be_exceeded(tmp_path):
    conn, plans = fresh(tmp_path)
    subscribe(conn, plans, "u1")
    engine = EntitlementEngine(conn, plans)
    period, day = period_start_for(), today_iso()

    # Spend the whole month in one go, spread so the daily cap is not what binds.
    conn.execute(
        "INSERT INTO usage_ledger (id, user_id, question_units, usage_date, period_start,"
        " created_at) VALUES ('bulk','u1',5000,?,?,?)",
        ("2000-01-01", period, now_iso()))

    availability = engine.availability("u1")
    assert availability.monthly_remaining == 0
    assert availability.available_now == 0
    assert engine.authorize("u1", 1).allowed is False


def test_invariant_a_partial_grant_never_exceeds_what_is_available(tmp_path):
    conn, plans = fresh(tmp_path)
    engine = EntitlementEngine(conn, plans)
    for requested in (1, 20, 50, 500, 10_000):
        decision = engine.authorize("free-user", requested)
        assert decision.granted <= decision.availability.available_now


# ---------------------------------------------------------------------------
# 4. Rollover
# ---------------------------------------------------------------------------

def test_invariant_rollover_cannot_exceed_fifty_percent(tmp_path):
    conn, plans = fresh(tmp_path)
    subscribe(conn, plans, "u1")
    service = UsageService(conn, EntitlementEngine(conn, plans))

    # A completely unused Pro month: 5,000 unused, cap 2,500.
    result = service.apply_rollover("u1")
    assert result["unused"] == 5000
    assert result["cap"] == 2500
    assert result["carried"] == 2500
    assert result["forfeited"] == 2500
    assert result["new_allowance"] == 7500


def test_invariant_rollover_cannot_become_negative(tmp_path):
    conn, plans = fresh(tmp_path)
    subscribe(conn, plans, "u1")
    period = period_start_for()
    # Over-consume beyond the allowance (a correction entry could do this).
    conn.execute(
        "INSERT INTO usage_ledger (id, user_id, question_units, usage_date, period_start,"
        " created_at) VALUES ('over','u1',9999,?,?,?)", ("2000-01-01", period, now_iso()))

    service = UsageService(conn, EntitlementEngine(conn, plans))
    result = service.apply_rollover("u1")
    assert result["unused"] >= 0
    assert result["carried"] >= 0
    assert result["new_allowance"] >= 0


def test_invariant_the_rollover_cap_comes_from_the_plan_not_from_code(tmp_path):
    conn, plans = fresh(tmp_path)
    for family in ("student", "pro", "power"):
        plan = plans.active(family, "monthly")
        assert plan.max_rollover == plan.monthly_question_allowance * plan.rollover_percent // 100


# ---------------------------------------------------------------------------
# 5. Subscription lifecycle
# ---------------------------------------------------------------------------

def test_invariant_cancelled_subscription_retains_access_until_period_end(tmp_path):
    conn, plans = fresh(tmp_path)
    service = subscribe(conn, plans, "u1")
    engine = EntitlementEngine(conn, plans)

    service.cancel("u1")
    assert engine.snapshot("u1")["plan"] == "pro"
    assert engine.snapshot("u1")["monthly_allowance"] == 5000
    assert engine.authorize("u1", 100).allowed is True

    service.expire_due(at=datetime(2030, 1, 1, tzinfo=timezone.utc))
    assert engine.snapshot("u1")["plan"] == "free"


def test_invariant_downgrade_does_not_happen_immediately(tmp_path):
    conn, plans = fresh(tmp_path)
    service = subscribe(conn, plans, "u1")
    engine = EntitlementEngine(conn, plans)

    service.schedule_downgrade("u1", plans.active("student", "monthly").id)
    snapshot = engine.snapshot("u1")
    assert snapshot["plan"] == "pro"
    assert snapshot["monthly_allowance"] == 5000, (
        "the user paid for this period; the allowance must not shrink before it ends")


def test_invariant_failed_payment_does_not_silently_grant_paid_entitlement(tmp_path):
    """A PENDING checkout grants nothing until the gateway confirms."""
    conn, plans = fresh(tmp_path)
    gateway = RazorpayAdapter(key_id="k", webhook_secret=SECRET,
                              transport=lambda m, p, b, a: {"id": "sub_x"})
    service = SubscriptionService(conn, gateway=gateway, plans=plans)
    engine = EntitlementEngine(conn, plans)

    service.begin_checkout("u1", plans.active("power", "monthly").id)
    assert engine.snapshot("u1")["plan"] == "free"
    assert conn.execute("SELECT COUNT(*) n FROM entitlements").fetchone()["n"] == 0

    # A payment failure must not upgrade anyone either.
    payload = {"id": "evt_fail", "event": "payment.failed",
               "payload": {"subscription": {"entity": {"id": "sub_x", "status": "halted"}}}}
    raw = json.dumps(payload).encode()
    service.handle_webhook(raw, signature_for(raw, SECRET))
    assert engine.snapshot("u1")["plan"] == "free"


def test_invariant_duplicate_webhook_cannot_double_credit(tmp_path):
    conn, plans = fresh(tmp_path)
    gateway = RazorpayAdapter(key_id="k", webhook_secret=SECRET,
                              transport=lambda m, p, b, a: {"id": "sub_u1"})
    service = SubscriptionService(conn, gateway=gateway, plans=plans)
    service.begin_checkout("u1", plans.active("pro", "monthly").id)

    payload = {"id": "evt_same", "event": "subscription.charged",
               "payload": {"subscription": {"entity": {"id": "sub_u1", "status": "active"}}}}
    raw = json.dumps(payload).encode()
    signature = signature_for(raw, SECRET)

    for _ in range(5):
        service.handle_webhook(raw, signature)

    assert conn.execute(
        "SELECT COUNT(*) n FROM entitlements WHERE effective_until IS NULL"
    ).fetchone()["n"] == 1
    assert conn.execute("SELECT COUNT(*) n FROM webhook_events").fetchone()["n"] == 1


# ---------------------------------------------------------------------------
# 6. Authority boundaries
# ---------------------------------------------------------------------------

def test_invariant_refund_cannot_be_triggered_by_a_learner(tmp_path):
    conn, plans = fresh(tmp_path)
    api = BillingAPI(conn)
    status, body = api.handle("POST", "/me/subscription/refund", user_id="u1")
    assert status == 403
    assert "support" in body["error"]


def test_invariant_learner_cannot_access_admin_economics(tmp_path):
    conn, plans = fresh(tmp_path)
    api = BillingAPI(conn)
    for path in ("/admin/economics", "/admin/economics/plans",
                 "/admin/economics/cost-per-500", "/admin/economics/models"):
        assert api.handle("GET", path, user_id="u1")[0] == 404, path
        assert api.handle("GET", path, user_id="a", is_admin=True)[0] == 200, path


def test_invariant_the_404_is_a_response_policy_not_the_security_mechanism(tmp_path):
    """
    The check is `is_admin`, evaluated server-side. The 404 is what a learner
    is TOLD; it is not what stops them.
    """
    conn, plans = fresh(tmp_path)
    api = BillingAPI(conn)
    # No user at all, admin claimed by the caller's own flag: still gated.
    assert api.handle("GET", "/admin/economics", user_id=None, is_admin=False)[0] == 404
    source = open("billing/api.py").read()
    assert "if not is_admin" in source


# ---------------------------------------------------------------------------
# 7. Separation of concerns
# ---------------------------------------------------------------------------

def test_invariant_provider_cost_is_separate_from_user_facing_usage(tmp_path):
    """
    A learner is billed one question whether it took one generation or three.
    Quintek pays for all three. Merging the two would hide the gap, and the
    gap is the contribution margin.
    """
    conn, plans = fresh(tmp_path)
    subscribe(conn, plans, "u1")
    usage = UsageService(conn, EntitlementEngine(conn, plans))
    ledger = CostLedger(conn)
    ledger.set_price(ModelPrice.from_usd_per_million("p", "m", 1.0, 1.0))

    reservation = usage.reserve("u1", 1)
    usage.commit(reservation.id, actual_units=1)
    # Three provider calls behind that one question.
    for _ in range(3):
        ledger.record(OperationCost("p", "m", user_id="u1", plan_family="pro",
                                    input_tokens=1000, output_tokens=500,
                                    questions_produced=1,
                                    questions_accepted=1, regenerations=1))

    user_units = conn.execute(
        "SELECT COALESCE(SUM(question_units),0) n FROM usage_ledger").fetchone()["n"]
    provider_calls = conn.execute("SELECT COUNT(*) n FROM cost_ledger").fetchone()["n"]
    assert user_units == 1
    assert provider_calls == 3


def test_invariant_no_ai_economics_reach_a_learner_route(tmp_path):
    conn, plans = fresh(tmp_path)
    subscribe(conn, plans, "u1")
    api = BillingAPI(conn)
    forbidden = ("provider", "cost_micro", "token", "nvidia", "openrouter", "cerebras",
                 "compute_unit", "price_in_micro")
    for path in ("/pricing", "/me/entitlements", "/me/usage", "/me/subscription"):
        _, body = api.handle("GET", path, user_id="u1")
        text = json.dumps(body).lower()
        for word in forbidden:
            assert word not in text, f"{word!r} leaked into {path}"


# ---------------------------------------------------------------------------
# 8. Configuration, not code
# ---------------------------------------------------------------------------

def test_invariant_changing_plan_configuration_needs_no_client_redeploy(tmp_path):
    """
    The business rule: displayed limits are launch configuration, not
    permanent promises. Changing one is a config edit and a reseed -- the
    client re-reads /pricing and /me/entitlements and shows the new numbers.
    """
    import copy
    conn, plans = fresh(tmp_path)
    api = BillingAPI(conn)

    before = api.handle("GET", "/pricing")[1]
    pro_before = next(f for f in before["families"] if f["family"] == "pro")
    assert pro_before["monthly_question_allowance"] == 5000

    # Recalibrate Pro, as the benchmark eventually will.
    config = json.loads(open("configs/plans.json").read())
    for spec in config["plans"]:
        if spec["family"] == "pro":
            spec["monthly_question_allowance"] = 4200
            spec["daily_question_limit"] = 250
    changed = tmp_path / "plans_v2.json"
    changed.write_text(json.dumps(config))
    plans.seed_from_config(changed)

    after = api.handle("GET", "/pricing")[1]
    pro_after = next(f for f in after["families"] if f["family"] == "pro")
    assert pro_after["monthly_question_allowance"] == 4200
    assert pro_after["daily_question_limit"] == 250


def test_invariant_an_existing_subscriber_keeps_the_terms_they_bought(tmp_path):
    """
    Versioning, not editing. Lowering an allowance must not retroactively
    shrink what someone already paid for.
    """
    conn, plans = fresh(tmp_path)
    subscribe(conn, plans, "u1")
    engine = EntitlementEngine(conn, plans)
    assert engine.snapshot("u1")["monthly_allowance"] == 5000

    config = json.loads(open("configs/plans.json").read())
    for spec in config["plans"]:
        if spec["family"] == "pro":
            spec["monthly_question_allowance"] = 1000
    changed = tmp_path / "plans_v2.json"
    changed.write_text(json.dumps(config))
    plans.seed_from_config(changed)

    assert engine.snapshot("u1")["monthly_allowance"] == 5000, (
        "an existing subscriber's allowance changed when the config changed")


def test_invariant_no_allowance_is_hardcoded_in_python():
    """
    The rule most easily broken by a quick fix, so it is checked mechanically.

    Parsed with `ast` rather than grepped, because the first version of this
    test failed on `billing/plans.py`'s own docstring, which QUOTES the
    prohibition (`if plan == "pro": allowance = 5000`) as the thing not to do.
    A line-based scan cannot tell an example of a mistake from the mistake.
    """
    import ast
    from pathlib import Path

    watched = {"monthly_allowance", "daily_limit", "session_limit", "allowance",
               "monthly_question_allowance", "daily_question_limit",
               "session_question_limit"}
    offenders = []

    for path in Path("billing").glob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            # Only a literal number assigned to one of these names is a
            # hardcoded allowance. Reading one from a plan or a row is fine.
            if not isinstance(value, ast.Constant) or not isinstance(value.value, int):
                continue
            if value.value < 10:
                continue          # 0 and small defaults are not allowances
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                name = getattr(target, "attr", None) or getattr(target, "id", None)
                if name in watched:
                    offenders.append(f"{path}:{node.lineno}: {name} = {value.value}")

    assert not offenders, ("allowances must come from configuration, not literals:\n"
                           + "\n".join(offenders))
