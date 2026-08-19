"""
Phase 1: persistence, identity, and the two invariants the schema enforces
rather than merely documents.
"""

from __future__ import annotations

import sqlite3

import pytest

from student.api import StudentAPI
from student.db import Database, now_iso


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "q.db")


@pytest.fixture
def api(db):
    return StudentAPI(db)


@pytest.fixture
def user(db):
    uid = db.create_user("learner@example.com", "correct-horse", name="Learner")
    return uid, db.issue_token(uid)


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def test_password_is_never_stored_in_the_clear(db):
    db.create_user("a@example.com", "hunter2hunter2")
    row = db.query_one("SELECT * FROM users WHERE email = 'a@example.com'")
    stored = " ".join(str(v) for v in tuple(row))
    assert "hunter2hunter2" not in stored
    assert len(row["password_hash"]) == 64          # sha256 hex
    assert row["password_salt"] != ""


def test_each_user_gets_a_distinct_salt(db):
    db.create_user("a@example.com", "same-password-x")
    db.create_user("b@example.com", "same-password-x")
    rows = db.query("SELECT password_salt, password_hash FROM users")
    assert rows[0]["password_salt"] != rows[1]["password_salt"]
    # Identical passwords must not produce identical hashes, or the database
    # leaks which accounts share one.
    assert rows[0]["password_hash"] != rows[1]["password_hash"]


def test_login_accepts_the_right_password_and_rejects_the_wrong_one(db):
    uid = db.create_user("a@example.com", "correct-horse")
    assert db.verify_password("a@example.com", "correct-horse") == uid
    assert db.verify_password("a@example.com", "correct-horse ") is None
    assert db.verify_password("nobody@example.com", "correct-horse") is None


def test_weak_password_and_bad_email_are_refused(db):
    with pytest.raises(ValueError):
        db.create_user("a@example.com", "short")
    with pytest.raises(ValueError):
        db.create_user("not-an-email", "long-enough-password")


def test_token_round_trip_and_revocation(db):
    uid = db.create_user("a@example.com", "correct-horse")
    token = db.issue_token(uid)
    assert db.user_for_token(token)["id"] == uid
    db.revoke_token(token)
    assert db.user_for_token(token) is None
    assert db.user_for_token(None) is None


def test_expired_token_is_rejected(db):
    uid = db.create_user("a@example.com", "correct-horse")
    token = db.issue_token(uid, ttl_hours=1)
    db.execute("UPDATE sessions_auth SET expires_at = '2000-01-01T00:00:00Z' WHERE token = ?",
               (token,))
    assert db.user_for_token(token) is None


# ---------------------------------------------------------------------------
# Schema invariants
# ---------------------------------------------------------------------------

def _make_attempt(db, uid):
    db.execute("INSERT INTO notebooks (id,owner_id,title,created_at) VALUES ('nb','%s','N',?)"
               % uid, (now_iso(),))
    db.execute("INSERT INTO questions (id,primary_notebook_id,stem,options_json,correct_index,"
               "generated_at) VALUES ('q','nb','stem','[\"a\",\"b\"]',1,?)", (now_iso(),))
    db.execute("INSERT INTO attempts (id,question_id,user_id,user_answer,correct_answer,"
               "is_correct,user_colour,created_at) VALUES ('att','q',?,1,1,1,'GREEN',?)",
               (uid, now_iso()))


def test_attempts_cannot_be_updated(db, user):
    uid, _ = user
    _make_attempt(db, uid)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("UPDATE attempts SET is_correct = 0 WHERE id = 'att'")


def test_attempts_cannot_be_deleted(db, user):
    """
    History is the evidence base for every colour, priority and gap the product
    asserts. Evidence that can be quietly removed is not evidence.
    """
    uid, _ = user
    _make_attempt(db, uid)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("DELETE FROM attempts WHERE id = 'att'")
    assert db.query_one("SELECT COUNT(*) c FROM attempts")["c"] == 1


def test_foreign_keys_are_actually_enforced(db):
    """SQLite disables foreign keys by default, per connection, silently. A
    schema full of unenforced REFERENCES is worse than none."""
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO notebooks (id,owner_id,title,created_at)"
                   " VALUES ('nb','ghost-user','N','2026-01-01T00:00:00Z')")


def test_a_concept_is_global_not_per_notebook(db):
    """One `Ferritin`, referenced from Medicine and Biochemistry alike."""
    db.execute("INSERT INTO concepts (id,canonical_name,normalized_name,first_seen_at)"
               " VALUES ('c1','Ferritin','ferritin',?)", (now_iso(),))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO concepts (id,canonical_name,normalized_name,first_seen_at)"
                   " VALUES ('c2','ferritin','ferritin',?)", (now_iso(),))


def test_user_colour_is_constrained_to_the_three_states(db, user):
    uid, _ = user
    _make_attempt(db, uid)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO attempts (id,question_id,user_id,correct_answer,is_correct,"
                   "user_colour,created_at) VALUES ('att2','q',?,1,1,'PURPLE',?)",
                   (uid, now_iso()))


# ---------------------------------------------------------------------------
# API: auth and scoping
# ---------------------------------------------------------------------------

def test_every_learner_route_requires_a_token(api):
    for method, path in [("GET", "/notebooks"), ("POST", "/notebooks"),
                         ("GET", "/me"), ("GET", "/notebooks/x")]:
        status, body = api.handle(method, path, {}, {}, None)
        assert status == 401, f"{method} {path} was reachable without a token"


def test_register_then_login_then_logout(api):
    status, reg = api.handle("POST", "/auth/register", {},
                             {"email": "new@example.com", "password": "correct-horse"}, None)
    assert status == 201 and reg["token"]

    status, me = api.handle("GET", "/me", {}, {}, reg["token"])
    assert status == 200 and me["email"] == "new@example.com"

    status, login = api.handle("POST", "/auth/login", {},
                               {"email": "new@example.com", "password": "correct-horse"}, None)
    assert status == 200 and login["user_id"] == reg["user_id"]

    api.handle("POST", "/auth/logout", {}, {}, reg["token"])
    assert api.handle("GET", "/me", {}, {}, reg["token"])[0] == 401


def test_duplicate_registration_does_not_confirm_the_address_exists(api):
    body = {"email": "dup@example.com", "password": "correct-horse"}
    assert api.handle("POST", "/auth/register", {}, body, None)[0] == 201
    status, err = api.handle("POST", "/auth/register", {}, body, None)
    assert status == 409
    assert "dup@example.com" not in err["error"]


def test_a_learner_cannot_see_another_learners_notebook(api, db):
    a = db.create_user("a@example.com", "correct-horse")
    b = db.create_user("b@example.com", "correct-horse")
    token_a, token_b = db.issue_token(a), db.issue_token(b)

    _, nb = api.handle("POST", "/notebooks", {}, {"title": "Private"}, token_a)

    assert api.handle("GET", "/notebooks", {}, {}, token_b)[1]["notebooks"] == []
    # 404, not 403: existence is itself information about another user's data.
    assert api.handle("GET", f"/notebooks/{nb['id']}", {}, {}, token_b)[0] == 404
    assert api.handle("GET", f"/notebooks/{nb['id']}", {}, {}, token_a)[0] == 200


def test_notebook_requires_a_title(api, user):
    _, token = user
    assert api.handle("POST", "/notebooks", {}, {"title": "   "}, token)[0] == 400


def test_source_without_an_engine_fails_loudly(api, user):
    """A server with no ingestion worker must say so, not leave the source
    sitting at 'uploaded' forever with no explanation."""
    _, token = user
    _, nb = api.handle("POST", "/notebooks", {}, {"title": "N"}, token)
    status, body = api.handle("POST", f"/notebooks/{nb['id']}/sources", {},
                              {"kind": "text", "text": "hello"}, token)
    assert status == 503
    assert "ingestion engine" in body["error"]
