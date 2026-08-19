"""
Phases 7, 8 and 9: knowledge state, priority, and adaptive revision.
"""

from __future__ import annotations

import json

import pytest

from student.concepts import ConceptStore
from student.db import Database, now_iso
from student.knowledge import GREEN, ORANGE, RED, KnowledgeStore, derive_concept_colour
from student.revision import PriorityEngine, RevisionEngine


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "q.db")


@pytest.fixture
def world(db):
    """One learner, one notebook, two concepts, four questions."""
    uid = db.create_user("l@example.com", "correct-horse")
    db.execute("INSERT INTO notebooks (id,owner_id,title,subject,created_at)"
               " VALUES ('nb',?,'Renal','Medicine',?)", (uid, now_iso()))
    db.execute("INSERT INTO sources (id,notebook_id,kind,status,uploaded_at)"
               " VALUES ('src','nb','text','extracted',?)", (now_iso(),))
    db.execute("INSERT INTO source_chunks (id,source_id,ordinal,text,locator_json,status)"
               " VALUES ('chk','src',1,'FeNa below 1% indicates pre-renal AKI.',"
               "'{\"page\": 3}','processed')")
    store = ConceptStore(db)
    aki = store.resolve_or_create("Pre-renal AKI", subject="Medicine")
    fena = store.resolve_or_create("FeNa interpretation", subject="Medicine")
    for cid in (aki, fena):
        store.link_to_notebook("nb", cid)
    store.relate(aki, fena, "measured_by")

    for i, cid in enumerate([aki, aki, fena, fena], start=1):
        qid = f"q{i}"
        db.execute("INSERT INTO questions (id,primary_notebook_id,stem,options_json,"
                   "correct_index,source_id,chunk_id,validation_status,generated_at)"
                   " VALUES (?,'nb',?,'[\"a\",\"b\"]',1,'src','chk','approved',?)",
                   (qid, f"Question {i}?", now_iso()))
        db.execute("INSERT INTO question_concepts (question_id,concept_id,role)"
                   " VALUES (?,?, 'target')", (qid, cid))
    return uid, aki, fena


# ---------------------------------------------------------------------------
# Phase 7: the learner owns the colour
# ---------------------------------------------------------------------------

def test_the_colour_must_be_supplied_and_is_never_inferred(db, world):
    uid, _, _ = world
    ks = KnowledgeStore(db)
    with pytest.raises(ValueError, match="never infers"):
        ks.record_attempt(user_id=uid, question_id="q1", user_answer=1, user_colour="")


def test_correctness_and_colour_are_recorded_independently(db, world):
    """A lucky guess is correct and RED; a slip is wrong and GREEN. Collapsing
    the two loses the distinction the product is built on."""
    uid, _, _ = world
    ks = KnowledgeStore(db)
    lucky = ks.record_attempt(user_id=uid, question_id="q1", user_answer=1, user_colour=RED)
    slip = ks.record_attempt(user_id=uid, question_id="q2", user_answer=0, user_colour=GREEN)
    assert lucky["is_correct"] is True and lucky["user_colour"] == RED
    assert slip["is_correct"] is False and slip["user_colour"] == GREEN


def test_one_red_does_not_condemn_a_concept(db, world):
    """Being wrong once is uncertainty; being wrong repeatedly is a gap."""
    assert derive_concept_colour([RED]) == ORANGE
    assert derive_concept_colour([RED, GREEN, GREEN]) == ORANGE
    assert derive_concept_colour([RED, RED]) == RED
    assert derive_concept_colour([RED, GREEN, RED]) == RED
    assert derive_concept_colour([GREEN, GREEN]) == GREEN
    assert derive_concept_colour([GREEN, GREEN, RED]) == ORANGE
    assert derive_concept_colour([]) == ORANGE, "unknown is not 'fine'"


def test_concept_state_follows_the_evidence_rule(db, world):
    uid, aki, _ = world
    ks = KnowledgeStore(db)
    ks.record_attempt(user_id=uid, question_id="q1", user_answer=0, user_colour=RED)
    assert ks.concept_state(uid, aki)["colour"] == ORANGE
    ks.record_attempt(user_id=uid, question_id="q2", user_answer=0, user_colour=RED)
    assert ks.concept_state(uid, aki)["colour"] == RED


def test_a_gap_links_back_to_its_full_evidence_chain(db, world):
    """'You are weak at anaemia' becomes 'this question, in this notebook,
    from this passage'."""
    uid, _, _ = world
    ks = KnowledgeStore(db)
    ks.record_attempt(user_id=uid, question_id="q1", user_answer=0, user_colour=RED,
                      gaps=["FeNa interpretation"])
    gaps = ks.gaps(uid)
    assert len(gaps) == 1 and gaps[0]["label"] == "FeNa interpretation"

    evidence = ks.gap_evidence(uid, gaps[0]["id"])
    assert len(evidence) == 1
    e = evidence[0]
    assert e["question_id"] == "q1"
    assert e["notebook_title"] == "Renal"
    assert "FeNa below 1%" in e["passage"], "the source passage must be reachable"


def test_a_recurring_gap_reopens_rather_than_duplicating(db, world):
    uid, _, _ = world
    ks = KnowledgeStore(db)
    ks.record_attempt(user_id=uid, question_id="q1", user_answer=0, user_colour=RED,
                      gaps=["FeNa interpretation"])
    gap_id = ks.gaps(uid)[0]["id"]
    ks.resolve_gap(uid, gap_id)
    assert ks.gaps(uid) == []

    ks.record_attempt(user_id=uid, question_id="q2", user_answer=0, user_colour=RED,
                      gaps=["fena interpretation"])   # same gap, different casing
    reopened = ks.gaps(uid)
    assert len(reopened) == 1 and reopened[0]["id"] == gap_id
    assert reopened[0]["evidence_count"] == 2


def test_a_red_grade_schedules_a_return_sooner_than_a_green_one(db, world):
    """SM-2 is graded on the learner's colour, not raw correctness: a
    guessed-correct answer graded RED must come back soon."""
    uid, _, _ = world
    ks = KnowledgeStore(db)
    ks.record_attempt(user_id=uid, question_id="q1", user_answer=1, user_colour=RED)
    ks.record_attempt(user_id=uid, question_id="q2", user_answer=1, user_colour=GREEN)
    red_due = db.query_one("SELECT due_at FROM revision_state WHERE question_id='q1'")["due_at"]
    green_due = db.query_one("SELECT due_at FROM revision_state WHERE question_id='q2'")["due_at"]
    assert red_due <= green_due


# ---------------------------------------------------------------------------
# Phase 8: deterministic priority
# ---------------------------------------------------------------------------

def test_priority_is_deterministic(db, world):
    uid, _, _ = world
    ks, pe = KnowledgeStore(db), PriorityEngine(db)
    ks.record_attempt(user_id=uid, question_id="q1", user_answer=0, user_colour=RED,
                      gaps=["FeNa interpretation"])
    first = [(r["concept_id"], r["priority_score"]) for r in pe.ranked(uid)]
    second = [(r["concept_id"], r["priority_score"]) for r in pe.ranked(uid)]
    assert first == second, "the same inputs must always give the same order"


def test_a_failing_concept_with_an_open_gap_outranks_a_healthy_one(db, world):
    uid, aki, fena = world
    ks, pe = KnowledgeStore(db), PriorityEngine(db)
    for q in ("q1", "q2"):
        ks.record_attempt(user_id=uid, question_id=q, user_answer=0, user_colour=RED,
                          gaps=["FeNa interpretation"])
    for q in ("q3", "q4"):
        ks.record_attempt(user_id=uid, question_id=q, user_answer=1, user_colour=GREEN)

    ranked = pe.ranked(uid)
    assert ranked[0]["concept_id"] == aki
    assert ranked[0]["priority_score"] > ranked[-1]["priority_score"]


def test_priority_explains_itself_with_named_signals(db, world):
    """The concept screen has to show why a concept sits where it does, and an
    explanation of named signals is checkable in a way an opinion is not."""
    uid, aki, _ = world
    ks, pe = KnowledgeStore(db), PriorityEngine(db)
    for q in ("q1", "q2"):
        ks.record_attempt(user_id=uid, question_id=q, user_answer=0, user_colour=RED,
                          gaps=["FeNa interpretation"])

    why = {r["signal"] for r in pe.explain(uid, aki)["why"]}
    assert {"colour", "wrong answers", "repeated failure", "unresolved gaps"} <= why


def test_retrieval_success_lowers_priority(db, world):
    uid, aki, _ = world
    ks, pe = KnowledgeStore(db), PriorityEngine(db)
    before = pe.explain(uid, aki)["priority_score"]
    for q in ("q1", "q2"):
        ks.record_attempt(user_id=uid, question_id=q, user_answer=1, user_colour=GREEN)
    assert pe.explain(uid, aki)["priority_score"] < before


# ---------------------------------------------------------------------------
# Phase 9: adaptive revision
# ---------------------------------------------------------------------------

def test_the_session_leads_with_red_gap_concepts(db, world):
    uid, aki, _ = world
    ks, re_ = KnowledgeStore(db), RevisionEngine(db)
    for q in ("q1", "q2"):
        ks.record_attempt(user_id=uid, question_id=q, user_answer=0, user_colour=RED,
                          gaps=["Pre-renal AKI recognition"])

    picked = re_.select_questions(uid, count=4, strategy="adaptive")
    first_concept = db.query_one(
        "SELECT concept_id FROM question_concepts WHERE question_id = ?", (picked[0],))
    assert first_concept["concept_id"] == aki


def test_new_questions_are_preferred_over_re_serving_a_failed_one(db, world):
    """A learner who has memorised that D is the answer has learned the
    question, not the concept."""
    uid, aki, _ = world
    ks, re_ = KnowledgeStore(db), RevisionEngine(db)
    ks.record_attempt(user_id=uid, question_id="q1", user_answer=0, user_colour=RED)
    ks.record_attempt(user_id=uid, question_id="q1", user_answer=0, user_colour=RED)

    picked = re_.select_questions(uid, count=1, strategy="adaptive")
    assert picked == ["q2"], "the unseen question on the same concept should come first"


def test_weak_plus_full_section_reaches_neighbouring_concepts(db, world):
    """If one concept is weak, the session covers the section around it rather
    than drilling a single item."""
    uid, aki, fena = world
    ks, re_ = KnowledgeStore(db), RevisionEngine(db)
    for q in ("q1", "q2"):
        ks.record_attempt(user_id=uid, question_id=q, user_answer=0, user_colour=RED)

    picked = re_.select_questions(uid, count=4, strategy="adaptive")
    concepts = {r["concept_id"] for r in db.query(
        "SELECT DISTINCT concept_id FROM question_concepts WHERE question_id IN "
        f"({','.join('?' for _ in picked)})", tuple(picked))}
    assert fena in concepts, "the related concept should be pulled in for coverage"


def test_explicit_strategies_narrow_the_selection(db, world):
    uid, aki, _ = world
    ks, re_ = KnowledgeStore(db), RevisionEngine(db)
    for q in ("q1", "q2"):
        ks.record_attempt(user_id=uid, question_id=q, user_answer=0, user_colour=RED)

    red_only = re_.select_questions(uid, count=10, strategy="red")
    for qid in red_only:
        cid = db.query_one("SELECT concept_id FROM question_concepts WHERE question_id=?",
                           (qid,))["concept_id"]
        assert ks.concept_state(uid, cid)["colour"] == RED

    with pytest.raises(ValueError, match="unknown strategy"):
        re_.select_questions(uid, count=5, strategy="vibes")


def test_a_session_records_the_exact_questions_it_served(db, world):
    """Otherwise the analysis afterwards cannot be re-derived and the record is
    anecdote."""
    uid, _, _ = world
    re_ = RevisionEngine(db)
    session = re_.start_session(uid, count=3, strategy="adaptive")
    row = db.query_one("SELECT * FROM revision_sessions WHERE id = ?", (session["session_id"],))
    assert json.loads(row["selected_question_ids_json"]) == session["question_ids"]
    assert row["selected_question_count"] == len(session["question_ids"])
    assert row["selection_strategy"] == "adaptive"


def test_next_question_never_leaks_the_answer(db, world):
    """The reveal happens only after an attempt is recorded."""
    uid, _, _ = world
    re_ = RevisionEngine(db)
    session = re_.start_session(uid, count=2)
    served = re_.next_question(uid, session["session_id"])
    assert "correct_index" not in served and "rationale" not in served
    assert set(served) == {"question_id", "stem", "options", "family", "difficulty"}


def test_the_session_advances_and_then_finishes(db, world):
    uid, _, _ = world
    ks, re_ = KnowledgeStore(db), RevisionEngine(db)
    session = re_.start_session(uid, count=2)
    sid = session["session_id"]

    first = re_.next_question(uid, sid)
    ks.record_attempt(user_id=uid, question_id=first["question_id"], user_answer=1,
                      user_colour=GREEN, session_id=sid)
    second = re_.next_question(uid, sid)
    assert second["question_id"] != first["question_id"]
    ks.record_attempt(user_id=uid, question_id=second["question_id"], user_answer=0,
                      user_colour=RED, session_id=sid, gaps=["FeNa interpretation"])
    assert re_.next_question(uid, sid) is None


def test_completion_produces_an_exact_read_list_not_a_topic(db, world):
    """'Revise anaemia' is what this replaces."""
    uid, _, _ = world
    ks, re_ = KnowledgeStore(db), RevisionEngine(db)
    session = re_.start_session(uid, count=2)
    sid = session["session_id"]
    q = re_.next_question(uid, sid)
    ks.record_attempt(user_id=uid, question_id=q["question_id"], user_answer=0,
                      user_colour=RED, session_id=sid, gaps=["FeNa interpretation"])

    result = re_.complete_session(uid, sid)
    assert result["questions"] == 1 and result["incorrect"] == 1
    assert result["colours"][RED] == 1

    read = result["read_list"]
    assert read and read[0]["label"] == "FeNa interpretation"
    assert read[0]["order"] == 1
    assert "FeNa below 1%" in read[0]["passage"], "the read list points at the passage"
    assert read[0]["locator"] == {"page": 3}


def test_a_session_needs_questions_to_exist(db):
    uid = db.create_user("empty@example.com", "correct-horse")
    with pytest.raises(ValueError, match="generate some first"):
        RevisionEngine(db).start_session(uid, count=5)


def test_the_dashboard_recommends_but_the_learner_chooses(db, world):
    uid, _, _ = world
    ks, re_ = KnowledgeStore(db), RevisionEngine(db)
    ks.record_attempt(user_id=uid, question_id="q1", user_answer=0, user_colour=RED,
                      gaps=["FeNa interpretation"])
    dash = re_.dashboard(uid)
    assert dash["recommended_question_count"] >= 10
    assert dash["top_priority"] and dash["top_priority"][0]["why"]

    # The recommendation is not a cap.
    session = re_.start_session(uid, count=2)
    assert session["selected_question_count"] <= 2
