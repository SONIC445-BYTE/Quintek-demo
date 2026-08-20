"""
The compute-budget calculator.

The first test here exists because of a real bug: the USD->INR conversion was
written a second time, by hand, with a stray factor of a hundred. Every figure
downstream stayed internally consistent, so nothing looked wrong -- the report
simply said Quintek could afford six billion questions a month. A cost model
can be wrong by two orders of magnitude and still look tidy.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import tools_compute_budget as tool
from billing.money import MICRO, token_cost_micro

PROFILE = tool.load_profile()


# --------------------------------------------------------------- conversion

def test_a_dollar_per_million_tokens_is_eighty_five_rupees_per_million() -> None:
    per_million = tool.price_per_million_micro(1.0, 8_500)
    cost = token_cost_micro(1_000_000, per_million)
    assert cost == 85 * 100 * MICRO, "one million tokens at $1/M must cost ₹85"


def test_conversion_matches_the_billing_module_exactly() -> None:
    """One conversion in the codebase, not two that drift apart."""
    from billing.costs import ModelPrice
    for usd in (0.0, 0.01, 0.019, 0.3, 2.5):
        expected = ModelPrice.from_usd_per_million(
            "p", "m", usd, usd, usd_to_inr_paise=8_500).input_per_million_micro
        assert tool.price_per_million_micro(usd, 8_500) == expected


def test_a_free_model_costs_zero_not_a_rounding_artefact() -> None:
    assert tool.price_per_million_micro(0.0, 8_500) == 0


# --------------------------------------------------------------- unit cost

def _profile(**overrides) -> dict:
    p = json.loads(json.dumps(PROFILE))
    p.update(overrides)
    return p


def test_cost_per_accepted_is_the_hand_calculation() -> None:
    p = _profile()
    p["fx"] = {"usd_to_inr_paise": 8_500}
    p["token_profile"] = {"generation": {"input_tokens": 1_000_000,
                                         "output_tokens": 0},
                          "validation": {"input_tokens": 0, "output_tokens": 0}}
    p["acceptance"] = {"accepted_per_100_produced": 100}
    # A million input tokens at $1/M is ₹85, and at 100% acceptance that is the
    # cost of the one accepted question it produced.
    assert tool.cost_per_accepted_micro(p, 1.0, 0.0, 0.0, 0.0) == 85 * 100 * MICRO


def test_rejected_work_is_charged_to_the_accepted_questions() -> None:
    p = _profile()
    p["fx"] = {"usd_to_inr_paise": 8_500}
    p["token_profile"] = {"generation": {"input_tokens": 1_000_000, "output_tokens": 0},
                          "validation": {"input_tokens": 0, "output_tokens": 0}}

    p["acceptance"] = {"accepted_per_100_produced": 100}
    full = tool.cost_per_accepted_micro(p, 1.0, 0.0, 0.0, 0.0)
    p["acceptance"] = {"accepted_per_100_produced": 50}
    half = tool.cost_per_accepted_micro(p, 1.0, 0.0, 0.0, 0.0)
    assert half == full * 2, "half the questions accepted, twice the cost each"


def test_validation_tokens_are_counted_not_forgotten() -> None:
    p = _profile()
    p["fx"] = {"usd_to_inr_paise": 8_500}
    p["acceptance"] = {"accepted_per_100_produced": 100}
    p["token_profile"] = {"generation": {"input_tokens": 100, "output_tokens": 0},
                          "validation": {"input_tokens": 0, "output_tokens": 0}}
    without = tool.cost_per_accepted_micro(p, 1.0, 1.0, 1.0, 1.0)
    p["token_profile"]["validation"] = {"input_tokens": 100, "output_tokens": 0}
    with_validation = tool.cost_per_accepted_micro(p, 1.0, 1.0, 1.0, 1.0)
    assert with_validation == without * 2


def test_zero_acceptance_does_not_divide_by_zero() -> None:
    p = _profile()
    p["acceptance"] = {"accepted_per_100_produced": 0}
    assert tool.cost_per_accepted_micro(p, 1.0, 1.0, 1.0, 1.0) == 0


# --------------------------------------------------------------- pairings

@pytest.fixture()
def shortlist(tmp_path) -> Path:
    path = tmp_path / "shortlists.json"
    path.write_text(json.dumps({
        "generation": [
            {"model_id": "free/gen", "price_in_per_m": 0.0, "price_out_per_m": 0.0},
            {"model_id": "paid/gen", "price_in_per_m": 0.5, "price_out_per_m": 1.0},
        ],
        "validation": [
            {"model_id": "free/val", "price_in_per_m": 0.0, "price_out_per_m": 0.0},
            {"model_id": "paid/gen", "price_in_per_m": 0.5, "price_out_per_m": 1.0},
            {"model_id": "paid/val", "price_in_per_m": 0.9, "price_out_per_m": 2.0},
        ],
    }))
    return path


def test_a_model_never_validates_itself(shortlist) -> None:
    rows = tool.candidate_costs(PROFILE, shortlist, paid_only=True)
    for row in rows:
        assert row["generator"] != row["validator"]
    # paid/gen is the cheapest paid validator, but it generated -- so paid/val.
    paid = next(r for r in rows if r["generator"] == "paid/gen")
    assert paid["validator"] == "paid/val"


def test_paid_only_excludes_free_models_from_both_sides(shortlist) -> None:
    rows = tool.candidate_costs(PROFILE, shortlist, paid_only=True)
    names = {r["generator"] for r in rows} | {r["validator"] for r in rows}
    assert not any(n.startswith("free/") for n in names), (
        "a 'paid' pairing validated by a free tier depends on that free tier"
        " for every question the product sells")


def test_without_paid_only_the_free_models_are_included(shortlist) -> None:
    rows = tool.candidate_costs(PROFILE, shortlist)
    assert any(r["generator"] == "free/gen" for r in rows)


def test_pairings_are_sorted_cheapest_first(shortlist) -> None:
    rows = tool.candidate_costs(PROFILE, shortlist)
    costs = [r["cost_per_accepted_micro"] for r in rows]
    assert costs == sorted(costs)


def test_a_missing_shortlist_is_empty_not_an_exception(tmp_path) -> None:
    assert tool.candidate_costs(PROFILE, tmp_path / "absent.json") == []


# --------------------------------------------------------------- formatting

def test_sub_paise_amounts_keep_their_precision() -> None:
    # 0.291 paise. `Money.format` would render this ₹0.00 and make an eight-fold
    # difference between two models invisible.
    assert tool.fine(291_000) == "0.291p"
    assert tool.fine(0) == "0.000p"


def test_amounts_of_a_rupee_or_more_use_normal_formatting() -> None:
    assert tool.fine(100 * MICRO) == "₹1.00"
    assert tool.fine(49_900 * MICRO) == "₹499.00"


def test_two_models_an_order_of_magnitude_apart_do_not_format_the_same() -> None:
    assert tool.fine(29_000) != tool.fine(291_000)


# --------------------------------------------------------------- plans

def test_the_free_plan_has_no_compute_budget_of_its_own() -> None:
    ids = {row["plan_id"] for row in tool.plan_table(PROFILE)}
    assert ids, "no plans seeded"
    assert not any("free" in plan_id for plan_id in ids)


def test_every_shipped_paid_plan_gets_a_per_question_ceiling() -> None:
    for row in tool.plan_table(PROFILE):
        assert row["max_cost_per_accepted_micro"] is not None, row["plan_id"]
        assert row["max_cost_per_accepted_micro"] > 0, row["plan_id"]


def test_a_bigger_allowance_at_the_same_price_lowers_the_per_question_ceiling() -> None:
    rows = {r["plan_id"]: r for r in tool.plan_table(PROFILE)}
    pro = rows["pro_monthly_v1"]
    power = rows["power_monthly_v1"]
    # Power costs more but sells twice as many questions, so each one may cost
    # LESS. A plan that sells more volume is not automatically more affordable.
    assert power["monthly_allowance"] > pro["monthly_allowance"]
    assert power["max_cost_per_accepted_micro"] < pro["max_cost_per_accepted_micro"]


# --------------------------------------------------------------- portfolio

def test_portfolio_ceiling_never_exceeds_revenue() -> None:
    result = tool.portfolio(PROFILE, 100_000, paying_users=250)
    assert result["budget"]["ceiling_minor"] < 100_000 * 100


def test_portfolio_counts_free_users_against_the_same_budget() -> None:
    with_free = tool.portfolio(PROFILE, 100_000, paying_users=250)
    assert with_free["assumed_free_questions"] > 0
    line = [d for d in with_free["budget"]["deductions"]
            if d["name"] == "free_tier_commitment"]
    assert line and line[0]["amount_minor"] > 0


def test_portfolio_router_ceiling_is_per_committed_question() -> None:
    result = tool.portfolio(PROFILE, 100_000, paying_users=250, allowance_per_user=5_000)
    assert result["committed_questions"] == 250 * 5_000 + result["assumed_free_questions"]
    allowed = result["verdict"]["allowed_per_question_micro"]
    assert allowed == (result["budget"]["ceiling_minor"] * MICRO
                       // result["committed_questions"])


def test_selling_more_questions_on_the_same_revenue_tightens_the_ceiling() -> None:
    loose = tool.portfolio(PROFILE, 100_000, paying_users=250, allowance_per_user=1_000)
    tight = tool.portfolio(PROFILE, 100_000, paying_users=250, allowance_per_user=20_000)
    assert (tight["verdict"]["allowed_per_question_micro"]
            < loose["verdict"]["allowed_per_question_micro"])


# --------------------------------------------------------------- the report

def test_the_report_labels_assumptions_as_assumptions() -> None:
    out = subprocess.run(
        [sys.executable, "tools_compute_budget.py", "--revenue", "100000"],
        capture_output=True, text=True, check=True).stdout
    assert "MEASURED" in out and "ASSUMED" in out
    # The acceptance rate has never been measured in production; if that ever
    # changes, this test should be updated deliberately rather than drift.
    assert PROFILE["acceptance"]["measured"] is False
    assert "ASSUMED   80" in out


def test_the_json_mode_is_machine_readable() -> None:
    out = subprocess.run(
        [sys.executable, "tools_compute_budget.py", "--json", "--revenue", "50000"],
        capture_output=True, text=True, check=True).stdout
    payload = json.loads(out)
    assert payload["plans"] and payload["paid_candidates"]
    assert payload["portfolio"]["verdict"]["verdict"] in {"OK", "TIGHT", "OVER", "UNKNOWN"}


def test_the_real_profile_and_shortlist_files_are_usable() -> None:
    assert PROFILE["token_profile"]["measured"] is True
    assert tool.candidate_costs(PROFILE, paid_only=True), (
        "no priced candidate pairing -- the budget would have nothing to check")
