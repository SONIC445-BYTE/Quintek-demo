"""
`DELETE /account` erases the rows, verified by looking for them afterwards.

WHY THIS FILE EXISTS
--------------------
`tests/test_beta_readiness.py` covers erasure from two directions and neither
of them is this one:

* `TestErasure` calls `accounts.erase()` directly and checks every table
  derived from `tables_holding_user_data` is empty afterwards. Thorough, and
  it never goes near a route.
* `TestTheRealServer.test_erasure_is_reachable_over_http` sends
  `DELETE /account` and asserts `status == 200`. It exists because the route
  was once wired to a server with no `do_DELETE`, so the real answer was 501.

Between them: **nothing checked that the request which returns 200 is the one
that removes the rows.** A handler that authenticated, returned 200 and did
nothing would satisfy both files. "The endpoint returned 200" is a statement
about the response, not about the database.

So this file does the whole thing through the socket -- register, put real
data in, upload a real file, delete, and then go looking. Every assertion
afterwards reads the database and the filesystem directly, not the response
body, because the response body is the thing under suspicion.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from student import accounts
from student.db import Database, new_id, now_iso
from student.server import build_api, make_handler


@pytest.fixture
def live(tmp_path):
    """A real server on a real socket, plus a handle on the same database."""
    db_path = tmp_path / "q.db"
    api = build_api(db_path, with_ai=False)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(api))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield {"origin": f"http://127.0.0.1:{httpd.server_address[1]}",
           "db": Database(db_path), "api": api}
    httpd.shutdown()


def request(base, method, path, body=None, token=None):
    req = urllib.request.Request(
        base + path, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"content-type": "application/json",
                 **({"authorization": "Bearer " + token} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


# ADR-029, RESOLVED 2026-09-24 by severance. These were xfail(strict=True)
# while erasure of a used account was refused; they must now pass.
def ERASURE_BLOCKED(fn):
    return fn


def _populate(db, uid):
    """Real rows across the tables a learner actually fills, so an erasure
    that only reaches `users` cannot pass."""
    nid = new_id("nb")
    db.execute("INSERT INTO notebooks (id,owner_id,title,subject,created_at)"
               " VALUES (?,?,?,?,?)", (nid, uid, "Cardiology", "cardiology", now_iso()))
    sid, cid = new_id("src"), new_id("chk")
    db.execute("INSERT INTO sources (id,notebook_id,kind,filename,status,uploaded_at)"
               " VALUES (?,?,?,?,?,?)", (sid, nid, "pdf", "notes.pdf", "extracted", now_iso()))
    db.execute("INSERT INTO source_chunks (id,source_id,ordinal,text,locator_json,"
               "confidence,extraction_method,needs_review,status)"
               " VALUES (?,?,?,?,?,?,?,?,?)",
               (cid, sid, 1, "A passage.", json.dumps({"page": 1}), 0.9,
                "text", 0, "processed"))
    qid = new_id("q")
    db.execute("INSERT INTO questions (id,primary_notebook_id,family,stem,options_json,"
               "correct_index,rationale,source_id,chunk_id,validation_status,"
               "generated_by_candidate_id,prompt_version,generated_at)"
               " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (qid, nid, "mcq", "A question?", json.dumps(["A", "B", "C", "D"]), 0,
                "Because.", sid, cid, "approved", "cand", "v1", now_iso()))
    db.execute("INSERT INTO attempts (id,user_id,question_id,session_id,user_answer,"
               "correct_answer,is_correct,user_colour,created_at)"
               " VALUES (?,?,?,?,?,?,?,?,?)",
               (new_id("att"), uid, qid, None, 1, 0, 0, "RED", now_iso()))
    # A reminder is NOT inserted here: the test creates one over HTTP, so the
    # row is one the SERVER made rather than one this fixture made, and erasure
    # has to reach those too. (This used to be the `notification_prefs` row
    # registration created; that table is retired -- ADR-031.)
    return {"notebook": nid, "source": sid, "question": qid}


@ERASURE_BLOCKED
def test_the_request_that_returns_200_is_the_one_that_removes_the_rows(live):
    db = live["db"]
    status, out = request(live["origin"], "POST", "/auth/register",
                          {"email": "erase-me@example.com", "password": "correct-horse"})
    assert status in (200, 201), out
    token, uid = out["token"], out["user_id"]
    _populate(db, uid)
    status, rem = request(live["origin"], "POST", "/reminders",
                          {"label": "revise patho", "local_date": "2099-01-01",
                           "local_time": "20:00", "timezone": "Asia/Kolkata"}, token=token)
    assert status == 201, rem

    # The data is really there BEFORE the delete. Without this, an erasure
    # that removes nothing passes every assertion below.
    before = {
        table: db.query_one(f'SELECT COUNT(*) n FROM "{table}" WHERE {column}=?',
                            (uid,))["n"]
        for table, column in accounts.tables_holding_user_data(db)
    }
    # `users` is not in this list -- `tables_holding_user_data` derives the
    # tables that REFERENCE a user, and the row in `users` is handled by
    # `erase` itself. Checked separately below for that reason.
    assert before["notebooks"] == 1, before
    assert before["attempts"] == 1, before
    assert before["sessions_auth"] >= 1, before
    assert before["reminders"] == 1, (
        "the reminder created over HTTP is not in the table erasure reads, so "
        f"this test is no longer covering a server-created row: {before}")
    assert sum(before.values()) >= 4, f"the fixture put almost nothing in: {before}"
    assert db.query_one("SELECT COUNT(*) n FROM users WHERE id=?", (uid,))["n"] == 1

    status, _ = request(live["origin"], "DELETE", "/account", token=token)
    assert status == 200

    # AND NOW LOOK. Derived from the schema, so a table added later is covered
    # the day it appears rather than the day someone remembers to list it.
    for table, column in accounts.tables_holding_user_data(db):
        if table in accounts.RETAINED_ANONYMISED:
            continue
        left = db.query_one(f'SELECT COUNT(*) n FROM "{table}" WHERE {column}=?',
                            (uid,))["n"]
        assert left == 0, (
            f"{table} still holds {left} row(s) for the erased account. "
            f"The request returned 200.")

    assert db.query_one("SELECT COUNT(*) n FROM users WHERE id=?", (uid,))["n"] == 0, (
        "the account row itself survived its own erasure")


@ERASURE_BLOCKED
def test_the_credentials_stop_working_and_cannot_be_used_again(live):
    """An account whose rows are gone must not still authenticate.

    A session row that outlived its user would be a token with no owner --
    and `_user()` looks the session up before it looks the user up.
    """
    origin = live["origin"]
    _, out = request(origin, "POST", "/auth/register",
                     {"email": "gone@example.com", "password": "correct-horse"})
    token = out["token"]
    _populate(live["db"], out["user_id"])

    assert request(origin, "GET", "/me", token=token)[0] == 200

    assert request(origin, "DELETE", "/account", token=token)[0] == 200

    status, _ = request(origin, "GET", "/me", token=token)
    assert status == 401, f"the token still worked after erasure: {status}"

    # And the password cannot be used to get a new one.
    status, _ = request(origin, "POST", "/auth/login",
                        {"email": "gone@example.com", "password": "correct-horse"})
    assert status == 401, f"the erased account could still log in: {status}"


@ERASURE_BLOCKED
def test_the_uploaded_file_leaves_the_disk_too(live, tmp_path):
    """Erasure that clears the database and leaves the PDF is not erasure.

    The file is the most identifying thing a learner gives this system -- it is
    their own notes, possibly a photograph of a page with a patient on it -- and
    it lives outside the database, where a CASCADE cannot reach it.
    """
    db = live["db"]
    storage = Path(live["api"].engine.storage_dir) if live["api"].engine else tmp_path
    storage.mkdir(parents=True, exist_ok=True)

    _, out = request(live["origin"], "POST", "/auth/register",
                     {"email": "filed@example.com", "password": "correct-horse"})
    token, uid = out["token"], out["user_id"]
    ids = _populate(db, uid)

    key = f"{ids['source']}.pdf"
    (storage / key).write_bytes(b"%PDF-1.4 the learner's own notes")
    db.execute("UPDATE sources SET storage_key = ? WHERE id = ?", (key, ids["source"]))
    assert (storage / key).exists()

    # A second learner's file, so "it deleted everything in the directory"
    # cannot pass for "it deleted the right thing".
    other_key = "src_someone_else.pdf"
    (storage / other_key).write_bytes(b"%PDF-1.4 not theirs")

    assert request(live["origin"], "DELETE", "/account", token=token)[0] == 200

    if live["api"].engine is None:
        pytest.skip("no ingestion engine on this build, so no storage directory "
                    "is wired; the database half is covered by the tests above")

    assert not (storage / key).exists(), (
        "the account was erased and the learner's uploaded file is still on disk")
    assert (storage / other_key).exists(), (
        "erasing one account removed another account's file")


def test_erasure_needs_the_callers_own_token(live):
    """One learner cannot erase another.

    `accounts.erase` takes `confirm` and the route passes the CALLER's id, so
    there is no field in which to name somebody else. This pins that: the day
    the route grows a `user_id` body field, this fails.
    """
    origin = live["origin"]
    _, victim = request(origin, "POST", "/auth/register",
                        {"email": "victim@example.com", "password": "correct-horse"})
    _, attacker = request(origin, "POST", "/auth/register",
                          {"email": "attacker@example.com", "password": "correct-horse"})
    _populate(live["db"], victim["user_id"])

    # Whatever the attacker puts in the body, the route reads their own token.
    request(origin, "DELETE", "/account", body={"user_id": victim["user_id"]},
            token=attacker["token"])

    still_there = live["db"].query_one("SELECT COUNT(*) n FROM users WHERE id = ?",
                                       (victim["user_id"],))["n"]
    assert still_there == 1, "one learner erased another learner's account"

    assert request(origin, "GET", "/me", token=victim["token"])[0] == 200


def test_erasure_without_a_token_does_nothing(live):
    origin = live["origin"]
    _, out = request(origin, "POST", "/auth/register",
                     {"email": "safe@example.com", "password": "correct-horse"})
    _populate(live["db"], out["user_id"])

    status, _ = request(origin, "DELETE", "/account")
    assert status == 401, status
    assert live["db"].query_one("SELECT COUNT(*) n FROM users WHERE id = ?",
                                (out["user_id"],))["n"] == 1
