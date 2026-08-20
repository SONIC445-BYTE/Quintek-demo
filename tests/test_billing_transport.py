"""
Billing on the wire.

These tests drive a REAL socket against the real handler, because the defects
this layer produces are transport defects and none of them are visible from
`BillingAPI.handle`: a webhook whose body was re-serialised before the
signature check, a learner reading another learner's usage by naming them in
the body, a route collision between `/me` and `/me/usage`.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from billing.gateway import RazorpayAdapter
from billing.mount import PREFIX, BillingMount
from student.db import Database
from student.server import make_handler


@pytest.fixture()
def stack(tmp_path):
    from student.api import StudentAPI

    db = Database(tmp_path / "student.db")
    api = StudentAPI(db)

    gateway = RazorpayAdapter(key_id="test_id", key_secret="s",
                              webhook_secret="whsec-for-tests")
    billing_path = tmp_path / "billing.db"
    mount = BillingMount(billing_path, gateway=gateway)
    # A separate connection for the test's own assertions, so nothing here
    # borrows the mount's thread-local one.
    conn = sqlite3.connect(billing_path, isolation_level=None)
    conn.row_factory = sqlite3.Row

    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(api, mount))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    base = f"http://{host}:{port}"
    yield {"base": base, "db": db, "api": api, "mount": mount, "conn": conn,
           "gateway": gateway}
    server.shutdown()
    server.server_close()


def call(base, method, path, body=None, token=None, headers=None, raw=None):
    data = raw if raw is not None else (
        json.dumps(body).encode("utf-8") if body is not None else None)
    request = urllib.request.Request(base + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def register(stack, email="a@b.c", role="student"):
    status, payload = call(stack["base"], "POST", "/auth/register",
                           {"email": email, "password": "correct horse 42",
                            "name": "Test"})
    assert status in (200, 201), payload
    token = payload.get("token") or payload.get("session", {}).get("token")
    assert token, payload
    if role != "student":
        stack["db"].execute("UPDATE users SET role=? WHERE email=?", (role, email))
    return token, payload


# ------------------------------------------------------------------ routing

def test_pricing_is_public(stack) -> None:
    status, payload = call(stack["base"], "GET", PREFIX + "/pricing")
    assert status == 200
    assert payload["families"], "the pricing page served no plans"


def test_the_learner_profile_and_billing_usage_do_not_collide(stack) -> None:
    """
    `/me` is the student API's profile. `/me/usage` is billing's. They are
    served by different modules against different databases, and the prefix is
    what keeps that unambiguous.
    """
    token, _ = register(stack)
    status, profile = call(stack["base"], "GET", "/me", token=token)
    assert status == 200 and "email" in profile

    status, usage = call(stack["base"], "GET", PREFIX + "/me/usage", token=token)
    assert status == 200
    assert "email" not in usage


def test_an_unknown_billing_route_is_a_404_not_a_student_route(stack) -> None:
    token, _ = register(stack)
    status, _ = call(stack["base"], "GET", PREFIX + "/notebooks", token=token)
    assert status == 404


# ------------------------------------------------------------------ identity

def test_billing_requires_a_session(stack) -> None:
    status, payload = call(stack["base"], "GET", PREFIX + "/me/usage")
    assert status == 401


def test_a_body_cannot_claim_another_users_identity(stack) -> None:
    """
    The single most valuable thing this layer does. Identity is taken from the
    bearer token; a `user_id` in the body is inert.
    """
    token_a, payload_a = register(stack, "a@x.com")
    token_b, payload_b = register(stack, "b@x.com")
    id_b = payload_b.get("user", {}).get("id") or payload_b.get("id")

    status, body = call(stack["base"], "POST", PREFIX + "/me/usage/check",
                        {"questions": 1, "user_id": id_b}, token=token_a)
    assert status == 200
    # The answer is A's, whatever the body said. Proven by asking as A with no
    # claim at all and getting the identical answer.
    status, plain = call(stack["base"], "POST", PREFIX + "/me/usage/check",
                         {"questions": 1}, token=token_a)
    assert body == plain


def test_a_learner_cannot_see_the_admin_surface(stack) -> None:
    token, _ = register(stack)
    status, _ = call(stack["base"], "GET", PREFIX + "/admin/economics", token=token)
    assert status == 404, "an admin route must not even admit to existing"


def test_an_admin_can(stack) -> None:
    token, _ = register(stack, "admin@x.com", role="admin")
    status, payload = call(stack["base"], "GET", PREFIX + "/admin/economics",
                           token=token)
    assert status == 200, payload


# ------------------------------------------------------------------ webhooks

def sign(raw: bytes) -> str:
    import hashlib
    import hmac
    return hmac.new(b"whsec-for-tests", raw, hashlib.sha256).hexdigest()


def known_subscription(stack, gateway_ref="sub_live_1"):
    """A subscription the webhook can be attributed to."""
    conn = stack["conn"]
    plan = conn.execute("SELECT id FROM plans WHERE price_minor > 0 LIMIT 1"
                        ).fetchone()["id"]
    conn.execute(
        "INSERT INTO subscriptions (id, user_id, plan_id, billing_interval,"
        " gateway, gateway_subscription_id, status, created_at, updated_at)"
        " VALUES ('sub_1','u1',?, 'monthly','razorpay',?, 'PENDING',"
        " '2026-08-01','2026-08-01')", (plan, gateway_ref))
    return gateway_ref


def test_a_webhook_is_verified_against_the_bytes_that_were_sent(stack) -> None:
    """
    The body is signed as sent. This payload carries whitespace and key order
    that `json.dumps` would not reproduce, so a handler that re-serialised
    before verifying would fail here -- which is the whole point of reading the
    raw bytes once, before anything parses them.
    """
    ref = known_subscription(stack)
    raw = ('{ "event":"subscription.activated",\n  "id":"evt_1",\n'
           '  "payload":{"subscription":{"entity":{"id":"%s","status":"active"}}} }'
           % ref).encode("utf-8")
    assert raw != json.dumps(json.loads(raw)).encode("utf-8"), (
        "this payload must NOT survive a round trip, or it proves nothing")

    status, payload = call(stack["base"], "POST", PREFIX + "/webhooks/razorpay",
                           raw=raw, headers={"X-Razorpay-Signature": sign(raw)})
    assert status == 200, payload
    assert payload["status"] != "FAILED", payload
    row = stack["conn"].execute(
        "SELECT status FROM subscriptions WHERE id='sub_1'").fetchone()
    assert row["status"] == "ACTIVE"


def test_a_replayed_webhook_changes_nothing(stack) -> None:
    ref = known_subscription(stack)
    raw = ('{"event":"subscription.activated","id":"evt_dup","payload":'
           '{"subscription":{"entity":{"id":"%s","status":"active"}}}}' % ref
           ).encode("utf-8")
    first = call(stack["base"], "POST", PREFIX + "/webhooks/razorpay",
                 raw=raw, headers={"X-Razorpay-Signature": sign(raw)})
    second = call(stack["base"], "POST", PREFIX + "/webhooks/razorpay",
                  raw=raw, headers={"X-Razorpay-Signature": sign(raw)})
    assert first[0] == 200 and second[0] == 200
    assert second[1]["status"] in {"IGNORED", "DUPLICATE"}, second[1]
    count = stack["conn"].execute(
        "SELECT COUNT(*) AS n FROM webhook_events").fetchone()["n"]
    assert count == 1


def test_a_forged_signature_is_rejected(stack) -> None:
    ref = known_subscription(stack)
    raw = ('{"event":"subscription.activated","id":"evt_2","payload":'
           '{"subscription":{"entity":{"id":"%s","status":"active"}}}}' % ref
           ).encode("utf-8")
    status, payload = call(stack["base"], "POST", PREFIX + "/webhooks/razorpay",
                           raw=raw, headers={"X-Razorpay-Signature": "0" * 64})
    assert status == 400, payload
    row = stack["conn"].execute(
        "SELECT status FROM subscriptions WHERE id='sub_1'").fetchone()
    assert row["status"] == "PENDING", "a forged event activated a subscription"


def test_a_webhook_needs_no_session(stack) -> None:
    """A gateway has no bearer token; requiring one would reject every event."""
    ref = known_subscription(stack)
    raw = ('{"event":"subscription.activated","id":"evt_3","payload":'
           '{"subscription":{"entity":{"id":"%s","status":"active"}}}}' % ref
           ).encode("utf-8")
    status, _ = call(stack["base"], "POST", PREFIX + "/webhooks/razorpay",
                     raw=raw, headers={"X-Razorpay-Signature": sign(raw)})
    assert status == 200


def test_an_unparseable_body_fails_the_signature_check_not_the_json_parser(
        stack) -> None:
    """
    Deciding whether a payload is genuine belongs to the signature check. The
    status is the same 400 either way -- what matters is WHICH check rejected
    it, because "malformed JSON" tells a forger their payload never reached
    verification.
    """
    status, payload = call(stack["base"], "POST", PREFIX + "/webhooks/razorpay",
                           raw=b"not json at all",
                           headers={"X-Razorpay-Signature": "0" * 64})
    assert status == 400
    assert "malformed" not in json.dumps(payload).lower()
    assert payload.get("status") == "REJECTED"


def test_an_event_for_an_unknown_subscription_asks_for_a_retry(stack) -> None:
    """
    500, deliberately. A webhook can overtake the checkout that created the
    subscription locally, and that race resolves on retry. Answering 200 would
    tell the gateway to stop retrying an event that has not been applied.
    """
    raw = (b'{"event":"subscription.activated","id":"evt_x","payload":'
           b'{"subscription":{"entity":{"id":"sub_nobody","status":"active"}}}}')
    status, payload = call(stack["base"], "POST", PREFIX + "/webhooks/razorpay",
                           raw=raw, headers={"X-Razorpay-Signature": sign(raw)})
    assert status == 500
    assert payload["status"] == "FAILED"


def test_a_malformed_body_on_a_normal_route_is_a_400(stack) -> None:
    token, _ = register(stack)
    status, payload = call(stack["base"], "POST", PREFIX + "/me/usage/check",
                           raw=b"{oh no", token=token)
    assert status == 400
    assert "malformed" in payload["error"].lower()


# ------------------------------------------------------------------ seeding

def test_the_mount_seeds_plans_once_and_only_once(tmp_path) -> None:
    path = tmp_path / "b.db"
    first = BillingMount(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    count = conn.execute("SELECT COUNT(*) AS n FROM plans").fetchone()["n"]
    assert count > 0
    BillingMount(path)
    assert conn.execute("SELECT COUNT(*) AS n FROM plans").fetchone()["n"] == count
    assert first.plans.all_active()


def test_each_thread_gets_its_own_connection(tmp_path) -> None:
    """
    sqlite refuses a connection used off its creating thread, and the server is
    threaded. Calling `handle` directly from a test never catches this; only
    calling it from another thread does.
    """
    mount = BillingMount(tmp_path / "b.db")
    results = []

    def worker():
        results.append(mount.handle("GET", PREFIX + "/pricing", {}, b"", {}, None))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert [status for status, _ in results] == [200] * 4
