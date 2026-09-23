"""
A revision session serves only the learner's own questions.

OPEN DISCLOSURE ROUTE -- ADR-030. Found 2026-09-23, reported, NOT FIXED.

Held unfixed under the standing stop condition that a disclosure route is
shown to the owner before it is closed. Every test here is `xfail(strict=True)`
so it FAILS the moment the fix lands and cannot be left behind.

THE DEFECT
----------
`RevisionEngine._questions_for_concepts` selects every question tagged with a
concept, from anyone's notebook:

    FROM questions q JOIN question_concepts qc ON qc.question_id = q.id
    WHERE qc.concept_id IN (...)

Concepts are GLOBAL rows -- `normalized_name` is UNIQUE -- so any two learners
whose material yields the same concept share its id. The adaptive strategy
(the default) and the colour strategies all go through this function, so a
learner's session is built partly from other learners' questions, and
`/revision/next` hands over the stem and options.

It also selects `validation_status IN ('approved', 'pending')`, so a PENDING
question -- one the validator has not passed -- can be served, to its author
and to other learners alike.

WHY THE CROSS-USER META-TEST DID NOT CATCH IT
---------------------------------------------
`tests/test_cross_user_access.py` does run sessions across two users. Its
fixture never gives both users the SAME concept, so the concept-driven
selection steps never had another learner's question to find. The coverage
existed; the fixture shape made it vacuous -- the third time on this project a
discovery predicate or fixture, not the code, has been the gap.

A SECOND, SMALLER ROUTE THROUGH THE SAME DESIGN
-----------------------------------------------
`GET /concepts/<id>` returns `related` from `concept_relationships`, which is
also global. A concept name that exists only in another learner's material is
listed to anyone who holds a concept it is linked to.
"""

from __future__ import annotations

import json

import pytest

from student.api import StudentAPI
from student.db import Database, new_id, now_iso

OPEN = pytest.mark.xfail(strict=True, reason=(
    "ADR-030: sessions select questions by GLOBAL concept with no owner scope. "
    "Reported under the disclosure stop condition; not fixed without the "
    "owner's go-ahead. strict=True so this fails once it is fixed."))


@pytest.fixture
def world(tmp_path):
    db = Database(tmp_path / "q.db")
    api = StudentAPI(db)

    def reg(email):
        return api.handle("POST", "/auth/register", {},
                          {"email": email, "password": "correct-horse"}, None)[1]["token"]

    alice, bob = reg("alice@example.com"), reg("bob@example.com")

    def notebook(tok, title):
        return api.handle("POST", "/notebooks", {},
                          {"title": title, "subject": "cardiology"}, tok)[1]["id"]

    na, nb = notebook(alice, "Alice"), notebook(bob, "Bob, private")

    def concept(name):
        cid = new_id("cpt")
        db.execute("INSERT INTO concepts (id,canonical_name,normalized_name,subject,"
                   "first_seen_at) VALUES (?,?,?,?,?)",
                   (cid, name, name.lower(), "cardiology", now_iso()))
        return cid

    shared = concept("Heart failure")               # both learners' material yields it
    bob_only = concept("A diagnosis only in Bob's notes")
    for n, c in ((na, shared), (nb, shared), (nb, bob_only)):
        db.execute("INSERT INTO notebook_concepts (notebook_id,concept_id) VALUES (?,?)", (n, c))
    db.execute("INSERT INTO concept_relationships (id,source_concept_id,target_concept_id,"
               "relation_type,confidence,created_at) VALUES (?,?,?,?,?,?)",
               (new_id("rel"), shared, bob_only, "related to", 0.9, now_iso()))

    def question(qid, nid, status="approved"):
        db.execute("INSERT INTO questions (id,primary_notebook_id,family,stem,options_json,"
                   "correct_index,rationale,validation_status,generated_by_candidate_id,"
                   "prompt_version,generated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (qid, nid, "mcq", "stem " + qid, json.dumps(["A", "B"]), 0, "r", status,
                    "c", "v", now_iso()))
        db.execute("INSERT INTO question_concepts (question_id,concept_id,role)"
                   " VALUES (?,?,?)", (qid, shared, "target"))

    question("alice-q", na)
    question("bob-q", nb)
    question("bob-pending", nb, status="pending")

    w = type("W", (), {})()
    w.api, w.alice, w.bob, w.shared = api, alice, bob, shared
    return w


def test_the_fixture_really_has_a_shared_concept(world):
    """Positive control. Without a shared concept every test below passes
    against the defect, which is exactly how the meta-test missed it."""
    st, body = world.api.handle("GET", f"/concepts/{world.shared}", {}, {}, world.alice)
    assert st == 200
    st, body = world.api.handle("GET", f"/concepts/{world.shared}", {}, {}, world.bob)
    assert st == 200


@OPEN
@pytest.mark.parametrize("strategy", ["adaptive", "orange"])
def test_a_session_contains_only_the_learners_own_questions(world, strategy):
    st, session = world.api.handle("POST", "/revision/sessions", {},
                                   {"count": 10, "strategy": strategy}, world.alice)
    assert st == 201, session
    foreign = [q for q in session["question_ids"] if q.startswith("bob")]
    assert foreign == [], f"Alice's {strategy} session was built with Bob's {foreign}"


@OPEN
def test_next_never_hands_over_another_learners_stem(world):
    st, session = world.api.handle("POST", "/revision/sessions", {},
                                   {"count": 10, "strategy": "adaptive"}, world.alice)
    served = []
    for _ in range(5):
        st, nxt = world.api.handle("GET", "/revision/next",
                                   {"session": session["session_id"]}, {}, world.alice)
        if nxt.get("finished") or not nxt.get("question"):
            break
        q = nxt["question"]
        served.append(q["question_id"])
        world.api.handle("POST", "/attempts", {},
                         {"question_id": q["question_id"], "user_answer": 0,
                          "user_colour": "GREEN", "session_id": session["session_id"]},
                         world.alice)
        if q["question_id"].startswith("bob"):
            break          # the refused attempt would loop on it forever
    assert not any(s.startswith("bob") for s in served), (
        f"/revision/next handed Alice another learner's question: {served}")


@OPEN
def test_an_unvalidated_question_is_never_served(world):
    st, session = world.api.handle("POST", "/revision/sessions", {},
                                   {"count": 10, "strategy": "adaptive"}, world.alice)
    assert "bob-pending" not in session["question_ids"]


@OPEN
def test_related_concepts_do_not_name_another_learners_material(world):
    st, body = world.api.handle("GET", f"/concepts/{world.shared}", {}, {}, world.alice)
    names = [r.get("canonical_name") for r in body.get("related", [])]
    assert "A diagnosis only in Bob's notes" not in names, (
        f"Alice's concept screen lists a concept from Bob's material: {names}")
