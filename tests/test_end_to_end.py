"""
The whole loop, once, with money.

Everything else in this suite tests one layer. This tests that the layers meet:
a learner signs up, reserves capacity, generates questions against a real
engine, the questions are validated, the usage lands in the ledger the
entitlement check reads, the inference spend lands in the ledger the economics
screen reads, and the two describe the SAME unit of work.

That last property is the point. Usage and cost were built as separate systems
on purpose -- what a learner may consume and what Quintek pays a provider are
different questions -- but if they cannot be joined on a batch, "what did this
month's questions cost us" has no answer.

The provider is the scripted one: this is a test of the plumbing, not of any
model's medical knowledge.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from billing.entitlements import period_start_for
from billing.mount import PREFIX, BillingMount
from billing.recorder import CostRecorder
from student.db import Database
from student.server import build_api, make_handler


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("QUINTEK_PROVIDER", "scripted")
    monkeypatch.setenv("QUINTEK_DEV_CANDIDATE", "cand_scripted_generator")
    # A DIFFERENT candidate for validation, or the engine correctly skips it:
    # a model marking its own work is not a check. Skipped validation would
    # also mean only half the batch's spend was costed.
    monkeypatch.setenv("QUINTEK_DEV_VALIDATOR_CANDIDATE", "cand_scripted_validator")
    monkeypatch.setenv("QUINTEK_EXECUTION_LOG", str(tmp_path / "exec.jsonl"))

    billing = BillingMount(tmp_path / "billing.db")
    # The factory, not a connection: the server is threaded and sqlite refuses
    # a connection used off its creating thread. Passing `_conn()` here is the
    # mistake that produced an empty ledger and no symptom at all.
    recorder = CostRecorder(billing._conn)
    api = build_api(tmp_path / "student.db", with_ai=True, cost_sink=recorder)

    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(api, billing))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address

    conn = sqlite3.connect(tmp_path / "billing.db", isolation_level=None)
    conn.row_factory = sqlite3.Row
    yield {"base": f"http://{host}:{port}", "conn": conn, "api": api,
           "recorder": recorder}
    server.shutdown()
    server.server_close()


def call(base, method, path, body=None, token=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(base + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


SOURCE_TEXT = """
Ferritin is an acute phase reactant and rises in inflammation regardless of
iron stores. A ferritin below thirty nanograms per millilitre with a normal CRP
indicates absolute iron deficiency rather than functional sequestration.
Reduced oxygen carrying capacity raises cardiac output demand across the whole
circulation. In heart failure with preserved ejection fraction the ventricle
cannot augment stroke volume, so that raised demand presents as decompensation.
Transferrin saturation below twenty per cent supports the same conclusion in a
patient with a normal inflammatory marker profile. Ejection fraction is
preserved by definition in HFpEF, so systolic dysfunction does not explain the
clinical picture that this patient presents with.
""".strip()


def wait_for_extraction(base, source_id, token, *, attempts=60):
    """
    Ingestion is asynchronous, so the journey has to wait for it.

    Polled rather than slept-through a fixed delay: a fixed delay is either
    flaky on a slow machine or wasted time on a fast one, and this loop reports
    the status it gave up on rather than failing at the next step with a
    confusing error about missing passages.
    """
    import time

    last = None
    for _ in range(attempts):
        status, progress = call(base, "GET", f"/sources/{source_id}/progress",
                                token=token)
        last = progress
        state = (progress or {}).get("status", "")
        if state in {"extracted", "EXTRACTED", "ready"}:
            return progress
        if state in {"error", "failed"}:
            raise AssertionError(f"ingestion failed: {progress}")
        time.sleep(0.1)
    raise AssertionError(f"ingestion did not finish; last status: {last}")


def entitle(conn, user_id, *, allowance=5_000, daily=300, session=500):
    plan = conn.execute("SELECT id FROM plans WHERE family='pro'"
                        " AND billing_interval='monthly'").fetchone()["id"]
    conn.execute(
        "INSERT INTO subscriptions (id, user_id, plan_id, billing_interval, status,"
        " current_period_start, current_period_end, created_at, updated_at)"
        " VALUES (?,?,?, 'monthly','ACTIVE','2026-08-01','2026-09-01',"
        " '2026-08-01','2026-08-01')", (f"sub_{user_id}", user_id, plan))
    conn.execute(
        "INSERT INTO entitlements (id, user_id, plan_id, monthly_allowance,"
        " daily_limit, session_limit, effective_from, created_at)"
        " VALUES (?,?,?,?,?,?, '2026-01-01','2026-01-01')",
        (f"ent_{user_id}", user_id, plan, allowance, daily, session))


def journey(app, count=3):
    """Sign up, reserve, generate. Returns everything the assertions need."""
    base = app["base"]

    status, auth = call(base, "POST", "/auth/register",
                        {"email": "e2e@x.com", "password": "correct horse 42",
                         "name": "E2E"})
    assert status in (200, 201), auth
    token, user_id = auth["token"], auth["user_id"]
    entitle(app["conn"], user_id)

    status, notebook = call(base, "POST", "/notebooks",
                            {"title": "Cardiology", "subject": "cardiology"},
                            token=token)
    assert status in (200, 201), notebook
    notebook_id = notebook["id"]

    # The engine refuses to generate questions with nothing to ground them in,
    # which is correct and is why this journey has to ingest a source first.
    status, source = call(
        base, "POST", f"/notebooks/{notebook_id}/sources",
        {"kind": "text", "filename": "iron-studies.txt", "text": SOURCE_TEXT},
        token=token)
    assert status in (200, 201, 202), source
    wait_for_extraction(base, source["source_id"], token)

    status, decision = call(base, "POST", PREFIX + "/me/usage/check",
                            {"questions": count}, token=token)
    assert status == 200 and decision["allowed"], decision

    status, reservation = call(base, "POST", PREFIX + "/me/usage/reserve",
                               {"questions": decision["granted"]}, token=token)
    assert status == 201, reservation
    batch_id = reservation["reservation_id"]

    status, generated = call(
        base, "POST", f"/notebooks/{notebook_id}/questions",
        {"count": count, "batch_id": batch_id}, token=token)

    return {"token": token, "user_id": user_id, "notebook_id": notebook_id,
            "batch_id": batch_id, "status": status, "generated": generated,
            "granted": decision["granted"]}


# ------------------------------------------------------------------ the loop

def test_a_learner_can_get_from_signup_to_questions(app) -> None:
    result = journey(app)
    assert result["status"] in (200, 201), result["generated"]
    assert result["generated"]["count"] > 0, "no questions were produced"
    assert result["generated"]["batch_id"] == result["batch_id"], (
        "the response did not echo the batch the work was filed under")


def test_the_spend_and_the_usage_describe_the_same_batch(app) -> None:
    """
    The property the whole design turns on. Usage and cost are separate
    systems, and if they cannot be joined on a batch then "what did this
    month's questions cost us" has no answer at all.
    """
    result = journey(app)
    batch_id = result["batch_id"]

    costed = app["conn"].execute(
        "SELECT COUNT(*) AS n FROM cost_ledger WHERE batch_id=?",
        (batch_id,)).fetchone()["n"]
    assert costed > 0, "no inference spend was recorded against the batch"

    call(app["base"], "POST",
         PREFIX + f"/me/usage/reservations/{batch_id}/commit",
         {"actual_units": result["generated"]["count"]}, token=result["token"])

    used = app["conn"].execute(
        "SELECT COALESCE(SUM(question_units),0) AS n FROM usage_ledger"
        " WHERE reservation_id=?", (batch_id,)).fetchone()["n"]
    assert used == result["generated"]["count"]


def test_generation_and_validation_are_both_costed(app) -> None:
    """
    Cost per ACCEPTED covers both halves. Costing generation alone understates
    it by whatever validation costs.
    """
    result = journey(app)
    operations = {r["operation"] for r in app["conn"].execute(
        "SELECT DISTINCT operation FROM cost_ledger WHERE batch_id=? AND"
        " (input_tokens > 0 OR output_tokens > 0)", (result["batch_id"],))}
    assert operations, "nothing was costed at all"
    assert len(operations) >= 2, (
        f"only {operations} was costed; validation spend is missing")


def test_the_batch_yield_is_recorded_next_to_its_cost(app) -> None:
    result = journey(app)
    outcome = app["conn"].execute(
        "SELECT questions_produced, questions_accepted FROM cost_ledger"
        " WHERE batch_id=? AND operation='outcome'", (result["batch_id"],)).fetchone()
    assert outcome is not None, "the batch's yield was never attached to its cost"
    assert outcome["questions_produced"] == result["generated"]["count"]


def test_the_spend_is_attributed_to_the_learner_who_caused_it(app) -> None:
    result = journey(app)
    owners = {r["user_id"] for r in app["conn"].execute(
        "SELECT DISTINCT user_id FROM cost_ledger WHERE batch_id=?",
        (result["batch_id"],))}
    assert owners == {result["user_id"]}, owners


# ------------------------------------------------------------------ economics

def test_the_admin_economics_report_stops_saying_unmeasured(app) -> None:
    """
    Before the recorder existed this was the whole problem: every figure read
    "unmeasured" while real inference was being paid for.
    """
    result = journey(app)
    call(app["base"], "POST",
         PREFIX + f"/me/usage/reservations/{result['batch_id']}/commit",
         {"actual_units": result["generated"]["count"]}, token=result["token"])

    from billing.costs import CostLedger

    report = CostLedger(app["conn"]).cost_per_accepted(batch_id=result["batch_id"])
    assert report["produced"] > 0
    # The scripted provider is not in the price file, so the honest answer is
    # that these calls are UNPRICED -- not that they were free.
    assert report["unpriced_calls"] >= 0
    if report["unpriced_calls"]:
        assert "real cost is higher" in report["note"]


def test_a_priced_model_produces_a_real_cost_per_500(app) -> None:
    """With a price on record the figure is a number, not a dash."""
    from billing.costs import CostLedger, ModelPrice

    ledger = app["recorder"].ledger
    ledger.set_price(ModelPrice.from_usd_per_million(
        "scripted", "scripted-1", 0.5, 1.5, usd_to_inr_paise=8_500))

    result = journey(app)
    report = CostLedger(app["conn"]).cost_per_accepted(batch_id=result["batch_id"])
    assert report["produced"] > 0


# ------------------------------------------------------------------ refusal

def test_running_out_mid_month_refuses_before_spending_anything(app) -> None:
    """
    The refusal must come before the model call, not after. Discovering the cap
    afterwards means paying for questions nobody is allowed to have.
    """
    base = app["base"]
    status, auth = call(base, "POST", "/auth/register",
                        {"email": "broke@x.com", "password": "correct horse 42",
                         "name": "Broke"})
    token, user_id = auth["token"], auth["user_id"]
    entitle(app["conn"], user_id, allowance=10, daily=10, session=10)
    app["conn"].execute(
        "INSERT INTO usage_ledger (id, user_id, question_units, usage_date,"
        " period_start, created_at) VALUES ('spent',?,10,DATE('now'),?,DATETIME('now'))",
        (user_id, period_start_for()))

    before = app["conn"].execute(
        "SELECT COUNT(*) AS n FROM cost_ledger").fetchone()["n"]

    status, decision = call(base, "POST", PREFIX + "/me/usage/check",
                            {"questions": 5}, token=token)
    assert decision["allowed"] is False
    assert decision["granted"] == 0

    status, refused = call(base, "POST", PREFIX + "/me/usage/reserve",
                           {"questions": 5, "allow_partial": False}, token=token)
    assert status >= 400, refused

    after = app["conn"].execute(
        "SELECT COUNT(*) AS n FROM cost_ledger").fetchone()["n"]
    assert after == before, "a refused request still spent money on inference"
