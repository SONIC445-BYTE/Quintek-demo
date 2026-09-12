"""
What happens to a wrong question that reaches someone who cannot check it.

THE DEFECT THIS FILE OPENS WITH
--------------------------------
The ingestion quality gate computes `confidence` and `needs_review` per chunk
and stores them. Both paths that hand a question to a learner --
`get_question` and the `/attempts` reveal -- each built their provenance
payload inline, and each selected only `text, locator_json`. So the flags were
computed, written, and never read again.

Verified by driving a question built from a chunk at confidence 0.31 with
`needs_review = 1` through the real API and looking at the response: both
fields absent. A flag that stops at the database is worse than no flag,
because it leaves a record saying the material was checked while the person
reading it was told nothing.

WHAT ELSE IS HERE
-----------------
A learner can say a question is wrong, that report keeps the question's
provenance FROZEN, and there is a queue it lands in. The validator lowers the
rate of wrong questions and nothing lowers it to zero; without a report path
the remainder is found by the person it misleads and goes no further.
"""

from __future__ import annotations

import ast
import json
import pathlib

import pytest

from student import safety
from student.api import StudentAPI
from student.db import Database, new_id, now_iso

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: The fields a learner must be given about where a question came from and how
#: far to trust it. Named once; every render path is checked against it.
PROVENANCE_FIELDS = ("source_passage", "source_locator", "chunk_confidence",
                     "needs_review", "extraction_method")


@pytest.fixture
def world(tmp_path):
    db = Database(tmp_path / "q.db")
    api = StudentAPI(db)

    def register(email):
        return api.handle("POST", "/auth/register", {},
                          {"email": email, "password": "correct-horse"}, None)[1]["token"]

    a, b = register("a@example.com"), register("b@example.com")
    uid_a = db.query_one("SELECT id FROM users WHERE email='a@example.com'")["id"]
    uid_b = db.query_one("SELECT id FROM users WHERE email='b@example.com'")["id"]
    nid = api.handle("POST", "/notebooks", {},
                     {"title": "N", "subject": "Med"}, a)[1]["id"]

    sid, cid, qid = new_id("src"), new_id("chk"), new_id("q")
    db.execute("INSERT INTO sources (id, notebook_id, kind, filename, status,"
               " uploaded_at) VALUES (?,?,?,?,?,?)",
               (sid, nid, "pdf", "scan.pdf", "extracted", now_iso()))
    # A deliberately POOR chunk: this is the case the flags exist for.
    db.execute("INSERT INTO source_chunks (id, source_id, ordinal, text, locator_json,"
               " confidence, extraction_method, needs_review, status)"
               " VALUES (?,?,?,?,?,?,?,?,?)",
               (cid, sid, 1, "Blurry OCR text about renal physiology.",
                json.dumps({"page": 7}), 0.31, "ocr_low", 1, "processed"))
    db.execute("INSERT INTO questions (id, primary_notebook_id, family, stem,"
               " options_json, correct_index, rationale, source_id, chunk_id,"
               " validation_status, generated_by_candidate_id, prompt_version,"
               " generated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (qid, nid, "mcq", "What does a low FeNa indicate?",
                json.dumps(["Pre-renal", "Intrinsic", "Post-renal", "Normal"]), 0,
                "Because the passage says so.", sid, cid, "flagged",
                "cand-gen", "gen/0.1.0", now_iso()))

    w = type("W", (), {})()
    w.db, w.api, w.a, w.b, w.uid_a, w.uid_b, w.qid, w.cid = db, api, a, b, uid_a, uid_b, qid, cid
    return w


# ---------------------------------------------------------------------------
# The flags reach the screen
# ---------------------------------------------------------------------------

def render_paths(world):
    """
    Every response that hands a learner a question, as (name, payload).

    Both are exercised because both had the defect independently -- each built
    its provenance inline, so fixing one would have left the other silent.
    """
    _, q = world.api.handle("GET", f"/questions/{world.qid}", {}, {}, world.a)
    _, att = world.api.handle("POST", "/attempts", {},
                              {"question_id": world.qid, "user_answer": 0,
                               "user_colour": "GREEN"}, world.a)
    return {"get_question": q, "attempt_reveal": att["reveal"]}


def test_every_inline_provenance_builder_uses_the_shared_helper():
    """
    The structural half. Both paths selected `text, locator_json` by hand; a
    third that does the same would silently drop the flags again.
    """
    source = (ROOT / "student" / "api.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    # Only queries that hand the learner PASSAGE TEXT. api.py also counts chunk
    # statuses and lists failed-chunk errors for the ingestion screen; those
    # touch source_chunks and have nothing to do with trust signals, and an
    # earlier version of this test flagged them. The property is narrower: if
    # you are giving someone the passage, give them how far to trust it.
    offenders = [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and "FROM source_chunks" in node.value
        and "text" in node.value.split("FROM")[0]
        and "confidence" not in node.value
    ]
    assert not offenders, (
        f"student/api.py line(s) {offenders} hand a learner chunk text without "
        "selecting confidence. Use _provenance_of: the quality gate's flags must "
        "not stop at the database.")


@pytest.mark.parametrize("field", PROVENANCE_FIELDS)
@pytest.mark.parametrize("path", ["get_question", "attempt_reveal"])
def test_the_learner_is_told_where_it_came_from(world, path, field):
    payload = render_paths(world)[path]
    assert field in payload, (
        f"{path} does not tell the learner {field!r}. It was computed by the "
        "ingestion quality gate and stored; dropping it here leaves a record "
        "saying the material was checked while the reader was told nothing.")


@pytest.mark.parametrize("path", ["get_question", "attempt_reveal"])
def test_the_values_are_the_real_ones_not_defaults(world, path):
    payload = render_paths(world)[path]
    assert payload["chunk_confidence"] == pytest.approx(0.31)
    assert payload["needs_review"] is True
    assert payload["extraction_method"] == "ocr_low"
    assert payload["source_locator"] == {"page": 7}


def test_the_reveal_carries_the_validation_verdict(world):
    """A learner should know the validator flagged this before trusting it."""
    assert render_paths(world)["attempt_reveal"]["validation_status"] == "flagged"


# ---------------------------------------------------------------------------
# Saying a question is wrong
# ---------------------------------------------------------------------------

class TestReporting:

    def test_a_learner_can_report_a_question(self, world):
        status, out = world.api.handle(
            "POST", f"/questions/{world.qid}/reports", {},
            {"kind": safety.FACTUALLY_WRONG, "note": "FeNa below 1% is pre-renal, "
                                                     "the rationale says the opposite"},
            world.a)
        assert status == 201
        assert out["resolution"] == safety.OPEN
        assert out["provenance"]["chunk_confidence"] == pytest.approx(0.31)

    def test_the_provenance_is_frozen_not_referenced(self, world):
        """
        A question can be regenerated and a chunk re-ingested. A report holding
        only ids would, by the time anyone read it, describe something that no
        longer exists in the form complained about.
        """
        world.api.handle("POST", f"/questions/{world.qid}/reports", {},
                         {"kind": safety.FACTUALLY_WRONG}, world.a)
        world.db.execute("UPDATE questions SET stem = ? WHERE id = ?",
                         ("A completely different question now.", world.qid))
        world.db.execute("UPDATE source_chunks SET confidence = 1.0 WHERE id = ?",
                         (world.cid,))

        [report] = safety.for_user(world.db, world.uid_a)
        assert report["provenance"]["stem"] == "What does a low FeNa indicate?"
        assert report["provenance"]["chunk_confidence"] == pytest.approx(0.31)

    def test_an_unknown_kind_is_refused(self, world):
        status, out = world.api.handle("POST", f"/questions/{world.qid}/reports", {},
                                       {"kind": "vibes"}, world.a)
        assert status == 400
        assert "unknown report kind" in str(out)

    def test_a_stranger_cannot_report_a_question_they_cannot_see(self, world):
        """Same contract as every other question route: 404, not 403."""
        status, _ = world.api.handle("POST", f"/questions/{world.qid}/reports", {},
                                     {"kind": safety.FACTUALLY_WRONG}, world.b)
        assert status == 404
        assert safety.for_question(world.db, world.qid) == []

    def test_a_learner_sees_only_their_own_reports(self, world):
        world.api.handle("POST", f"/questions/{world.qid}/reports", {},
                         {"kind": safety.AMBIGUOUS}, world.a)
        _, mine = world.api.handle("GET", "/reports", {}, {}, world.a)
        _, theirs = world.api.handle("GET", "/reports", {}, {}, world.b)
        assert len(mine["reports"]) == 1
        assert theirs["reports"] == []


class TestTheQueue:

    def test_open_reports_are_oldest_first(self, world):
        """
        A newest-first queue is how a backlog becomes permanent: the report
        that has waited longest is the one most likely to have been forgotten.
        """
        for kind in (safety.FACTUALLY_WRONG, safety.AMBIGUOUS, safety.NOT_IN_SOURCE):
            safety.record(world.db, user_id=world.uid_a, question_id=world.qid, kind=kind)
        queue = safety.open_reports(world.db)
        assert [r["kind"] for r in queue] == [safety.FACTUALLY_WRONG, safety.AMBIGUOUS,
                                              safety.NOT_IN_SOURCE]

    def test_resolving_needs_a_named_person(self, world):
        r = safety.record(world.db, user_id=world.uid_a, question_id=world.qid,
                          kind=safety.FACTUALLY_WRONG)
        with pytest.raises(safety.ReportRejected, match="name"):
            safety.resolve(world.db, r["report_id"], resolution=safety.UPHELD,
                           resolved_by="   ")

    def test_a_resolved_report_leaves_the_queue(self, world):
        r = safety.record(world.db, user_id=world.uid_a, question_id=world.qid,
                          kind=safety.AMBIGUOUS)
        safety.resolve(world.db, r["report_id"], resolution=safety.REJECTED,
                       resolved_by="Dr A Reviewer", note="wording is standard")
        assert safety.open_reports(world.db) == []

    def test_reopening_is_not_possible_by_resolving_to_open(self, world):
        r = safety.record(world.db, user_id=world.uid_a, question_id=world.qid,
                          kind=safety.OTHER)
        with pytest.raises(safety.ReportRejected):
            safety.resolve(world.db, r["report_id"], resolution=safety.OPEN,
                           resolved_by="Dr A Reviewer")


class TestTheGoldErrorRoute:
    """
    A gold error found by a learner rather than by the corpus author.

    The development corpus is model-authored and unreviewed, and one CLEAN item
    has already turned out to be wrong. A learner hitting a bad question is
    finding the same class of defect from the other end.
    """

    def test_content_errors_become_adjudication_candidates(self, world):
        safety.record(world.db, user_id=world.uid_a, question_id=world.qid,
                      kind=safety.FACTUALLY_WRONG, note="the key is wrong")
        [candidate] = safety.gold_candidates(world.db)
        assert candidate["note"] == "the key is wrong"
        assert candidate["provenance"]["validation_status"] == "flagged"
        assert candidate["provenance"]["generated_by_candidate_id"] == "cand-gen"

    def test_taste_complaints_are_not_adjudication_candidates(self, world):
        """
        `ambiguous` and `offensive_or_unsafe` matter and are recorded, but they
        are not assertions that a fact is wrong, so they do not queue for
        corpus adjudication.
        """
        for kind in (safety.AMBIGUOUS, safety.OFFENSIVE_OR_UNSAFE, safety.OTHER):
            safety.record(world.db, user_id=world.uid_a, question_id=world.qid, kind=kind)
        assert safety.gold_candidates(world.db) == []
        assert len(safety.open_reports(world.db)) == 3

    def test_a_rejected_report_leaves_the_candidate_queue(self, world):
        r = safety.record(world.db, user_id=world.uid_a, question_id=world.qid,
                          kind=safety.NOT_IN_SOURCE)
        safety.resolve(world.db, r["report_id"], resolution=safety.REJECTED,
                       resolved_by="Dr A Reviewer")
        assert safety.gold_candidates(world.db) == []

    def test_it_does_not_write_to_the_corpus(self):
        """
        Adjudication is `validator/review.py` -- two named reviewers, blind,
        kappa reported. This module produces the queue that feeds it and must
        never relabel anything itself.
        """
        # Checked on the AST, not the text: the module DISCUSSES the corpus at
        # length in its docstrings, which is the point -- it explains why it
        # must not touch it. An earlier version of this test read the raw
        # source and failed on its own explanation.
        tree = ast.parse((ROOT / "student" / "safety.py").read_text(encoding="utf-8"))
        writers = {"open", "write_text", "write_bytes", "unlink", "mkdir", "rename"}
        called = {
            (node.func.id if isinstance(node.func, ast.Name) else node.func.attr)
            for node in ast.walk(tree) if isinstance(node, ast.Call)
            and isinstance(node.func, (ast.Name, ast.Attribute))
        }
        assert not (called & writers), (
            f"student/safety.py calls {sorted(called & writers)}. A report queue that "
            "can edit the ground truth is the corpus tuning itself on complaints; "
            "adjudication is validator/review.py.")
        assert "corpus" not in {
            node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }


class TestTheScopeStatement:

    def test_it_is_served_without_an_account(self, tmp_path):
        api = StudentAPI(Database(tmp_path / "q.db"))
        status, out = api.handle("GET", "/scope", {}, {}, None)
        assert status == 200
        assert out["scope_statement"] == safety.SCOPE_STATEMENT

    def test_it_says_the_three_things_that_matter(self):
        text = safety.SCOPE_STATEMENT.lower()
        assert "revision aid" in text
        assert "can be wrong" in text
        assert "not a clinical reference" in text

    def test_it_offers_the_report_kinds_the_client_must_render(self, tmp_path):
        api = StudentAPI(Database(tmp_path / "q.db"))
        _, out = api.handle("GET", "/scope", {}, {}, None)
        assert set(out["report_kinds"]) == set(safety.REPORT_KINDS)
