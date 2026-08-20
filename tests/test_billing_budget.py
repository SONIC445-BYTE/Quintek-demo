"""
The compute budget: given ₹X of revenue, how much AI can Quintek buy?

The tests that matter here are not "does the arithmetic add up" -- they are the
ones that pin down what the module refuses to do: report a ceiling as safe when
the liability behind it was never measured, call an uncosted model affordable,
or divide by an expected volume of zero and report an infinite allowance.
"""

from __future__ import annotations

import ast
import sqlite3

import pytest

from billing.budget import (OK, OVER, TIGHT, UNKNOWN, BudgetPolicy, ComputeBudget,
                            BudgetService, compute_budget, plan_budget, runway_days)
from billing.economics import FeeModel
from billing.money import MICRO, Money
from billing.plans import Plan, PlanStore

SCHEMA = open("billing/schema.sql").read()
RS = lambda major: Money(major * 100)  # noqa: E731  -- rupees, as minor units


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "b.db", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    c.execute("PRAGMA foreign_keys = ON")
    return c


def _plan(conn, plan_id, family, interval, price_minor, allowance):
    conn.execute(
        "INSERT INTO plans (id, family, name, billing_interval, price_minor,"
        " currency, monthly_question_allowance, daily_question_limit,"
        " session_question_limit, rollover_percent, created_at, updated_at)"
        " VALUES (?,?,?,?,?,'INR',?,?,?,50,'2026-01-01','2026-01-01')",
        (plan_id, family, family.title(), interval, price_minor, allowance,
         max(1, allowance // 10), max(1, allowance // 50)))


def _sub(conn, sub_id, user_id, plan_id, interval="monthly", status="ACTIVE"):
    conn.execute(
        "INSERT INTO subscriptions (id, user_id, plan_id, billing_interval,"
        " status, current_period_start, current_period_end, created_at, updated_at)"
        " VALUES (?,?,?,?,?, '2026-08-01','2026-09-01','2026-08-01','2026-08-01')",
        (sub_id, user_id, plan_id, interval, status))


# ---------------------------------------------------------------- the waterfall

def test_gst_is_extracted_from_an_inclusive_price_not_added() -> None:
    # ₹499 inclusive of 18% => ₹76.12 tax, ₹422.88 revenue. Not ₹89.82.
    budget = compute_budget(Money(49_900), BudgetPolicy(
        fees=FeeModel(percent_bps=0, gst_on_fee_bps=0),
        refund_reserve_bps=0, target_contribution_bps=0),
        paid_liability=Money.zero(), free_commitment=Money.zero())
    gst = next(d for d in budget.deductions if d.name == "gst_collected")
    assert gst.amount.minor == 7_612            # rounded up, in Quintek's disfavour
    assert budget.ceiling.minor == 49_900 - 7_612


def test_gateway_fee_is_charged_on_the_gross_including_gst() -> None:
    budget = compute_budget(Money(49_900), BudgetPolicy(
        refund_reserve_bps=0, target_contribution_bps=0),
        paid_liability=Money.zero(), free_commitment=Money.zero())
    fee = next(d for d in budget.deductions if d.name == "gateway_fees")
    # 2% of ₹499 = ₹9.98, +18% GST on the fee = ₹11.78. Charged on 499, not 422.88.
    assert fee.basis == "gross"
    assert fee.amount.minor == 998 + 180


def test_the_waterfall_balances_exactly() -> None:
    budget = compute_budget(RS(100_000), BudgetPolicy(fixed_costs_minor=500_000),
                            paid_liability=RS(5_000), free_commitment=RS(2_000))
    total = sum(d.amount.minor for d in budget.deductions)
    assert total + budget.ceiling.minor == budget.gross.minor
    assert budget.solvent


def test_every_deduction_rounds_up() -> None:
    """
    A deduction rounded down is money Quintek believes it has and does not.
    One paise per subscription per month is invisible and permanent.
    """
    # ₹1.00: every percentage deduction here is a fraction of a paise, and
    # every one of them must still cost at least a whole paise.
    budget = compute_budget(Money(100), BudgetPolicy(target_contribution_bps=1),
                            paid_liability=Money.zero(), free_commitment=Money.zero())
    for deduction in budget.deductions:
        assert deduction.amount.minor >= 1, deduction.name
    # And specifically: 18% of ₹1.00 inclusive is 15.25 paise, taken as 16.
    gst = next(d for d in budget.deductions if d.name == "gst_collected")
    assert gst.amount.minor == 16


def test_more_expensive_policy_never_raises_the_ceiling() -> None:
    cheap = compute_budget(RS(100_000), BudgetPolicy(target_contribution_bps=1_000),
                           paid_liability=Money.zero(), free_commitment=Money.zero())
    dear = compute_budget(RS(100_000), BudgetPolicy(target_contribution_bps=4_000),
                          paid_liability=Money.zero(), free_commitment=Money.zero())
    assert dear.ceiling <= cheap.ceiling


def test_target_contribution_takes_the_larger_of_percentage_and_floor() -> None:
    small = compute_budget(RS(1_000), BudgetPolicy(
        target_contribution_bps=2_500, target_contribution_minor=90_000),
        paid_liability=Money.zero(), free_commitment=Money.zero())
    line = next(d for d in small.deductions if d.name == "target_contribution")
    assert line.amount.minor == 90_000          # the floor beat 25% of ₹1,000
    assert line.basis == "absolute"


def test_annual_cash_is_not_authorised_as_a_month_of_compute(conn) -> None:
    """
    The single most dangerous mistake in this calculation. An annual sale is
    twelve months of obligation, so it may fund one month of compute, not
    twelve.
    """
    _plan(conn, "pro_annual", "pro", "annual", 4_99_900, 3_000)
    _sub(conn, "s1", "u1", "pro_annual", interval="annual")
    service = BudgetService(conn)
    assert service.recognised_revenue().minor == 4_99_900 // 12


# ---------------------------------------------------------------- insolvency

def test_deductions_exceeding_revenue_report_a_shortfall_not_a_negative_ceiling() -> None:
    budget = compute_budget(RS(1_000), BudgetPolicy(fixed_costs_minor=RS(5_000).minor),
                            paid_liability=Money.zero(), free_commitment=Money.zero())
    assert budget.ceiling.minor == 0
    assert budget.shortfall.minor > 0
    assert not budget.solvent
    assert any("no compute budget" in w for w in budget.warnings)


def test_a_shortfall_never_produces_a_spendable_per_question_allowance() -> None:
    budget = compute_budget(RS(1_000), BudgetPolicy(fixed_costs_minor=RS(5_000).minor),
                            paid_liability=Money.zero(), free_commitment=Money.zero())
    assert budget.per_question_ceiling_micro(1_000) == 0


# ---------------------------------------------------------------- honesty

def test_unmeasured_liability_is_warned_about_and_not_treated_as_zero() -> None:
    budget = compute_budget(RS(100_000), BudgetPolicy(),
                            paid_liability=None, free_commitment=None)
    assert budget.liability_measured is False
    assert any("UNMEASURED" in w for w in budget.warnings)
    assert not any(d.name == "outstanding_paid_allowance" for d in budget.deductions)


def test_measured_zero_liability_is_not_the_same_as_unmeasured() -> None:
    measured = compute_budget(RS(100_000), BudgetPolicy(),
                              paid_liability=Money.zero(), free_commitment=Money.zero())
    assert measured.liability_measured is True
    assert measured.warnings == ()


def test_an_uncosted_model_is_unknown_not_affordable() -> None:
    budget = compute_budget(RS(100_000), BudgetPolicy(),
                            paid_liability=Money.zero(), free_commitment=Money.zero())
    verdict = budget.verdict(None, 10_000)
    assert verdict["verdict"] == UNKNOWN
    assert verdict["verdict"] != OK
    assert "no cost has been measured" in verdict["reason"]


def test_zero_expected_volume_reports_no_allowance_rather_than_an_infinite_one() -> None:
    budget = compute_budget(RS(100_000), BudgetPolicy(),
                            paid_liability=Money.zero(), free_commitment=Money.zero())
    assert budget.per_question_ceiling_micro(0) is None
    assert budget.verdict(5_000, 0)["verdict"] == UNKNOWN


# ---------------------------------------------------------------- the verdict

@pytest.mark.parametrize("fraction,expected", [
    (0.10, OK),        # costs a tenth of what is allowed
    (0.50, OK),
    (0.95, TIGHT),     # inside the 15% margin
    (1.00, TIGHT),
    (1.20, OVER),
])
def test_verdict_bands(fraction, expected) -> None:
    budget = compute_budget(RS(100_000), BudgetPolicy(),
                            paid_liability=Money.zero(), free_commitment=Money.zero())
    allowed = budget.per_question_ceiling_micro(10_000)
    verdict = budget.verdict(int(allowed * fraction), 10_000)
    assert verdict["verdict"] == expected, verdict


def test_headroom_is_reported_alongside_the_verdict() -> None:
    budget = compute_budget(RS(100_000), BudgetPolicy(),
                            paid_liability=Money.zero(), free_commitment=Money.zero())
    allowed = budget.per_question_ceiling_micro(10_000)
    verdict = budget.verdict(allowed // 2, 10_000)
    assert 4_900 <= verdict["headroom_bps"] <= 5_000


# ---------------------------------------------------------------- runway

def test_runway_is_none_when_nothing_is_burning() -> None:
    assert runway_days(RS(10_000), Money.zero()) is None


def test_runway_is_zero_on_an_empty_balance() -> None:
    assert runway_days(Money.zero(), RS(100)) == 0


def test_runway_counts_whole_days_only() -> None:
    assert runway_days(RS(1_000), RS(300)) == 3     # not 3.33


# ---------------------------------------------------------------- per plan

def test_plan_budget_puts_an_annual_seat_on_a_monthly_basis() -> None:
    annual = Plan(id="pro_a", family="pro", name="Pro", billing_interval="annual",
                  price_minor=4_99_900, currency="INR",
                  monthly_question_allowance=3_000, daily_question_limit=300,
                  session_question_limit=100, rollover_percent=50, version=1,
                  active=True, sort_order=3)
    result = plan_budget(annual)
    assert result.monthly_revenue.minor == 4_99_900 // 12


def test_plan_budget_yields_a_per_question_ceiling_the_router_can_use() -> None:
    monthly = Plan(id="pro_m", family="pro", name="Pro", billing_interval="monthly",
                   price_minor=49_900, currency="INR",
                   monthly_question_allowance=3_000, daily_question_limit=300,
                   session_question_limit=100, rollover_percent=50, version=1,
                   active=True, sort_order=3)
    result = plan_budget(monthly)
    assert result.per_question_micro is not None
    # Sanity: a ₹499 plan with 3,000 questions cannot afford ₹0.17 per question.
    assert result.per_question_micro < 17 * MICRO
    assert result.ceiling < Money(49_900)


def test_a_plan_with_no_allowance_reports_no_per_question_ceiling() -> None:
    broken = Plan(id="x", family="x", name="X", billing_interval="monthly",
                  price_minor=49_900, currency="INR", monthly_question_allowance=0,
                  daily_question_limit=0, session_question_limit=0,
                  rollover_percent=0, version=1, active=True, sort_order=0)
    assert plan_budget(broken).per_question_micro is None


# ---------------------------------------------------------------- the service

def test_unconsumed_units_separate_paid_obligation_from_free_commitment(conn) -> None:
    _plan(conn, "free", "free", "none", 0, 100)
    _plan(conn, "pro", "pro", "monthly", 49_900, 3_000)
    _sub(conn, "s1", "payer", "pro")
    for user, plan, allowance in (("payer", "pro", 3_000), ("guest", "free", 100)):
        conn.execute(
            "INSERT INTO entitlements (id, user_id, plan_id, monthly_allowance,"
            " daily_limit, session_limit, effective_from, created_at)"
            " VALUES (?,?,?,?,?,?, '2026-01-01','2026-01-01')",
            (f"e_{user}", user, plan, allowance, allowance // 10, allowance // 50))

    units = BudgetService(conn).unconsumed_units()
    assert units["paid_units"] == 3_000
    assert units["free_units"] == 100


def test_consumption_reduces_the_outstanding_obligation(conn) -> None:
    from billing.entitlements import period_start_for
    _plan(conn, "pro", "pro", "monthly", 49_900, 3_000)
    _sub(conn, "s1", "payer", "pro")
    conn.execute(
        "INSERT INTO entitlements (id, user_id, plan_id, monthly_allowance,"
        " daily_limit, session_limit, effective_from, created_at)"
        " VALUES ('e1','payer','pro',3000,300,100,'2026-01-01','2026-01-01')")
    conn.execute(
        "INSERT INTO usage_ledger (id, user_id, question_units, usage_date,"
        " period_start, created_at) VALUES ('u1','payer',1200,'2026-08-20',?,?)",
        (period_start_for(), "2026-08-20T00:00:00Z"))
    assert BudgetService(conn).unconsumed_units()["paid_units"] == 1_800


def test_service_reports_unknown_when_nothing_has_been_costed(conn) -> None:
    _plan(conn, "pro", "pro", "monthly", 49_900, 3_000)
    _sub(conn, "s1", "payer", "pro")
    report = BudgetService(conn).report()
    assert report["measured_cost_per_accepted_micro"] is None
    assert report["verdict"]["verdict"] == UNKNOWN
    assert report["budget"]["liability_measured"] is False


def test_service_deducts_a_measured_liability(conn) -> None:
    _plan(conn, "pro", "pro", "monthly", 49_900, 3_000)
    _sub(conn, "s1", "payer", "pro")
    conn.execute(
        "INSERT INTO entitlements (id, user_id, plan_id, monthly_allowance,"
        " daily_limit, session_limit, effective_from, created_at)"
        " VALUES ('e1','payer','pro',3000,300,100,'2026-01-01','2026-01-01')")
    conn.execute(
        "INSERT INTO cost_ledger (id, operation, provider, model, cost_micro,"
        " questions_produced, questions_accepted, created_at)"
        " VALUES ('c1','generation','nvidia','m', 20000000, 120, 100,"
        " '2026-08-20T00:00:00Z')")
    service = BudgetService(conn)
    assert service.unit_cost_micro() == 200_000        # ₹0.20 per accepted question
    budget = service.budget()
    assert budget.liability_measured is True
    line = next(d for d in budget.deductions if d.name == "outstanding_paid_allowance")
    assert line.amount.minor == (3_000 * 200_000 + MICRO - 1) // MICRO


def test_report_lists_a_per_question_ceiling_for_every_paid_plan(conn) -> None:
    _plan(conn, "free", "free", "none", 0, 100)
    _plan(conn, "pro", "pro", "monthly", 49_900, 3_000)
    report = BudgetService(conn).report()
    families = {p["plan_id"] for p in report["plans"]}
    assert families == {"pro"}, "the free plan has no revenue to budget against"
    assert report["plans"][0]["max_cost_per_accepted_micro"] is not None


# ---------------------------------------------------------------- no floats

def test_no_floating_point_money_in_the_module() -> None:
    """
    Parsed rather than grepped: a docstring mentioning 0.5 must not fail, and a
    real float literal must not pass because it happens to sit in a comment.
    """
    tree = ast.parse(open("billing/budget.py").read())
    floats = [node for node in ast.walk(tree)
              if isinstance(node, ast.Constant) and isinstance(node.value, float)]
    assert floats == [], f"float literals in budget.py: {[f.value for f in floats]}"


def test_no_division_operator_that_would_produce_a_float() -> None:
    tree = ast.parse(open("billing/budget.py").read())
    divisions = [n for n in ast.walk(tree) if isinstance(n, ast.BinOp)
                 and isinstance(n.op, ast.Div)]
    assert divisions == [], "use // or Money.scale, never /"
