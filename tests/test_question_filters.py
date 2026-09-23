"""
The question list's three filters actually filter.

WHY THIS FILE EXISTS
--------------------
`StudentAPI.question_bank` appended its filters as bare `AND ...` fragments,
so each one attached itself to whichever JOIN came last. While that was the
INNER join to `notebooks` it happened to work. A LEFT JOIN to `source_chunks`
was added after it (commit ef523fb, to carry `needs_review` to the list), and
from then on all three filters landed on a LEFT join's ON clause -- where a
false condition keeps the row and only nulls the chunk columns.

Nothing failed. Every existing test that touched `question_bank` had one
notebook, one concept and one status in play, so "filtered" and "not
filtered" returned the same rows. It was found by a render test that gave one
learner two concepts and noticed a question on the wrong one.

So every case below has at least one row the filter must EXCLUDE, and asserts
on its absence. A filter test with nothing to filter out passes against a
filter that does nothing.
"""

from __future__ import annotations

import json

import pytest

from student.api import StudentAPI
from student.db import Database, new_id, now_iso


@pytest.fixture
def world(tmp_path):
    db = Database(tmp_path / "q.db")
    api = StudentAPI(db)

    def register(email):
        return api.handle("POST", "/auth/register", {},
                          {"email": email, "password": "correct-horse"}, None)[1]["token"]

    a, b = register("a@example.com"), register("b@example.com")

    def notebook(tok, title):
        return api.handle("POST", "/notebooks", {},
                          {"title": title, "subject": "cardiology"}, tok)[1]["id"]

    n1, n2, nb = notebook(a, "A1"), notebook(a, "A2"), notebook(b, "B1")
    cx, cy = new_id("cpt"), new_id("cpt")
    for cid, name in ((cx, "X"), (cy, "Y")):
        db.execute("INSERT INTO concepts (id,canonical_name,normalized_name,subject,"
                   "first_seen_at) VALUES (?,?,?,?,?)",
                   (cid, name, name.lower(), "cardiology", now_iso()))

    # A chunk on one question only, so the LEFT JOIN that caused this has rows
    # on both sides of it.
    sid, ck = new_id("src"), new_id("chk")
    db.execute("INSERT INTO sources (id,notebook_id,kind,filename,status,uploaded_at)"
               " VALUES (?,?,?,?,?,?)", (sid, n1, "pdf", "n.pdf", "extracted", now_iso()))
    db.execute("INSERT INTO source_chunks (id,source_id,ordinal,text,locator_json,"
               "confidence,extraction_method,needs_review,status)"
               " VALUES (?,?,?,?,?,?,?,?,?)",
               (ck, sid, 1, "t", json.dumps({"page": 1}), 0.4, "ocr", 1, "processed"))

    def question(qid, nid, status, concept=None, chunk=None):
        db.execute("INSERT INTO questions (id,primary_notebook_id,family,stem,options_json,"
                   "correct_index,rationale,chunk_id,validation_status,"
                   "generated_by_candidate_id,prompt_version,generated_at)"
                   " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (qid, nid, "mcq", "s", json.dumps(["A", "B"]), 0, "r", chunk, status,
                    "c", "v", now_iso()))
        if concept:
            db.execute("INSERT INTO question_concepts (question_id,concept_id,role)"
                       " VALUES (?,?,?)", (qid, concept, "target"))
            db.execute("INSERT OR IGNORE INTO notebook_concepts (notebook_id,concept_id)"
                       " VALUES (?,?)", (nid, concept))

    question("a1-approved-x", n1, "approved", cx, chunk=ck)
    question("a1-pending-y", n1, "pending", cy)
    question("a2-approved-y", n2, "approved", cy)
    question("b-approved-x", nb, "approved", cx)     # another learner's

    w = type("W", (), {})()
    w.api, w.a, w.b, w.n1, w.n2, w.cx, w.cy = api, a, b, n1, n2, cx, cy
    return w


def ids(response):
    status, body = response
    assert status == 200, body
    return sorted(q["id"] for q in body["questions"])


def test_status_filter_excludes_other_statuses(world):
    got = ids(world.api.handle("GET", "/questions", {"status": "approved"}, {}, world.a))
    assert got == ["a1-approved-x", "a2-approved-y"]
    assert "a1-pending-y" not in got


def test_notebook_filter_excludes_the_learners_other_notebooks(world):
    got = ids(world.api.handle("GET", f"/notebooks/{world.n1}/questions", {}, {}, world.a))
    assert got == ["a1-approved-x", "a1-pending-y"]
    assert "a2-approved-y" not in got, (
        "a notebook's question list included a question from another notebook")


def test_concept_filter_excludes_questions_on_other_concepts(world):
    got = ids(world.api.handle("GET", "/questions", {"concept": world.cx}, {}, world.a))
    assert got == ["a1-approved-x"]


def test_concept_detail_lists_only_that_concepts_questions(world):
    """The screen where this was found."""
    status, body = world.api.handle("GET", f"/concepts/{world.cy}", {}, {}, world.a)
    assert status == 200, body
    assert sorted(q["id"] for q in body["questions"]) == ["a1-pending-y", "a2-approved-y"]


def test_filters_combine(world):
    got = ids(world.api.handle("GET", f"/notebooks/{world.n1}/questions",
                               {"status": "approved"}, {}, world.a))
    assert got == ["a1-approved-x"]


def test_no_filter_still_returns_everything_the_learner_owns_and_nothing_else(world):
    got = ids(world.api.handle("GET", "/questions", {}, {}, world.a))
    assert got == ["a1-approved-x", "a1-pending-y", "a2-approved-y"]
    assert "b-approved-x" not in got


def test_the_chunk_join_still_carries_needs_review(world):
    """The LEFT JOIN is still wanted; it was the filters' placement that was
    wrong. A question with a chunk carries its flag; one without carries None."""
    body = world.api.handle("GET", "/questions", {}, {}, world.a)[1]["questions"]
    by = {q["id"]: q for q in body}
    assert by["a1-approved-x"]["chunk_needs_review"] is True
    assert by["a1-pending-y"]["chunk_needs_review"] is None
