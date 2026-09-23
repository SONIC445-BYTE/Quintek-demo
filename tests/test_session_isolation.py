"""
A revision session serves only the learner's own questions.

ADR-030 -- found 2026-09-23, reported under the disclosure stop condition,
FIXED on the owner's instruction. These tests were `xfail(strict=True)` while
the defect was held open; they are plain tests now and must stay passing.

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
    question("alice-pending", na, status="pending")   # OWN and unvalidated: the
    question("bob-q", nb)                               # owner join cannot hide it,
    question("bob-pending", nb, status="pending")       # only the status filter can

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


@pytest.mark.parametrize("strategy", ["adaptive", "orange"])
def test_a_session_contains_only_the_learners_own_questions(world, strategy):
    st, session = world.api.handle("POST", "/revision/sessions", {},
                                   {"count": 10, "strategy": strategy}, world.alice)
    assert st == 201, session
    foreign = [q for q in session["question_ids"] if q.startswith("bob")]
    assert foreign == [], f"Alice's {strategy} session was built with Bob's {foreign}"


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


def test_an_unvalidated_question_is_never_served(world):
    st, session = world.api.handle("POST", "/revision/sessions", {},
                                   {"count": 10, "strategy": "adaptive"}, world.alice)
    assert "bob-pending" not in session["question_ids"]
    assert "alice-pending" not in session["question_ids"], (
        "Alice's OWN pending question was served -- there is no author exception")


def test_related_concepts_do_not_name_another_learners_material(world):
    st, body = world.api.handle("GET", f"/concepts/{world.shared}", {}, {}, world.alice)
    names = [r.get("canonical_name") for r in body.get("related", [])]
    assert "A diagnosis only in Bob's notes" not in names, (
        f"Alice's concept screen lists a concept from Bob's material: {names}")


# ---------------------------------------------------------------------------
# The owner's decision: only validated questions are ever served, no exception
# ---------------------------------------------------------------------------
#
# Found while fixing ADR-030 and closed under the same decision. Every path
# below is OWNER-scoped -- nobody else's question was reachable -- but each one
# handed a learner the content of a question the validator had not passed:
# `unseen` and step 8 of the adaptive session had no status filter at all, and
# the bank, the single-question route and the attempt reveal returned stems,
# answers and rationales for pending and rejected questions alike.

@pytest.fixture
def unvalidated(tmp_path):
    db = Database(tmp_path / "u.db")
    api = StudentAPI(db)
    tok = api.handle("POST", "/auth/register", {},
                     {"email": "author@example.com", "password": "correct-horse"},
                     None)[1]["token"]
    nid = api.handle("POST", "/notebooks", {}, {"title": "Mine", "subject": "cardiology"},
                     tok)[1]["id"]
    for qid, status in (("ok", "approved"), ("pend", "pending"),
                        ("flag", "flagged"), ("rej", "rejected")):
        db.execute("INSERT INTO questions (id,primary_notebook_id,family,stem,options_json,"
                   "correct_index,rationale,validation_status,generated_by_candidate_id,"
                   "prompt_version,generated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (qid, nid, "mcq", "CONTENT OF " + qid, json.dumps(["A", "B"]), 1,
                    "RATIONALE OF " + qid, status, "c", "v", now_iso()))
    w = type("W", (), {})()
    w.api, w.tok = api, tok
    return w


@pytest.mark.parametrize("strategy", ["adaptive", "unseen"])
def test_only_approved_questions_enter_a_session(unvalidated, strategy):
    st, s = unvalidated.api.handle("POST", "/revision/sessions", {},
                                   {"count": 10, "strategy": strategy}, unvalidated.tok)
    assert st == 201, s
    assert s["question_ids"] == ["ok"], (
        f"the {strategy} session selected {s['question_ids']}; only 'ok' is approved")


@pytest.mark.parametrize("qid", ["pend", "flag", "rej"])
def test_an_unvalidated_questions_content_is_not_returned_directly(unvalidated, qid):
    st, body = unvalidated.api.handle("GET", f"/questions/{qid}", {}, {}, unvalidated.tok)
    assert st == 403, body
    assert "CONTENT OF" not in json.dumps(body)


@pytest.mark.parametrize("qid", ["pend", "flag", "rej"])
def test_an_unvalidated_question_cannot_be_attempted_for_its_reveal(unvalidated, qid):
    st, body = unvalidated.api.handle("POST", "/attempts", {},
                                      {"question_id": qid, "user_answer": 0,
                                       "user_colour": "RED"}, unvalidated.tok)
    assert st == 403, body
    assert "RATIONALE OF" not in json.dumps(body)
    assert "correct_answer" not in json.dumps(body)


def test_the_bank_lists_unvalidated_questions_without_their_content(unvalidated):
    st, body = unvalidated.api.handle("GET", "/questions", {}, {}, unvalidated.tok)
    by = {q["id"]: q for q in body["questions"]}
    # Still LISTED: the learner is told their material produced four questions.
    assert set(by) == {"ok", "pend", "flag", "rej"}
    assert by["ok"]["stem"] == "CONTENT OF ok" and by["ok"]["withheld"] is False
    for qid in ("pend", "flag", "rej"):
        assert by[qid]["stem"] is None and by[qid]["withheld"] is True
    assert "CONTENT OF pend" not in json.dumps(body)


def test_a_question_rejected_mid_session_is_not_served(unvalidated):
    """Eligibility is re-checked at serve time, not only when the session was
    built -- a report can flag a question between the two."""
    api, tok = unvalidated.api, unvalidated.tok
    st, s = api.handle("POST", "/revision/sessions", {}, {"count": 5}, tok)
    api.db.execute("UPDATE questions SET validation_status='flagged' WHERE id='ok'")
    st, nxt = api.handle("GET", "/revision/next", {"session": s["session_id"]}, {}, tok)
    assert not nxt.get("question"), f"a flagged question was served: {nxt}"


def test_the_concept_query_itself_is_owner_scoped_and_approved_only(world):
    """The session is protected twice: this query is scoped, AND every step
    passes through an eligibility check. Tested separately so that removing
    either layer fails a test, rather than being silently covered by the
    other."""
    uid = world.api.db.query_one("SELECT id FROM users WHERE email='alice@example.com'")["id"]
    got = world.api.revision._questions_for_concepts(uid, [world.shared], set())
    assert got == ["alice-q"], got
