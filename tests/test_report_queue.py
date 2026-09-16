"""
The other end of the learner's report path.

WHY THIS FILE EXISTS
--------------------
`student/safety.py` recorded a report with its provenance frozen, promoted
content errors to adjudication candidates, and could close one in a named
person's name. All of it was reachable from Python and from nowhere else: no
HTTP route read the queue.

So a learner pressing "this question is wrong" wrote a row into a table nobody
could open. That is worse than having no report button, because the button
implies a loop that closes and none of it did. These tests are about the loop
actually closing, end to end, through the same interface an operator uses.
"""

from __future__ import annotations

import json

import pytest

from student.db import Database, new_id, now_iso
from student.api import StudentAPI


@pytest.fixture
def world(tmp_path):
    db = Database(tmp_path / "q.db")
    api = StudentAPI(db)

    def register(email):
        return api.handle("POST", "/auth/register", {},
                          {"email": email, "password": "correct-horse"}, None)[1]["token"]

    learner = register("learner@example.com")
    other = register("other@example.com")
    operator = register("ops@example.com")
    db.execute("UPDATE users SET role = 'admin', name = 'Dr Ops' WHERE email = ?",
               ("ops@example.com",))

    nid = api.handle("POST", "/notebooks", {}, {"title": "N", "subject": "Med"},
                     learner)[1]["id"]
    sid, cid = new_id("src"), new_id("chk")
    db.execute("INSERT INTO sources (id, notebook_id, kind, filename, status,"
               " uploaded_at) VALUES (?,?,?,?,?,?)",
               (sid, nid, "pdf", "scan.pdf", "extracted", now_iso()))
    db.execute("INSERT INTO source_chunks (id, source_id, ordinal, text, locator_json,"
               " confidence, extraction_method, needs_review, status)"
               " VALUES (?,?,?,?,?,?,?,?,?)",
               (cid, sid, 1, "Blurry OCR text.", json.dumps({"page": 7}), 0.31,
                "ocr_low", 1, "processed"))

    def question(qid, stem):
        db.execute("INSERT INTO questions (id, primary_notebook_id, family, stem,"
                   " options_json, correct_index, rationale, source_id, chunk_id,"
                   " validation_status, generated_by_candidate_id, prompt_version,"
                   " generated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (qid, nid, "mcq", stem, json.dumps(["A", "B", "C", "D"]), 0,
                    "Because.", sid, cid, "approved", "cand-gen", "gen/0.1.0",
                    now_iso()))
        return qid

    w = type("W", (), {})()
    w.db, w.api = db, api
    w.learner, w.other, w.operator = learner, other, operator
    w.q1 = question(new_id("q"), "First question.")
    w.q2 = question(new_id("q"), "Second question.")
    return w


def _report(world, qid, kind="factually_wrong", note="", token=None):
    status, body = world.api.handle(
        "POST", f"/questions/{qid}/reports", {},
        {"kind": kind, "note": note}, token or world.learner)
    assert status == 201, body
    return body


# ---------------------------------------------------------------------------
# The loop closes
# ---------------------------------------------------------------------------

def test_a_report_a_learner_files_is_readable_by_an_operator(world):
    """The single fact this whole file exists to establish."""
    _report(world, world.q1, note="The keyed answer is wrong.")

    status, body = world.api.handle("GET", "/ops/reports", {}, {}, world.operator)
    assert status == 200
    assert len(body["reports"]) == 1
    assert body["reports"][0]["question_id"] == world.q1


def test_the_operator_sees_the_frozen_provenance_not_just_the_complaint(world):
    """
    A reviewer needs the stem, the passage and what the extractor thought of
    it. A queue of bare question ids would send them to look up a question
    that may since have been regenerated.
    """
    _report(world, world.q1)
    entry = world.api.handle("GET", "/ops/reports", {}, {}, world.operator)[1]["reports"][0]

    prov = entry["provenance"]
    assert prov["stem"] == "First question."
    assert prov["chunk_confidence"] == pytest.approx(0.31)
    assert prov["chunk_needs_review"] is True
    assert prov["locator"] == {"page": 7}
    assert prov["validation_status"] == "approved"


def test_resolving_closes_the_loop_and_the_learner_can_see_it(world):
    """
    The learner who reported it must be able to see that somebody answered.
    A resolution only an operator can see is the same silence as before.
    """
    report = _report(world, world.q1, note="Keyed answer is B, not A.")

    status, resolved = world.api.handle(
        "POST", f"/ops/reports/{report['report_id']}", {},
        {"resolution": "upheld", "note": "Confirmed against the source."},
        world.operator)
    assert status == 200
    assert resolved["resolution"] == "upheld"

    mine = world.api.handle("GET", "/reports", {}, {}, world.learner)[1]["reports"]
    assert len(mine) == 1
    assert mine[0]["resolution"] == "upheld"
    assert mine[0]["resolved_by"] == "Dr Ops"


def test_a_resolved_report_leaves_the_open_queue(world):
    report = _report(world, world.q1)
    _report(world, world.q2)
    assert len(world.api.handle("GET", "/ops/reports", {}, {}, world.operator)[1]["reports"]) == 2

    world.api.handle("POST", f"/ops/reports/{report['report_id']}", {},
                     {"resolution": "rejected"}, world.operator)

    remaining = world.api.handle("GET", "/ops/reports", {}, {}, world.operator)[1]["reports"]
    assert [r["question_id"] for r in remaining] == [world.q2]


# ---------------------------------------------------------------------------
# Who may read it
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/ops/reports", "/ops/reports/gold-candidates"])
def test_a_learner_cannot_read_the_queue(world, path):
    """
    404, not 403, matching every other refusal here. The queue names other
    learners' ids and the stems they complained about, and a 403 would confirm
    the route exists to somebody who should not know.
    """
    _report(world, world.q1)
    status, _ = world.api.handle("GET", path, {}, {}, world.learner)
    assert status == 404


def test_a_learner_cannot_resolve_their_own_report(world):
    """Otherwise 'upheld' means nothing: the complainant marked their own homework."""
    report = _report(world, world.q1)
    status, _ = world.api.handle("POST", f"/ops/reports/{report['report_id']}", {},
                                 {"resolution": "upheld"}, world.learner)
    assert status == 404


def test_an_unauthenticated_caller_cannot_read_the_queue(world):
    status, _ = world.api.handle("GET", "/ops/reports", {}, {}, None)
    assert status in (401, 404)


# ---------------------------------------------------------------------------
# A resolution has a name on it
# ---------------------------------------------------------------------------

def test_the_resolver_defaults_to_the_authenticated_operator(world):
    """A resolution that can name anybody is a resolution that names nobody."""
    report = _report(world, world.q1)
    resolved = world.api.handle("POST", f"/ops/reports/{report['report_id']}", {},
                                {"resolution": "acknowledged"}, world.operator)[1]
    assert resolved["resolved_by"] == "Dr Ops"


def test_an_operator_may_record_someone_elses_judgement(world):
    """
    An operator acting on a clinician's call should be able to record the
    clinician, because that is who actually decided.
    """
    report = _report(world, world.q1)
    resolved = world.api.handle(
        "POST", f"/ops/reports/{report['report_id']}", {},
        {"resolution": "upheld", "resolved_by": "Dr A. Sen, Nephrology"},
        world.operator)[1]
    assert resolved["resolved_by"] == "Dr A. Sen, Nephrology"


def test_an_unknown_resolution_is_refused(world):
    report = _report(world, world.q1)
    status, body = world.api.handle("POST", f"/ops/reports/{report['report_id']}", {},
                                    {"resolution": "probably fine"}, world.operator)
    assert status == 400
    assert "resolution must be one of" in body["error"]


def test_a_report_cannot_be_resolved_back_to_open(world):
    """'Open' is not a resolution; reopening by that route would erase a name."""
    report = _report(world, world.q1)
    status, _ = world.api.handle("POST", f"/ops/reports/{report['report_id']}", {},
                                 {"resolution": "open"}, world.operator)
    assert status == 400


def test_resolving_a_report_that_does_not_exist_is_a_400_not_a_crash(world):
    status, body = world.api.handle("POST", "/ops/reports/rep_nonexistent", {},
                                    {"resolution": "rejected"}, world.operator)
    assert status == 400
    assert "no such report" in body["error"]


# ---------------------------------------------------------------------------
# The gold-error route
# ---------------------------------------------------------------------------

def test_a_content_error_becomes_a_gold_candidate(world):
    """
    The route for a gold error found by someone other than the author. The
    corpus is model-authored and unreviewed; a learner hitting a bad question
    is finding that same defect from the other end.
    """
    _report(world, world.q1, kind="wrong_answer_keyed")

    status, body = world.api.handle(
        "GET", "/ops/reports/gold-candidates", {}, {}, world.operator)
    assert status == 200
    assert [c["question_id"] for c in body["candidates"]] == [world.q1]


def test_a_taste_complaint_is_not_a_gold_candidate(world):
    """
    "Ambiguous" says the question was unclear, not that the keyed answer is
    wrong. Only factually_wrong, wrong_answer_keyed and not_in_source assert a
    CONTENT error; letting the rest into the adjudication queue would waste the
    scarcest resource the project has.
    """
    _report(world, world.q1, kind="ambiguous")
    body = world.api.handle("GET", "/ops/reports/gold-candidates", {}, {},
                            world.operator)[1]
    assert body["candidates"] == []


def test_an_upheld_content_error_stays_a_candidate(world):
    """
    Upheld means a human agreed it is wrong -- which makes it MORE relevant to
    adjudication, not less. It leaves the open queue and stays here.
    """
    report = _report(world, world.q1, kind="not_in_source")
    world.api.handle("POST", f"/ops/reports/{report['report_id']}", {},
                     {"resolution": "upheld"}, world.operator)

    assert world.api.handle("GET", "/ops/reports", {}, {}, world.operator)[1]["reports"] == []
    candidates = world.api.handle("GET", "/ops/reports/gold-candidates", {}, {},
                                  world.operator)[1]["candidates"]
    assert [c["question_id"] for c in candidates] == [world.q1]


def test_a_rejected_content_error_leaves_the_candidate_queue(world):
    report = _report(world, world.q1, kind="factually_wrong")
    world.api.handle("POST", f"/ops/reports/{report['report_id']}", {},
                     {"resolution": "rejected"}, world.operator)
    assert world.api.handle("GET", "/ops/reports/gold-candidates", {}, {},
                            world.operator)[1]["candidates"] == []


def test_the_gold_queue_does_not_write_to_the_corpus(world):
    """
    This produces the queue that FEEDS adjudication. Adjudication is two named
    reviewers, blind, with kappa reported. Nothing here may touch the corpus.
    """
    import hashlib
    from pathlib import Path
    corpus = Path("corpus")
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(corpus.rglob("*.jsonl"))}

    report = _report(world, world.q1, kind="wrong_answer_keyed")
    world.api.handle("GET", "/ops/reports/gold-candidates", {}, {}, world.operator)
    world.api.handle("POST", f"/ops/reports/{report['report_id']}", {},
                     {"resolution": "upheld"}, world.operator)

    after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(corpus.rglob("*.jsonl"))}
    assert before == after, "the report queue modified the corpus"
