"""
The operator surface, walked as a real admin for the first time (2026-09-24).

Three defects came out of that walk and are pinned here:

  1. A suspended learner who logged in got 200 and a fresh token that failed
     on first use -- a "successful" login they could not use, and a session
     row for an account that was switched off.
  2. Upholding a report changed only the report. A question an operator had
     just confirmed was keyed wrong went on being served as 'approved'.
  3. (Recorded, not changed.) A resolution is recorded under the admin's
     display name, which is not unique; it is the name because the learner
     sees it, and an email address is not theirs to be handed.

And the learner's side of the loop, which had no screen: the bank row now
carries the learner's own report status and why a question is withheld.
"""

from __future__ import annotations

import json

import pytest

from student import accounts
from student.api import StudentAPI
from student.db import new_id, now_iso

ADMIN = "op@quintek.invalid"
ADMIN_PW = "an operator passphrase for tests"


@pytest.fixture
def w(any_backend):
    db = any_backend.student()
    api = StudentAPI(db)
    accounts.bootstrap_admin(db, ADMIN)
    accounts.set_password(db, ADMIN, ADMIN_PW)
    admin = api.handle("POST", "/auth/login", {}, {"email": ADMIN, "password": ADMIN_PW},
                       None)[1]["token"]
    st, reg = api.handle("POST", "/auth/register", {},
                         {"email": "learner@example.com", "password": "correct-horse"}, None)
    nid = api.handle("POST", "/notebooks", {}, {"title": "N", "subject": "renal"},
                     reg["token"])[1]["id"]
    qid = new_id("q")
    db.execute("INSERT INTO questions (id,primary_notebook_id,family,stem,options_json,"
               "correct_index,rationale,validation_status,validation_json,"
               "generated_by_candidate_id,prompt_version,generated_at)"
               " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
               (qid, nid, "mcq", "Low FeNa?", json.dumps(["Intrinsic", "Pre-renal"]), 0, "r",
                "approved", json.dumps({"validator": "kept"}), "c", "v", now_iso()))
    x = type("W", (), {})()
    x.api, x.db, x.admin, x.tok, x.uid, x.qid = api, db, admin, reg["token"], reg["user_id"], qid
    return x


def call(w, method, path, body=None, token=None):
    return w.api.handle(method, path, {}, body or {}, token)


# ---------------------------------------------------------------------------
# 1. Suspension
# ---------------------------------------------------------------------------

def test_a_suspended_learner_is_refused_at_login_with_the_reason(w):
    st, _ = call(w, "POST", f"/admin/users/{w.uid}/suspend", {"reason": "paused for review"},
                 w.admin)
    assert st == 200
    assert call(w, "GET", "/me", token=w.tok)[0] == 401, "the old session survived"
    before = w.db.query_one("SELECT COUNT(*) n FROM sessions_auth WHERE user_id = ?",
                            (w.uid,))["n"]
    st, out = call(w, "POST", "/auth/login",
                   {"email": "learner@example.com", "password": "correct-horse"})
    assert st == 403 and "paused for review" in out["error"], out
    assert "token" not in out
    assert w.db.query_one("SELECT COUNT(*) n FROM sessions_auth WHERE user_id = ?",
                          (w.uid,))["n"] == before, "a session was issued to a suspended account"


def test_a_wrong_password_does_not_reveal_a_suspension(w):
    call(w, "POST", f"/admin/users/{w.uid}/suspend", {"reason": "paused"}, w.admin)
    st, out = call(w, "POST", "/auth/login",
                   {"email": "learner@example.com", "password": "not-the-password"})
    assert st == 401 and "suspended" not in out["error"]


def test_reinstating_lets_the_learner_back_in(w):
    call(w, "POST", f"/admin/users/{w.uid}/suspend", {"reason": "paused"}, w.admin)
    assert call(w, "POST", f"/admin/users/{w.uid}/reinstate", {}, w.admin)[0] == 200
    st, out = call(w, "POST", "/auth/login",
                   {"email": "learner@example.com", "password": "correct-horse"})
    assert st == 200
    assert call(w, "GET", "/me", token=out["token"])[0] == 200


# ---------------------------------------------------------------------------
# 2. An upheld report withdraws its question
# ---------------------------------------------------------------------------

def report(w, kind="wrong_answer_keyed"):
    st, out = call(w, "POST", f"/questions/{w.qid}/reports", {"kind": kind, "note": "n"}, w.tok)
    assert st == 201, out
    return out["report_id"]


def resolve(w, rid, resolution, **extra):
    return call(w, "POST", f"/ops/reports/{rid}", {"resolution": resolution, **extra}, w.admin)


def status_of(w):
    return w.db.query_one("SELECT validation_status, validation_json FROM questions"
                          " WHERE id = ?", (w.qid,))


def test_upholding_a_report_takes_the_question_out_of_service(w):
    rid = report(w)
    st, out = resolve(w, rid, "upheld", note="the key is wrong")
    assert st == 200 and out["question_withdrawn"] is True
    row = status_of(w)
    assert row["validation_status"] == "flagged"
    record = json.loads(row["validation_json"])
    assert record["validator"] == "kept", "the validator's own record was overwritten"
    assert record["withdrawn"] == {"report_id": rid, "by": "Operator",
                                   "at": record["withdrawn"]["at"],
                                   "previous_status": "approved"}
    # Everywhere the learner could meet it:
    assert call(w, "GET", f"/questions/{w.qid}", token=w.tok)[0] == 403
    st, s = call(w, "POST", "/revision/sessions", {"count": 5, "strategy": "unseen"}, w.tok)
    assert w.qid not in (s.get("question_ids") or [])
    st, attempt = call(w, "POST", "/attempts", {"question_id": w.qid, "user_answer": 1,
                                                "user_colour": "RED"}, w.tok)
    assert st == 403


@pytest.mark.parametrize("resolution", ["rejected", "acknowledged"])
def test_other_resolutions_leave_the_question_in_service(w, resolution):
    rid = report(w)
    st, out = resolve(w, rid, resolution)
    assert st == 200 and out["question_withdrawn"] is False
    assert status_of(w)["validation_status"] == "approved"
    assert call(w, "GET", f"/questions/{w.qid}", token=w.tok)[0] == 200


def test_withdrawal_is_one_way(w):
    rid = report(w)
    resolve(w, rid, "upheld")
    resolve(w, rid, "rejected")
    assert status_of(w)["validation_status"] == "flagged", (
        "re-resolving the report put a withdrawn question back in service")


# ---------------------------------------------------------------------------
# 3. Who resolved it
# ---------------------------------------------------------------------------

def test_an_explicit_reviewer_is_still_recorded(w):
    rid = report(w)
    st, out = resolve(w, rid, "upheld", resolved_by="Dr A. Reviewer")
    assert out["resolved_by"] == "Dr A. Reviewer"


# ---------------------------------------------------------------------------
# The learner's side
# ---------------------------------------------------------------------------

def bank_row(w):
    st, out = call(w, "GET", "/questions", token=w.tok)
    return next(q for q in out["questions"] if q["id"] == w.qid)


def test_the_bank_row_carries_the_learners_report_and_the_withdrawal(w):
    row = bank_row(w)
    assert row["my_report"] is None and row["withheld"] is False
    rid = report(w)
    assert bank_row(w)["my_report"] == "open"
    resolve(w, rid, "upheld")
    row = bank_row(w)
    assert row["my_report"] == "upheld"
    assert row["withheld"] is True and row["withheld_reason"] == "withdrawn"
    assert row["stem"] is None


def test_an_unvalidated_question_is_withheld_for_that_reason(w):
    w.db.execute("UPDATE questions SET validation_status = 'pending' WHERE id = ?", (w.qid,))
    row = bank_row(w)
    assert row["withheld"] is True and row["withheld_reason"] == "unvalidated"


def test_the_bank_does_not_show_another_learners_report_as_mine(w):
    """`my_report` is the CALLER's. Reports are only possible on one's own
    questions today, so this plants one directly to prove the filter exists."""
    st, reg = call(w, "POST", "/auth/register",
                   {"email": "other@example.com", "password": "correct-horse"})
    w.db.execute("INSERT INTO question_reports (id,question_id,user_id,kind,note,"
                 "provenance_json,resolution,created_at) VALUES (?,?,?,?,?,?,?,?)",
                 (new_id("rpt"), w.qid, reg["user_id"], "other", "", "{}", "open", now_iso()))
    assert bank_row(w)["my_report"] is None
