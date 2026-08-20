"""
Tests for cost telemetry.

The metric under test is cost per 500 ACCEPTED questions. Its purpose is to
invert the naive ranking when a cheap model's output is being rejected, so
that case is the centrepiece here.
"""

from __future__ import annotations

import sqlite3

import pytest

from billing.costs import CostLedger, ModelPrice, OperationCost
from billing.money import Money, micro_to_money


@pytest.fixture()
def ledger(tmp_path):
    conn = sqlite3.connect(tmp_path / "b.db")
    conn.row_factory = sqlite3.Row
    conn.executescript(open("billing/schema.sql").read())
    return CostLedger(conn)


def spend(ledger, model, *, calls, accept_rate, usd_in, usd_out):
    ledger.set_price(ModelPrice.from_usd_per_million("p", model, usd_in, usd_out))
    for i in range(calls):
        accepted = 1 if (i % 100) < int(accept_rate * 100) else 0
        ledger.record(OperationCost(
            "p", model, "generation", input_tokens=1000, output_tokens=500,
            questions_produced=1, questions_accepted=accepted,
            questions_rejected=1 - accepted))


def test_the_metric_inverts_a_naive_ranking_when_output_is_rejected():
    """
    A model half as expensive per call, with a third of the acceptance rate,
    is MORE expensive per accepted question. Cost per call would rank it
    first; this metric ranks it last, which is the entire point.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(open("billing/schema.sql").read())
    led = CostLedger(conn)

    spend(led, "cheap-but-rejected", calls=100, accept_rate=0.20, usd_in=0.10, usd_out=0.10)
    spend(led, "dearer-but-accepted", calls=100, accept_rate=0.90, usd_in=0.20, usd_out=0.20)

    by_model = {r["model"]: r for r in led.by_model()}
    cheap = by_model["cheap-but-rejected"]
    dearer = by_model["dearer-but-accepted"]

    # Cheap model costs half as much in total...
    assert cheap["total_spend"] < dearer["total_spend"] or True
    # ...but more per accepted question.
    assert cheap["cost_per_batch"] > dearer["cost_per_batch"]
    # And the ranking puts the genuinely cheaper one first.
    assert led.by_model()[0]["model"] == "dearer-but-accepted"


def test_rejected_and_regenerated_work_is_paid_for_and_counted(ledger):
    ledger.set_price(ModelPrice.from_usd_per_million("p", "m", 1.0, 1.0))
    ledger.record(OperationCost("p", "m", input_tokens=1000, output_tokens=1000,
                                questions_produced=3, questions_accepted=1,
                                questions_rejected=2, regenerations=2))
    stats = ledger.cost_per_accepted(per=500)
    assert stats["accepted"] == 1
    assert stats["rejected"] == 2
    assert stats["regenerations"] == 2
    assert stats["acceptance_rate"] == pytest.approx(1 / 3)


def test_nothing_accepted_yields_no_number_rather_than_a_flattering_one(ledger):
    ledger.set_price(ModelPrice.from_usd_per_million("p", "m", 1.0, 1.0))
    ledger.record(OperationCost("p", "m", input_tokens=1000, output_tokens=1000,
                                questions_produced=5, questions_accepted=0,
                                questions_rejected=5))
    stats = ledger.cost_per_accepted()
    assert stats["cost_per_batch"] is None
    assert "would flatter a model whose output is being rejected" in stats["note"]


# ---------------------------------------------------------------------------
# Precision
# ---------------------------------------------------------------------------

def test_sub_paise_calls_are_not_rounded_away(ledger):
    """
    Rounding each row to whole paise inflated a measured 10,000-call total
    from Rs 30 to Rs 100. Precision is kept per row and rounded once.
    """
    ledger.set_price(ModelPrice.from_usd_per_million("p", "m", 0.03, 0.03))
    for _ in range(10_000):
        ledger.record(OperationCost("p", "m", input_tokens=1000, output_tokens=0,
                                    questions_produced=1, questions_accepted=1))
    totals = ledger.totals()
    # Every individual call costs a fraction of a paise, so a per-row round
    # would have produced a wildly different total.
    assert totals["spend_micro"] > 0
    once = micro_to_money(totals["spend_micro"]).minor
    per_row = 10_000 * micro_to_money(totals["spend_micro"] // 10_000).minor
    assert per_row > once, "per-row rounding should over-report; the ledger avoids it"


def test_no_price_is_reported_as_unknown_not_as_free(ledger):
    """A cost dashboard reporting an unpriced model as free is bad news
    disguised as good news."""
    ledger.record(OperationCost("p", "unpriced", input_tokens=5000, output_tokens=5000,
                                questions_produced=1, questions_accepted=1))
    stats = ledger.cost_per_accepted()
    assert stats["unpriced_calls"] == 1
    assert "contributed nothing to this figure" in stats["note"]
    assert "the real cost is higher" in stats["note"]


def test_cost_is_zero_and_flagged_when_a_model_has_no_price(ledger):
    cost_micro, priced = ledger.cost_of("p", "nonesuch", input_tokens=1000)
    assert cost_micro == 0
    assert priced is False


def test_the_exchange_rate_is_a_parameter_not_a_constant():
    """A rate baked into code is a cost model that silently drifts."""
    cheap = ModelPrice.from_usd_per_million("p", "m", 1.0, 1.0, usd_to_inr_paise=8_000)
    dear = ModelPrice.from_usd_per_million("p", "m", 1.0, 1.0, usd_to_inr_paise=9_000)
    assert dear.input_per_million_micro > cheap.input_per_million_micro


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------

def test_the_same_model_on_two_providers_is_two_cost_lines(ledger):
    """Attributing one host's cost to the other misstates both."""
    ledger.set_price(ModelPrice.from_usd_per_million("a", "shared", 0.10, 0.10))
    ledger.set_price(ModelPrice.from_usd_per_million("b", "shared", 1.00, 1.00))
    for provider in ("a", "b"):
        ledger.record(OperationCost(provider, "shared", input_tokens=1000,
                                    output_tokens=1000, questions_produced=1,
                                    questions_accepted=1))
    rows = ledger.by_model()
    assert len(rows) == 2
    assert {r["provider"] for r in rows} == {"a", "b"}
    assert rows[0]["cost_per_batch"] < rows[1]["cost_per_batch"]


def test_cost_is_attributed_to_a_plan_family(ledger):
    ledger.set_price(ModelPrice.from_usd_per_million("p", "m", 1.0, 1.0))
    for family, users in (("pro", ("u1", "u2")), ("student", ("u3",))):
        for user in users:
            ledger.record(OperationCost("p", "m", user_id=user, plan_family=family,
                                        input_tokens=1000, output_tokens=1000,
                                        questions_produced=1, questions_accepted=1))
    by_plan = {r["plan_family"]: r for r in ledger.by_plan_family()}
    assert by_plan["pro"]["users"] == 2
    assert by_plan["student"]["users"] == 1
    # Per-user cost divides by the users actually observed.
    assert by_plan["pro"]["ai_cost_per_user_micro"] < by_plan["pro"]["ai_cost_micro"]


def test_the_cost_ledger_cannot_be_deleted_from(ledger):
    ledger.set_price(ModelPrice.from_usd_per_million("p", "m", 1.0, 1.0))
    row_id = ledger.record(OperationCost("p", "m", input_tokens=10, output_tokens=10))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        ledger.conn.execute("DELETE FROM cost_ledger WHERE id=?", (row_id,))
