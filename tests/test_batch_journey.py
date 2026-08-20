"""
The 500-question batch, end to end, over HTTP.

This is the journey the product is actually sold on and the one where a
mistake costs money in both directions: charge for questions that were never
produced, or produce questions nobody was charged for. It is tested as a
sequence rather than as separate units because every defect here lives in the
seams -- between the check and the reservation, between generation and the
commit.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from billing.entitlements import period_start_for
from billing.mount import PREFIX, BillingMount
from student.api import StudentAPI
from student.db import Database
from student.server import make_handler

PLAN_ALLOWANCE = 5_000
DAILY_LIMIT = 300
SESSION_LIMIT = 500


@pytest.fixture()
def world(tmp_path):
    db = Database(tmp_path / "student.db")
    api = StudentAPI(db)
    mount = BillingMount(tmp_path / "billing.db")

    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(api, mount))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address

    conn = sqlite3.connect(tmp_path / "billing.db", isolation_level=None)
    conn.row_factory = sqlite3.Row

    ctx = {"base": f"http://{host}:{port}", "conn": conn, "db": db}
    yield ctx
    server.shutdown()
    server.server_close()


def call(base, method, path, body=None, token=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(base + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def learner(world, email="learner@x.com"):
    status, payload = call(world["base"], "POST", "/auth/register",
                           {"email": email, "password": "correct horse 42",
                            "name": "Learner"})
    assert status in (200, 201), payload
    token = payload.get("token") or payload.get("session", {}).get("token")
    user_id = payload.get("user_id") or (payload.get("user") or {}).get("id")
    assert token and user_id, payload
    return token, user_id


def entitle(world, user_id, *, allowance=PLAN_ALLOWANCE, daily=DAILY_LIMIT,
            session=SESSION_LIMIT):
    conn = world["conn"]
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


def consume(world, user_id, units):
    """Record usage directly, standing in for questions generated earlier today."""
    world["conn"].execute(
        "INSERT INTO usage_ledger (id, user_id, question_units, usage_date,"
        " period_start, created_at) VALUES (?,?,?,DATE('now'),?,DATETIME('now'))",
        (f"u_{user_id}_{units}", user_id, units, period_start_for()))


# ------------------------------------------------------------- the happy path

def test_a_batch_within_allowance_is_granted_in_full(world) -> None:
    token, user_id = learner(world)
    entitle(world, user_id)
    status, decision = call(world["base"], "POST", PREFIX + "/me/usage/check",
                            {"questions": 100}, token=token)
    assert status == 200
    assert decision["allowed"] is True
    assert decision["partial"] is False
    assert decision["granted"] == 100


def test_checking_consumes_nothing(world) -> None:
    """
    The check is a question, not a purchase. If it consumed, a user who
    changed their mind would be charged for a batch they never took.
    """
    token, user_id = learner(world)
    entitle(world, user_id)
    for _ in range(5):
        call(world["base"], "POST", PREFIX + "/me/usage/check",
             {"questions": 500}, token=token)
    status, usage = call(world["base"], "GET", PREFIX + "/me/usage", token=token)
    assert usage["this_month"]["used"] == 0


# ------------------------------------------------------------- partial capacity

def test_five_hundred_against_a_daily_cap_is_offered_partially(world) -> None:
    token, user_id = learner(world)
    entitle(world, user_id)
    consume(world, user_id, 127)                     # 300 - 127 = 173 left today

    status, decision = call(world["base"], "POST", PREFIX + "/me/usage/check",
                            {"questions": 500}, token=token)
    assert status == 200
    assert decision["allowed"] is True
    assert decision["partial"] is True
    assert decision["granted"] == 173
    assert "generate_available" in decision["actions"]


def test_the_partial_message_names_the_limit_that_actually_bound(world) -> None:
    """
    Telling someone their MONTHLY allowance ran out when it was the daily cap
    sends them to upgrade for nothing -- the upgrade would not have helped.
    """
    token, user_id = learner(world)
    entitle(world, user_id)
    consume(world, user_id, 127)

    _, decision = call(world["base"], "POST", PREFIX + "/me/usage/check",
                       {"questions": 500}, token=token)
    assert decision["availability"]["binding_constraint"] == "daily"
    assert "daily" in decision["reason"].lower()
    assert "monthly allowance" not in decision["reason"].lower()


def test_a_grant_is_never_larger_than_what_is_available(world) -> None:
    token, user_id = learner(world)
    entitle(world, user_id)
    consume(world, user_id, 299)
    _, decision = call(world["base"], "POST", PREFIX + "/me/usage/check",
                       {"questions": 500}, token=token)
    assert decision["granted"] == 1
    assert decision["granted"] <= decision["availability"]["available_now"]


def test_nothing_left_today_is_a_refusal_with_a_route_out(world) -> None:
    token, user_id = learner(world)
    entitle(world, user_id)
    consume(world, user_id, DAILY_LIMIT)
    status, decision = call(world["base"], "POST", PREFIX + "/me/usage/check",
                            {"questions": 500}, token=token)
    assert decision["allowed"] is False
    assert decision["granted"] == 0
    assert "upgrade" in decision["actions"]
    assert "resets tomorrow" in decision["reason"]


# ------------------------------------------------------------- reserve/settle

def test_the_full_sequence_reserve_generate_commit(world) -> None:
    token, user_id = learner(world)
    entitle(world, user_id)
    consume(world, user_id, 127)

    _, decision = call(world["base"], "POST", PREFIX + "/me/usage/check",
                       {"questions": 500}, token=token)
    granted = decision["granted"]

    status, reservation = call(world["base"], "POST", PREFIX + "/me/usage/reserve",
                               {"questions": granted, "allow_partial": False},
                               token=token)
    assert status == 201, reservation
    assert reservation["question_units"] == granted
    assert reservation["status"] == "HELD"

    status, settled = call(
        world["base"], "POST",
        PREFIX + f"/me/usage/reservations/{reservation['reservation_id']}/commit",
        {"actual_units": granted}, token=token)
    assert status == 200, settled

    _, usage = call(world["base"], "GET", PREFIX + "/me/usage", token=token)
    assert usage["today"]["used"] == 127 + granted


def test_a_held_reservation_is_visible_to_the_next_request(world) -> None:
    """
    The whole point of reserving. Two devices must not both spend the same
    remaining 173.
    """
    token, user_id = learner(world)
    entitle(world, user_id)
    consume(world, user_id, 127)

    call(world["base"], "POST", PREFIX + "/me/usage/reserve",
         {"questions": 173, "allow_partial": False}, token=token)

    _, decision = call(world["base"], "POST", PREFIX + "/me/usage/check",
                       {"questions": 100}, token=token)
    assert decision["allowed"] is False, (
        "a second request was offered capacity that is already held")


def test_settling_lower_than_reserved_charges_only_what_was_produced(world) -> None:
    """
    The engine can return fewer than were reserved -- a thin source, a
    validator that rejects everything. Charging for questions that do not
    exist is the one billing error a user always notices.
    """
    token, user_id = learner(world)
    entitle(world, user_id)

    _, reservation = call(world["base"], "POST", PREFIX + "/me/usage/reserve",
                          {"questions": 100}, token=token)
    call(world["base"], "POST",
         PREFIX + f"/me/usage/reservations/{reservation['reservation_id']}/commit",
         {"actual_units": 12}, token=token)

    _, usage = call(world["base"], "GET", PREFIX + "/me/usage", token=token)
    assert usage["today"]["used"] == 12


def test_settling_higher_than_reserved_is_refused(world) -> None:
    """A reservation is a ceiling. Otherwise it authorises nothing."""
    token, user_id = learner(world)
    entitle(world, user_id)

    _, reservation = call(world["base"], "POST", PREFIX + "/me/usage/reserve",
                          {"questions": 10}, token=token)
    status, payload = call(
        world["base"], "POST",
        PREFIX + f"/me/usage/reservations/{reservation['reservation_id']}/commit",
        {"actual_units": 999}, token=token)

    _, usage = call(world["base"], "GET", PREFIX + "/me/usage", token=token)
    assert usage["today"]["used"] <= 10, "a commit exceeded its reservation"


def test_releasing_returns_the_capacity(world) -> None:
    token, user_id = learner(world)
    entitle(world, user_id)
    consume(world, user_id, 127)

    _, reservation = call(world["base"], "POST", PREFIX + "/me/usage/reserve",
                          {"questions": 173, "allow_partial": False}, token=token)
    call(world["base"], "POST",
         PREFIX + f"/me/usage/reservations/{reservation['reservation_id']}/release",
         {"reason": "generation failed"}, token=token)

    _, decision = call(world["base"], "POST", PREFIX + "/me/usage/check",
                       {"questions": 173}, token=token)
    assert decision["granted"] == 173, "released capacity was not returned"

    _, usage = call(world["base"], "GET", PREFIX + "/me/usage", token=token)
    assert usage["today"]["used"] == 127, "a released reservation was charged for"


def test_another_learner_cannot_settle_your_reservation(world) -> None:
    token_a, id_a = learner(world, "a@x.com")
    token_b, id_b = learner(world, "b@x.com")
    entitle(world, id_a)
    entitle(world, id_b)

    _, reservation = call(world["base"], "POST", PREFIX + "/me/usage/reserve",
                          {"questions": 50}, token=token_a)
    status, _ = call(
        world["base"], "POST",
        PREFIX + f"/me/usage/reservations/{reservation['reservation_id']}/commit",
        {"actual_units": 50}, token=token_b)
    assert status in (403, 404), "one learner settled another's reservation"

    _, usage_b = call(world["base"], "GET", PREFIX + "/me/usage", token=token_b)
    assert usage_b["today"]["used"] == 0
