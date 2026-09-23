"""
Naming a gap after the reveal: `POST /attempts/<id>/gaps`.

Found while writing the dashboard how-to (2026-09-23): the live app records an
attempt the moment the learner picks a colour, because that response carries
the reveal and an attempt is immutable. "Which part failed?" is only
answerable after the reveal, so it came too late to go into the attempt, and
the live session showed that question with no way to answer it. Nothing in the
live app could create a gap, and the weak list -- which is built only from
gaps -- was empty for every real learner.

These tests pin the route that closes that: the learner's own words become a
gap linked to that answer; the attempt row itself is never changed; another
learner's attempt is "no such attempt" (the cross-user half is in
test_cross_user_access.py).
"""

from __future__ import annotations

import json

import pytest

from student.api import StudentAPI
from student.db import now_iso


@pytest.fixture
def w(any_backend):
    db = any_backend.student()
    api = StudentAPI(db)
    tok = api.handle("POST", "/auth/register", {},
                     {"email": "gaps@example.com", "password": "correct-horse"},
                     None)[1]["token"]
    nid = api.handle("POST", "/notebooks", {}, {"title": "Renal", "subject": "renal"},
                     tok)[1]["id"]
    db.execute("INSERT INTO questions (id,primary_notebook_id,family,stem,options_json,"
               "correct_index,rationale,validation_status,generated_by_candidate_id,"
               "prompt_version,generated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               ("q1", nid, "mcq", "stem", json.dumps(["A", "B"]), 0, "r", "approved",
                "c", "v", now_iso()))

    def attempt(colour):
        st, out = api.handle("POST", "/attempts", {},
                             {"question_id": "q1", "user_answer": 1, "user_colour": colour},
                             tok)
        assert st == 201, out
        return out["attempt_id"]

    x = type("W", (), {})()
    x.api, x.db, x.tok, x.attempt = api, db, tok, attempt
    return x


def call(w, method, path, body=None):
    return w.api.handle(method, path, {}, body or {}, w.tok)


@pytest.mark.parametrize("colour", ["RED", "ORANGE"])
def test_a_named_gap_reaches_the_weak_list_linked_to_that_answer(w, colour):
    att = w.attempt(colour)
    assert call(w, "GET", "/gaps")[1]["gaps"] == [], "the fixture started with a gap"
    st, out = call(w, "POST", f"/attempts/{att}/gaps", {"gaps": ["FeNa interpretation"]})
    assert st == 201 and len(out["gaps"]) == 1, out
    gaps = call(w, "GET", "/gaps")[1]["gaps"]
    assert [(g["label"], g["colour"]) for g in gaps] == [("FeNa interpretation", colour)]
    st, qs = call(w, "GET", f"/gaps/{gaps[0]['id']}/questions")
    assert st == 200 and "q1" in json.dumps(qs)


def test_the_attempt_row_is_not_changed(w):
    att = w.attempt("RED")
    before = dict(w.db.query_one("SELECT * FROM attempts WHERE id = ?", (att,)))
    call(w, "POST", f"/attempts/{att}/gaps", {"gaps": ["FeNa interpretation"]})
    assert dict(w.db.query_one("SELECT * FROM attempts WHERE id = ?", (att,))) == before


def test_naming_the_same_gap_again_is_one_gap(w):
    a, b = w.attempt("RED"), w.attempt("ORANGE")
    call(w, "POST", f"/attempts/{a}/gaps", {"gaps": ["FeNa interpretation"]})
    call(w, "POST", f"/attempts/{b}/gaps", {"gaps": ["fena  interpretation"]})
    gaps = call(w, "GET", "/gaps")[1]["gaps"]
    assert len(gaps) == 1, gaps
    links = w.db.query_one("SELECT COUNT(*) n FROM gap_links WHERE gap_id = ?",
                           (gaps[0]["id"],))["n"]
    assert links == 2


def test_a_green_answer_takes_no_gap(w):
    att = w.attempt("GREEN")
    st, out = call(w, "POST", f"/attempts/{att}/gaps", {"gaps": ["anything"]})
    assert st == 400 and "Red or Orange" in out["error"]
    assert call(w, "GET", "/gaps")[1]["gaps"] == []


@pytest.mark.parametrize("body, why", [
    ({"gaps": []}, "at least one"),
    ({}, "at least one"),
    ({"gaps": "FeNa"}, "at least one"),
    ({"gaps": ["  "]}, "needs some text"),
    ({"gaps": [7]}, "needs some text"),
    ({"gaps": ["x" * 121]}, "at most 120"),
    ({"gaps": ["bad\x00"]}, "NUL"),
    ({"gaps": [f"g{i}" for i in range(11)]}, "at most 10"),
])
def test_an_unusable_request_is_refused_and_writes_nothing(w, body, why):
    att = w.attempt("RED")
    st, out = call(w, "POST", f"/attempts/{att}/gaps", body)
    assert st == 400 and why in out["error"], out
    assert call(w, "GET", "/gaps")[1]["gaps"] == []


def test_a_batch_with_one_bad_label_writes_none_of_it(w):
    att = w.attempt("RED")
    st, _ = call(w, "POST", f"/attempts/{att}/gaps", {"gaps": ["fine", "  "]})
    assert st == 400
    assert call(w, "GET", "/gaps")[1]["gaps"] == [], "half a batch was written"


def test_an_unknown_attempt_is_a_404(w):
    st, _ = call(w, "POST", "/attempts/att_nope/gaps", {"gaps": ["x"]})
    assert st == 404
