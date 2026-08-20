"""
The join between the two financial systems.

Customers pay Quintek through the gateway; Quintek separately funds provider
accounts. Nothing connects the two automatically, and until the recorder
existed nothing connected them at all: the engine made model calls and
`cost_ledger` stayed empty, so every economics figure read "unmeasured" while
real money was being spent on inference.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from billing.costs import CostLedger
from billing.db import connect
from billing.money import MICRO
from billing.recorder import CostRecorder, load_prices
from student.ai import AIEngine
from student.db import Database


@pytest.fixture()
def recorder(tmp_path):
    return CostRecorder(connect(tmp_path / "b.db"))


# ------------------------------------------------------------------- prices

def test_the_shipped_price_file_loads(recorder) -> None:
    assert recorder.priced_models > 0, "no model prices are configured"


def test_prices_come_from_configuration_not_code(tmp_path) -> None:
    """
    A provider changes its prices without asking. A price baked into a module
    is a cost model that drifts from what is actually being charged.
    """
    path = tmp_path / "prices.json"
    path.write_text(json.dumps({"usd_to_inr_paise": 8_500, "prices": [
        {"provider": "p", "model": "m", "usd_in_per_million": 1.0,
         "usd_out_per_million": 2.0}]}))
    ledger = CostLedger(sqlite3.connect(":memory:"))
    assert load_prices(ledger, path) == 1
    price = ledger.price_for("p", "m")
    assert price.input_per_million_micro == 85 * 100 * MICRO


def test_a_missing_price_file_is_zero_prices_not_a_crash(tmp_path) -> None:
    assert load_prices(CostLedger(sqlite3.connect(":memory:")),
                       tmp_path / "absent.json") == 0


# ------------------------------------------------------------------ recording

def test_a_call_is_costed_from_its_tokens(recorder) -> None:
    recorder({"provider": "openrouter", "model": "inclusionai/ling-2.6-flash",
              "operation": "generation", "batch_id": "b1",
              "input_tokens": 524, "output_tokens": 137})
    row = recorder.conn.execute(
        "SELECT * FROM cost_ledger WHERE batch_id='b1'").fetchone()
    assert row["input_tokens"] == 524
    assert row["cost_micro"] > 0
    assert row["price_in_micro"] is not None


def test_an_unpriced_model_is_recorded_as_unpriced_not_as_free(recorder) -> None:
    """
    A dashboard reporting an unpriced model as ₹0.00 is worse than one
    reporting it as unknown, because free looks like good news.
    """
    recorder({"provider": "nobody", "model": "mystery", "operation": "generation",
              "batch_id": "b2", "input_tokens": 500, "output_tokens": 100})
    row = recorder.conn.execute(
        "SELECT * FROM cost_ledger WHERE batch_id='b2'").fetchone()
    assert row["price_in_micro"] is None
    assert recorder.unpriced_since() == 1

    recorder.record_outcome("b2", produced=1, accepted=1)
    report = recorder.ledger.cost_per_accepted(batch_id="b2")
    assert report["unpriced_calls"] == 1
    assert "real cost is higher" in report["note"]


def test_no_question_counts_are_guessed_at_call_time(recorder) -> None:
    """
    At call time nobody knows how many questions survived parsing, let alone
    validation. A guess here would make every cost-per-accepted figure
    downstream a guess too.
    """
    recorder({"provider": "openrouter", "model": "inclusionai/ling-2.6-flash",
              "operation": "generation", "batch_id": "b3",
              "input_tokens": 100, "output_tokens": 50})
    row = recorder.conn.execute(
        "SELECT * FROM cost_ledger WHERE batch_id='b3'").fetchone()
    assert row["questions_produced"] == 0
    assert row["questions_accepted"] == 0


def test_a_broken_ledger_never_breaks_the_call_it_measures(tmp_path) -> None:
    """
    A learner losing their generated questions because a cost row would not
    insert is a worse outcome than an incomplete ledger.
    """
    rec = CostRecorder(connect(tmp_path / "b.db"))
    rec.conn.close()
    assert rec({"provider": "p", "model": "m", "input_tokens": 1}) is None
    assert rec.record_outcome("b", produced=1, accepted=1) is None


# ---------------------------------------------------------------- attribution

def test_the_outcome_is_a_separate_row_not_an_edit(recorder) -> None:
    """
    The ledger's value is that it is append-only. Spend and yield are two
    observations made at two different times and they look like it.
    """
    recorder({"provider": "openrouter", "model": "inclusionai/ling-2.6-flash",
              "operation": "generation", "batch_id": "b4",
              "input_tokens": 524, "output_tokens": 137})
    recorder.record_outcome("b4", produced=5, accepted=4, rejected=1)

    rows = recorder.conn.execute(
        "SELECT operation, cost_micro, questions_accepted FROM cost_ledger"
        " WHERE batch_id='b4' ORDER BY operation").fetchall()
    assert len(rows) == 2
    outcome = [r for r in rows if r["operation"] == "outcome"][0]
    assert outcome["cost_micro"] == 0 and outcome["questions_accepted"] == 4


def test_a_settlement_row_is_not_counted_as_an_unpriced_call(recorder) -> None:
    """
    It carries no tokens and nobody could have priced it. Counting it would put
    a false caveat on a correct figure.
    """
    recorder({"provider": "openrouter", "model": "inclusionai/ling-2.6-flash",
              "operation": "generation", "batch_id": "b5",
              "input_tokens": 524, "output_tokens": 137})
    recorder.record_outcome("b5", produced=5, accepted=4)
    report = recorder.ledger.cost_per_accepted(batch_id="b5")
    assert report["unpriced_calls"] == 0
    assert report["note"] == ""


def test_an_outcome_with_no_batch_is_refused(recorder) -> None:
    """Unattributable spend is worse than unrecorded spend: it pollutes every
    other batch's figure."""
    assert recorder.record_outcome("", produced=5, accepted=5) is None


def test_generation_and_validation_both_land_on_the_same_batch(recorder) -> None:
    """
    Cost per ACCEPTED covers both halves. Costing generation alone understates
    it by whatever validation costs, which on a cheap generator is most of the
    bill.
    """
    for operation, model in (("generation", "inclusionai/ling-2.6-flash"),
                             ("validation", "inclusionai/ling-3.0-flash")):
        recorder({"provider": "openrouter", "model": model, "operation": operation,
                  "batch_id": "b6", "input_tokens": 500, "output_tokens": 100})
    recorder.record_outcome("b6", produced=1, accepted=1)

    report = recorder.ledger.cost_per_accepted(batch_id="b6")
    generation_only = recorder.conn.execute(
        "SELECT cost_micro FROM cost_ledger WHERE batch_id='b6'"
        " AND operation='generation'").fetchone()["cost_micro"]
    assert report["cost_per_batch"] > generation_only * 500


# ------------------------------------------------------------------ the engine

class FakeResponse:
    ok = True
    raw_output = "{}"
    parsed: dict = {}
    error = ""
    attempts = 1
    latency_ms = 12.0
    input_tokens = 524
    output_tokens = 137


class FakeProvider:
    name = "openrouter"
    model = "inclusionai/ling-2.6-flash"
    model_version = "1"

    def generate(self, request):
        return FakeResponse()


def engine_with(tmp_path, sink):
    db = Database(tmp_path / "student.db")
    return AIEngine(db, provider_factory=lambda c: FakeProvider(),
                    development_candidate="cand_x", cost_sink=sink)


def test_the_engine_reports_every_call_to_the_sink(tmp_path) -> None:
    seen: list[dict] = []
    engine = engine_with(tmp_path, seen.append)
    engine.call("question_generation", "prompt")
    assert len(seen) == 1
    assert seen[0]["provider"] == "openrouter"
    assert seen[0]["input_tokens"] == 524
    assert seen[0]["operation"] == "question_generation"


def test_without_a_sink_the_engine_behaves_exactly_as_before(tmp_path) -> None:
    engine = engine_with(tmp_path, None)
    result = engine.call("question_generation", "prompt")
    assert result.model == "inclusionai/ling-2.6-flash"


def test_a_sink_that_raises_does_not_break_generation(tmp_path) -> None:
    def hostile(_call):
        raise RuntimeError("the ledger is on fire")

    engine = engine_with(tmp_path, hostile)
    assert engine.call("question_generation", "prompt").provider == "openrouter"


def test_attribution_names_the_batch_and_the_learner(tmp_path) -> None:
    seen: list[dict] = []
    engine = engine_with(tmp_path, seen.append)
    with engine.attribute(batch_id="batch_9", user_id="u1", plan_family="pro"):
        engine.call("question_generation", "prompt")
    assert seen[0]["batch_id"] == "batch_9"
    assert seen[0]["user_id"] == "u1"
    assert seen[0]["plan_family"] == "pro"


def test_attribution_is_restored_even_when_the_batch_raises(tmp_path) -> None:
    """
    Otherwise a failed generation files the NEXT learner's costs under the
    previous one.
    """
    seen: list[dict] = []
    engine = engine_with(tmp_path, seen.append)
    with pytest.raises(RuntimeError):
        with engine.attribute(batch_id="batch_bad", user_id="u_bad"):
            raise RuntimeError("boom")
    engine.call("question_generation", "prompt")
    assert seen[0]["batch_id"] == ""
    assert seen[0]["user_id"] == ""


def test_unattributed_calls_are_recorded_with_nobody_rather_than_a_guess(tmp_path) -> None:
    seen: list[dict] = []
    engine_with(tmp_path, seen.append).call("question_generation", "prompt")
    assert seen[0]["user_id"] == "" and seen[0]["batch_id"] == ""


def test_the_engine_and_the_recorder_fit_together(tmp_path) -> None:
    """The end-to-end claim: a real call produces a costed ledger row."""
    rec = CostRecorder(connect(tmp_path / "b.db"))
    engine = engine_with(tmp_path, rec)
    with engine.attribute(batch_id="live_1", user_id="u1", plan_family="pro"):
        engine.call("question_generation", "prompt")
    rec.record_outcome("live_1", produced=3, accepted=2, plan_family="pro")

    report = rec.ledger.cost_per_accepted(batch_id="live_1")
    assert report["accepted"] == 2
    assert report["cost_per_batch"] > 0
    assert report["unpriced_calls"] == 0
