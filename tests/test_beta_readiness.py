"""
What has to be true before a named person other than the author can use this.

WHAT THE BRIEF GOT WRONG HERE, VERIFIED BY EXECUTION
------------------------------------------------------
Two of the five Phase 5 items were already done and the brief assumed
otherwise:

  CORS is not permissive by accident. `production.check()` already REFUSES to
  start a production deployment with `QUINTEK_CORS_ORIGIN` unset, and the
  message explains why `*` is defensible for the Android WebView specifically
  (origin is the literal string "null", and auth is a bearer header rather
  than a cookie, so `*` grants nothing).

  Token revocation exists. `db.revoke_token` and an `expires_at` on every
  session were already there.

What was genuinely absent: rate limiting at the HTTP layer, any request log,
a way to turn an account off, and a way to erase one.

THE ONE I NEARLY SHIPPED BROKEN
---------------------------------
`DELETE /account` was routed in the API and the server had no `do_DELETE`, so
`BaseHTTPRequestHandler` would have answered 501 Not Implemented. A learner
asking for erasure being told the server does not implement it is both wrong
and the worst possible answer to that request. Caught by driving a real HTTP
server rather than the API object, which is why the live test below exists.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from student import accounts, production, throttle
from student.api import StudentAPI
from student.db import Database
from student.server import build_api, make_handler


@pytest.fixture
def world(tmp_path):
    db = Database(tmp_path / "q.db")
    api = StudentAPI(db)

    def register(email):
        return api.handle("POST", "/auth/register", {},
                          {"email": email, "password": "correct-horse"}, None)[1]["token"]

    learner, admin = register("l@example.com"), register("ops@example.com")
    db.execute("UPDATE users SET role='admin' WHERE email='ops@example.com'")
    uid = db.query_one("SELECT id FROM users WHERE email='l@example.com'")["id"]
    api.handle("POST", "/notebooks", {}, {"title": "Renal", "subject": "Med"}, learner)

    w = type("W", (), {})()
    w.db, w.api, w.learner, w.admin, w.uid = db, api, learner, admin, uid
    return w


# ---------------------------------------------------------------------------
# Turning an account off
# ---------------------------------------------------------------------------

class TestSuspension:

    def test_a_suspended_account_stops_working(self, world):
        world.api.handle("POST", f"/admin/users/{world.uid}/suspend", {},
                         {"reason": "runaway ingestion loop"}, world.admin)
        status, _ = world.api.handle("GET", "/me", {}, {}, world.learner)
        assert status in (401, 403), "a suspended learner could still use their token"

    def test_suspension_revokes_live_sessions(self, world):
        """
        A suspension that leaves a valid token in someone's pocket has not
        stopped anything.
        """
        out = accounts.suspend(world.db, world.uid, by="ops", reason="abuse")
        assert out["sessions_revoked"] >= 1
        assert world.db.query_one(
            "SELECT COUNT(*) n FROM sessions_auth WHERE user_id=?", (world.uid,))["n"] == 0

    def test_a_token_issued_before_suspension_is_still_refused(self, world):
        """
        The gap `suspend()`'s revocation alone would leave. Checked on every
        authenticated request rather than only at login.
        """
        world.db.execute("UPDATE users SET status='suspended', status_reason='x'"
                         " WHERE id=?", (world.uid,))
        status, body = world.api.handle("GET", "/me", {}, {}, world.learner)
        assert status == 403
        assert "suspended" in str(body)

    def test_it_is_reversible_and_keeps_the_data(self, world):
        accounts.suspend(world.db, world.uid, by="ops", reason="mistake")
        accounts.reinstate(world.db, world.uid, by="ops")
        assert accounts.status_of(world.db, world.uid) == accounts.ACTIVE
        assert world.db.query_one(
            "SELECT COUNT(*) n FROM notebooks WHERE owner_id=?", (world.uid,))["n"] == 1

    @pytest.mark.parametrize("kwargs", [
        {"by": "", "reason": "x"},
        {"by": "ops", "reason": "  "},
    ])
    def test_it_refuses_to_be_anonymous_or_unexplained(self, world, kwargs):
        """
        A suspension with nobody attached is indistinguishable from a bug, and
        the person suspended is entitled to a reason from someone who can be
        asked about it.
        """
        with pytest.raises(accounts.AccountError):
            accounts.suspend(world.db, world.uid, **kwargs)

    def test_only_an_admin_can_suspend(self, world):
        status, _ = world.api.handle("POST", f"/admin/users/{world.uid}/suspend", {},
                                     {"reason": "x"}, world.learner)
        assert status == 404
        assert accounts.status_of(world.db, world.uid) == accounts.ACTIVE


# ---------------------------------------------------------------------------
# Erasing one
# ---------------------------------------------------------------------------

class TestErasure:

    def test_the_table_list_is_derived_from_the_live_schema(self, world):
        """
        Not hand-maintained. Twice on this project an inventory has been the
        gap rather than the code it inventoried.
        """
        found = dict(accounts.tables_holding_user_data(world.db))
        assert found["notebooks"] == "owner_id"
        assert found["attempts"] == "user_id"
        assert found["question_reports"] == "user_id"

    def test_a_new_table_is_covered_the_day_it_appears(self, world):
        world.db.execute("CREATE TABLE later_addition ("
                         " id TEXT PRIMARY KEY, user_id TEXT NOT NULL)")
        assert ("later_addition", "user_id") in accounts.tables_holding_user_data(world.db)

    def test_erasure_removes_the_data_and_verifies_it(self, world):
        out = accounts.erase(world.db, world.uid, confirm=world.uid)
        assert out["removed"]["notebooks"] == 1
        assert out["removed"]["users"] == 1
        for table, column in accounts.tables_holding_user_data(world.db):
            if table in accounts.RETAINED_ANONYMISED:
                continue
            left = world.db.query_one(
                f'SELECT COUNT(*) n FROM "{table}" WHERE {column}=?', (world.uid,))
            assert left["n"] == 0, f"{table} still holds rows for the erased user"

    def test_it_raises_rather_than_reporting_an_incomplete_erasure(self, world, monkeypatch):
        """
        The verification pass, exercised on the case it exists for: a table the
        CASCADE DOES NOT COVER.

        An earlier version of this test suppressed the explicit delete of
        `notebooks` and expected rows to survive. They did not -- `PRAGMA
        foreign_keys = ON` cascades from `users`, so the row went anyway and
        the test proved nothing. That is worth knowing and worth stating: the
        cascade works for tables that declare the reference. It is the ones
        that do not, or that reference by a column nobody wired up, that this
        check is for -- and a cascade cannot report what it removed, so
        relying on one makes "your data is deleted" a claim nobody verified.
        """
        world.db.execute("CREATE TABLE orphan_notes ("
                         " id TEXT PRIMARY KEY, user_id TEXT NOT NULL)")
        world.db.execute("INSERT INTO orphan_notes (id, user_id) VALUES ('n1', ?)",
                         (world.uid,))
        real = world.db.execute

        def skip_orphans(sql, params=()):
            # Double quotes: `erase()` quotes identifiers the SQL-standard
            # way now. Single quotes made PostgreSQL read the table name as a
            # string literal, so `DELETE FROM 'attempts'` was a syntax error.
            if 'DELETE FROM "orphan_notes"' in sql:
                class R:
                    rowcount = 0
                return R()
            return real(sql, params)

        monkeypatch.setattr(world.db, "execute", skip_orphans)
        with pytest.raises(accounts.AccountError, match="erasure incomplete"):
            accounts.erase(world.db, world.uid, confirm=world.uid)

    def test_a_table_the_cascade_does_not_cover_is_still_erased(self, world):
        """The positive control for the test above."""
        world.db.execute("CREATE TABLE orphan_notes ("
                         " id TEXT PRIMARY KEY, user_id TEXT NOT NULL)")
        world.db.execute("INSERT INTO orphan_notes (id, user_id) VALUES ('n1', ?)",
                         (world.uid,))
        accounts.erase(world.db, world.uid, confirm=world.uid)
        assert world.db.query_one("SELECT COUNT(*) n FROM orphan_notes")["n"] == 0

    def test_it_takes_no_defaults(self, world):
        with pytest.raises(accounts.AccountError, match="confirmed"):
            accounts.erase(world.db, world.uid, confirm="")

    def test_operational_records_are_anonymised_not_deleted(self, world):
        """
        An incident is evidence the system failed. Erasing it because the
        affected account left would let a fault be erased by erasing the person
        who hit it.
        """
        from student import operations as ops
        ops.record(world.db, operation=ops.INGESTION, error=ValueError("pdf unreadable"),
                   user_id=world.uid)
        out = accounts.erase(world.db, world.uid, confirm=world.uid)
        assert out["anonymised"]["incidents"] == 1
        assert world.db.query_one("SELECT COUNT(*) n FROM incidents")["n"] == 1
        assert world.db.query_one("SELECT user_id FROM incidents")["user_id"] == ""

    def test_every_anonymised_table_states_why(self):
        for table, reason in accounts.RETAINED_ANONYMISED.items():
            assert len(reason) > 40, (
                f"{table} is an exception to 'your data is deleted' and needs a "
                "reason defensible in writing")

    def test_a_learner_can_export_before_erasing(self, world):
        status, out = world.api.handle("GET", "/account/export", {}, {}, world.learner)
        assert status == 200
        assert out["user"]["email"] == "l@example.com"
        assert "notebooks" in out["tables"]

    def test_the_export_never_contains_the_password_hash(self, world):
        _, out = world.api.handle("GET", "/account/export", {}, {}, world.learner)
        blob = json.dumps(out)
        assert "password_hash" not in blob and "password_salt" not in blob

    def test_uploads_are_deleted_but_only_inside_the_storage_directory(self, tmp_path):
        storage = tmp_path / "storage"
        storage.mkdir()
        (storage / "src_1.pdf").write_bytes(b"mine")
        outside = tmp_path / "not-mine.pdf"
        outside.write_bytes(b"someone else's")
        accounts.erase_uploads(storage, ["src_1.pdf", "../not-mine.pdf"])
        assert not (storage / "src_1.pdf").exists()
        assert outside.exists(), "erasure walked outside the storage directory"


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

class TestThrottle:

    def test_it_refuses_rather_than_blocking(self):
        """
        A thread held open sleeping is the resource an abusive caller wants.
        `check` never sleeps -- it returns a verdict.
        """
        limiter = throttle.RateLimiter(limit=2, window=60.0, clock=lambda: 0.0)
        assert limiter.check(token=None, address="1.2.3.4", path="/me")["allowed"]
        assert limiter.check(token=None, address="1.2.3.4", path="/me")["allowed"]
        verdict = limiter.check(token=None, address="1.2.3.4", path="/me")
        assert verdict["allowed"] is False
        assert verdict["retry_after"] > 0

    def test_an_authenticated_caller_is_limited_per_account_not_per_address(self):
        """
        Keying only on address punishes everyone behind one NAT -- a hospital,
        a university residence, a carrier -- which is most of the audience.
        """
        limiter = throttle.RateLimiter(limit=1, window=60.0, clock=lambda: 0.0)
        assert limiter.check(token="alice", address="1.2.3.4", path="/me")["allowed"]
        assert limiter.check(token="bob", address="1.2.3.4", path="/me")["allowed"], (
            "two accounts behind one address limited each other")

    def test_the_token_is_never_a_key(self):
        limiter = throttle.RateLimiter()
        who, _bucket = limiter.key_for(token="secret-token-value",
                                       address="1.2.3.4", path="/me")
        assert "secret-token-value" not in who

    def test_login_is_bounded_harder_than_everything_else(self):
        limiter = throttle.RateLimiter(limit=1000, window=60.0, clock=lambda: 0.0)
        codes = [limiter.check(token=None, address="1.2.3.4", path="/auth/login")["allowed"]
                 for _ in range(throttle.SENSITIVE["/auth/login"] + 2)]
        assert codes[-1] is False, "guessing a password is not rate limited"

    def test_the_window_resets(self):
        now = [0.0]
        limiter = throttle.RateLimiter(limit=1, window=10.0, clock=lambda: now[0])
        assert limiter.check(token="a", address="x", path="/me")["allowed"]
        assert not limiter.check(token="a", address="x", path="/me")["allowed"]
        now[0] = 11.0
        assert limiter.check(token="a", address="x", path="/me")["allowed"]

    def test_the_table_does_not_grow_forever(self):
        """
        The leak that turns a rate limiter into the outage it was meant to
        prevent: unbounded memory driven by the caller.
        """
        now = [0.0]
        limiter = throttle.RateLimiter(limit=5, window=10.0, clock=lambda: now[0])
        for n in range(300):
            limiter.check(token=f"caller-{n}", address="x", path="/me")
        assert len(limiter) == 300
        now[0] = 100.0
        limiter.forget()
        assert len(limiter) == 0


# ---------------------------------------------------------------------------
# Over a real socket
# ---------------------------------------------------------------------------

class TestTheRealServer:

    @pytest.fixture
    def live(self, tmp_path):
        api = build_api(tmp_path / "q.db", with_ai=False)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(api))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
        httpd.shutdown()

    @staticmethod
    def request(base, method, path, body=None, token=None):
        req = urllib.request.Request(
            base + path, method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"content-type": "application/json",
                     **({"authorization": "Bearer " + token} if token else {})})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def test_erasure_is_reachable_over_http(self, live):
        """
        THE ONE THIS FILE EXISTS FOR. `DELETE /account` was routed in the API
        while the server had no `do_DELETE`, so the real answer would have been
        501 Not Implemented -- told to a learner asking to be forgotten.
        """
        _, out = self.request(live, "POST", "/auth/register",
                              {"email": "e@example.com", "password": "correct-horse"})
        status, _ = self.request(live, "DELETE", "/account", token=out["token"])
        assert status != 501, "DELETE /account is routed but the server cannot serve it"
        assert status == 200

    def test_a_burst_is_refused_with_a_retry_after(self, live):
        codes = [self.request(live, "POST", "/auth/register",
                              {"email": f"b{n}@example.com", "password": "correct-horse"})[0]
                 for n in range(throttle.SENSITIVE["/auth/register"] + 4)]
        assert 429 in codes, f"no request was throttled: {codes}"

    def test_health_is_served_without_a_session(self, live):
        assert self.request(live, "GET", "/health")[0] == 200

    def test_the_scope_statement_is_served_without_a_session(self, live):
        status, out = self.request(live, "GET", "/scope")
        assert status == 200 and "revision aid" in out["scope_statement"]


# ---------------------------------------------------------------------------
# What the brief assumed was missing and was not
# ---------------------------------------------------------------------------

class TestAlreadyDone:

    def test_production_refuses_an_unset_cors_origin(self):
        problems = production.check({"QUINTEK_ENV": "production",
                                     "QUINTEK_DATABASE_URL": "postgres://x?sslmode=require"})
        assert any("CORS" in p for p in problems)

    def test_production_refuses_ephemeral_storage(self):
        problems = production.check({"QUINTEK_ENV": "production"})
        assert any("QUINTEK_DATABASE_URL" in p for p in problems)

    def test_tokens_can_be_revoked_and_expire(self, world):
        assert hasattr(world.db, "revoke_token")
        columns = {r[1] for r in world.db.connect().execute(
            "PRAGMA table_info(sessions_auth)")}
        assert "expires_at" in columns

    def test_an_unqualified_model_cannot_be_forced_into_production(self):
        problems = production.check({
            "QUINTEK_ENV": "production",
            "QUINTEK_DATABASE_URL": "postgres://x?sslmode=require",
            "QUINTEK_CORS_ORIGIN": "https://app.example.com",
            "QUINTEK_DEV_CANDIDATE": "some-model"})
        assert any("QUINTEK_DEV_CANDIDATE" in p for p in problems), (
            "the hard gate is that no learner sees model-generated medical content "
            "until the validator clears its holdout; this is the environment "
            "variable that would route around it")
