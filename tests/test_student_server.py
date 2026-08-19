"""
Transport-level tests.

`student/api.py` is already tested directly, so these do not re-test routing
logic. What they check is the things only the HTTP layer can get wrong: does
the bearer token actually reach the API, does a malformed body produce a 400
instead of a stack trace, does an unauthenticated call get refused before it
touches data, and does CORS preflight answer at all.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from student.server import build_api, make_handler


@pytest.fixture()
def live(tmp_path):
    api = build_api(tmp_path / "student.db", with_ai=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(api))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(method, path, body=None, token=None, raw=None, headers=None):
        request = urllib.request.Request(base + path, method=method)
        data = None
        if raw is not None:
            data = raw
            request.add_header("Content-Type", "application/json")
        elif body is not None:
            data = json.dumps(body).encode("utf-8")
            request.add_header("Content-Type", "application/json")
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, data, timeout=10) as response:
                payload = response.read()
                return response.status, json.loads(payload) if payload else {}, response.headers
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            return exc.code, json.loads(payload) if payload else {}, exc.headers

    try:
        yield call
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def token(live):
    status, body, _ = live("POST", "/auth/register",
                           {"email": "learner@example.test",
                            "password": "correct-horse-battery",
                            "name": "Learner"})
    assert status == 201, body
    return body["token"]


def test_register_then_login_round_trip(live):
    live("POST", "/auth/register", {"email": "a@example.test", "password": "a-long-password"})
    status, body, _ = live("POST", "/auth/login",
                           {"email": "a@example.test", "password": "a-long-password"})
    assert status == 200
    assert body["token"]


def test_bearer_token_reaches_the_api(live, token):
    status, body, _ = live("GET", "/me", token=token)
    assert status == 200
    assert body["email"] == "learner@example.test"


def test_missing_token_is_refused_at_the_edge(live):
    status, body, _ = live("GET", "/me")
    assert status == 401
    assert "error" in body


def test_a_non_bearer_authorization_header_is_not_treated_as_a_token(live, token):
    # "Basic <token>" must not authenticate, or the scheme means nothing.
    status, _, _ = live("GET", "/me", headers={"Authorization": f"Basic {token}"})
    assert status == 401


def test_malformed_json_body_is_a_400_not_a_crash(live):
    status, body, _ = live("POST", "/auth/login", raw=b"{not json")
    assert status == 400
    assert "malformed JSON" in body["error"]


def test_query_parameters_are_parsed(live, token):
    live("POST", "/notebooks", {"title": "Cardiology"}, token=token)
    status, body, _ = live("GET", "/questions?limit=3", token=token)
    assert status == 200
    assert isinstance(body["questions"], list)


def test_preflight_is_answered(live):
    status, _, headers = live("OPTIONS", "/me")
    assert status == 204
    assert headers["Access-Control-Allow-Origin"] == "*"


def test_unknown_path_is_404_with_a_json_body(live, token):
    status, body, headers = live("GET", "/no/such/route", token=token)
    assert status == 404
    assert headers["Content-Type"] == "application/json"
    assert "error" in body


def test_without_ai_the_engine_reports_unavailable_rather_than_pretending(live, token):
    status, body, _ = live("POST", "/notebooks", {"title": "Renal"}, token=token)
    notebook_id = body["id"]
    status, body, _ = live("POST", f"/notebooks/{notebook_id}/questions", {"count": 2}, token=token)
    # 503, not 200-with-nothing: an engine that cannot generate must say so.
    assert status == 503
    assert "error" in body


def test_build_api_without_ai_leaves_generation_services_unset(tmp_path):
    api = build_api(tmp_path / "s.db", with_ai=False)
    assert api.generator is None
    assert api.engine is None
