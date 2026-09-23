"""
Phases 10 and 11, and the whole loop end to end.

The last test in this file is the one that matters most: it walks the core loop
the product exists to run --

    source -> notebook -> concepts -> questions -> attempt -> R/O/G ->
    knowledge gap -> concept performance -> priority -> revision queue ->
    read list

-- through the real API, with persistence, and then asserts the state survives
a restart.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from student.ai import AIEngine
from student.api import StudentAPI
from student.db import Database, now_iso
from student.generation import AIConceptExtractor, QuestionGenerator
from student.ingestion import IngestionEngine
from student.knowledge import GREEN, ORANGE, RED
from student.validation import QuestionValidator


class _Scripted:
    name, model, model_version = "scripted", "test-model", "1.0"

    def __init__(self, router):
        self.router = router          # prompt -> reply
        self.prompts: list[str] = []

    def generate(self, request):
        from benchmark.providers.base import GenerationResponse
        from student.ai import extract_json
        self.prompts.append(request.prompt)
        reply = self.router(request.prompt)
        return GenerationResponse(
            item_id=request.item_id, raw_output=reply, parsed=extract_json(reply),
            provider=self.name, model=self.model, model_version=self.model_version,
            latency_ms=3.0, input_tokens=1, output_tokens=1, error=None, attempts=1)


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "q.db")


# ---------------------------------------------------------------------------
# Phase 10: the daily trigger -- RETIRED (ADR-031)
# ---------------------------------------------------------------------------
#
# The one-trigger-per-learner model and its seven tests were replaced by
# multi-reminders. Every guarantee they held that still applies is re-tested
# against the new model in tests/test_reminders.py: input is validated, the
# instant is computed in the learner's own zone, every firing is recorded
# whether it sent or not, no sender means "failed" with the reason, firing
# never starts a revision session, and only what is due fires.


# ---------------------------------------------------------------------------
# The whole loop
# ---------------------------------------------------------------------------

def _reply_for(prompt: str) -> str:
    if "Extract the medical concepts" in prompt:
        return json.dumps({
            "concepts": [
                {"name": "Pre-renal AKI", "description": "Hypoperfusion injury"},
                {"name": "FeNa interpretation", "description": "Sodium excretion fraction"},
            ],
            "relationships": [{"from": "FeNa interpretation", "to": "Pre-renal AKI",
                               "type": "diagnostic_feature_of", "confidence": 0.9}]})
    if "Write" in prompt and "question" in prompt:
        return json.dumps({"questions": [
            {"stem": f"Question {i} about FeNa?",
             "options": ["Pre-renal", "Intrinsic", "Post-renal", "Normal"],
             "correct_index": 0, "rationale": "FeNa below 1%.",
             "concepts_tested": ["FeNa interpretation"], "passage": 1}
            for i in range(1, 5)]})
    if "Review this examination question" in prompt:
        from student.validation import CHECKS
        return json.dumps({"checks": {k: True for k, _ in CHECKS}, "issues": [],
                           "verdict": "approved"})
    return "{}"


@pytest.fixture
def app(db, tmp_path):
    provider = _Scripted(_reply_for)
    ai = AIEngine(db, provider_factory=lambda c: provider, development_candidate="cand-gen")
    engine = IngestionEngine(db, concept_extractor=AIConceptExtractor(db, ai),
                             storage_dir=tmp_path / "src")
    # A distinct candidate for validation, so independence holds.
    validator_ai = AIEngine(db, provider_factory=lambda c: provider,
                            development_candidate="cand-val")
    api = StudentAPI(db, engine=engine, ai=ai,
                     generator=QuestionGenerator(db, ai),
                     validator=QuestionValidator(db, validator_ai),
                     notifier=lambda p: True)
    yield api, engine
    engine.stop()


def test_the_whole_loop_from_a_source_to_a_read_list(app, db, tmp_path):
    api, engine = app

    # --- register ---
    _, auth = api.handle("POST", "/auth/register", {},
                         {"email": "pg@example.com", "password": "correct-horse"}, None)
    token = auth["token"]

    # --- notebook + source ---
    _, nb = api.handle("POST", "/notebooks", {}, {"title": "Renal", "subject": "Medicine"}, token)
    status, src = api.handle("POST", f"/notebooks/{nb['id']}/sources", {}, {
        "kind": "text",
        "text": ("Pre-renal acute kidney injury arises from hypoperfusion. "
                 "The fractional excretion of sodium is below one percent in pre-renal "
                 "disease, whereas intrinsic renal injury shows values above two percent. "
                 * 4)}, token)
    assert status == 202
    assert engine.wait_idle(30)

    _, progress = api.handle("GET", f"/sources/{src['source_id']}/progress", {}, {}, token)
    assert progress["status"] == "extracted"
    assert progress["concepts_found"] >= 2, "ingestion must populate the concept graph"

    # --- the graph exists and is cross-linked ---
    _, graph = api.handle("GET", "/graph", {}, {}, token)
    assert len(graph["nodes"]) >= 2 and len(graph["edges"]) >= 1

    # --- generate + validate ---
    status, gen = api.handle("POST", f"/notebooks/{nb['id']}/questions", {},
                             {"count": 4, "difficulty": "postgraduate"}, token)
    assert status == 201 and gen["count"] == 4
    assert gen["validation"]["approved"] == 4, "validation runs on a different configuration"

    _, bank = api.handle("GET", f"/notebooks/{nb['id']}/questions", {}, {}, token)
    assert len(bank["questions"]) == 4
    assert all(q["validation_status"] == "approved" for q in bank["questions"])

    # --- a session ---
    _, dash = api.handle("GET", "/revision/dashboard", {}, {}, token)
    assert dash["recommended_question_count"] >= 1

    status, session = api.handle("POST", "/revision/sessions", {},
                                 {"count": 3, "strategy": "adaptive"}, token)
    assert status == 201
    sid = session["session_id"]

    # --- answer, wrongly, and grade it RED with a specific gap ---
    _, served = api.handle("GET", "/revision/next", {"session": sid}, {}, token)
    q = served["question"]
    assert "correct_index" not in q, "the key must not travel with the question"

    status, recorded = api.handle("POST", "/attempts", {}, {
        "question_id": q["question_id"], "user_answer": 1, "user_colour": RED,
        "session_id": sid, "gaps": ["FeNa interpretation"]}, token)
    assert status == 201
    reveal = recorded["reveal"]
    assert reveal["is_correct"] is False
    assert reveal["correct_answer"] == 0
    assert "source_passage" in reveal, "the reveal shows where the answer came from"

    # --- a second wrong answer makes the concept RED, not the first ---
    _, served2 = api.handle("GET", "/revision/next", {"session": sid}, {}, token)
    api.handle("POST", "/attempts", {}, {
        "question_id": served2["question"]["question_id"], "user_answer": 1,
        "user_colour": RED, "session_id": sid, "gaps": ["FeNa interpretation"]}, token)

    _, concepts = api.handle("GET", "/concepts", {}, {}, token)
    top = concepts["concepts"][0]
    assert top["colour"] == RED
    assert top["priority_score"] > 0 and top["why"], "priority must explain itself"

    # --- the gap points back at its evidence ---
    _, gaps = api.handle("GET", "/gaps", {}, {}, token)
    assert len(gaps["gaps"]) == 1
    gap = gaps["gaps"][0]
    assert gap["label"] == "FeNa interpretation" and gap["evidence_count"] == 2

    _, evidence = api.handle("GET", f"/gaps/{gap['id']}", {}, {}, token)
    assert len(evidence["evidence"]) == 2
    assert "fractional excretion" in evidence["evidence"][0]["passage"].lower()

    # --- "test me on this" ---
    _, recall = api.handle("GET", f"/gaps/{gap['id']}/questions", {}, {}, token)
    assert recall["questions"], "a gap must be practisable"

    # --- finish, and get an exact read list ---
    status, summary = api.handle("POST", f"/revision/sessions/{sid}/complete", {}, {}, token)
    assert status == 200
    assert summary["questions"] == 2 and summary["incorrect"] == 2
    assert summary["colours"][RED] == 2
    read = summary["read_list"]
    assert read and read[0]["label"] == "FeNa interpretation"
    assert read[0]["passage"], "a read list entry names the passage, not the topic"

    # --- progress reflects the session ---
    _, prog = api.handle("GET", "/progress", {}, {}, token)
    assert prog["attempts_total"] == 2 and prog["attempts_correct"] == 0
    assert prog["open_gaps"] == 1

    # --- and none of it was in memory ---
    reopened = Database(db.path)
    fresh = StudentAPI(reopened)
    _, after_restart = fresh.handle("GET", "/gaps", {}, {}, token)
    assert len(after_restart["gaps"]) == 1, "learner state must survive a restart"
    _, prog2 = fresh.handle("GET", "/progress", {}, {}, token)
    assert prog2["attempts_total"] == 2


def test_a_second_learner_shares_no_state_with_the_first(app, db):
    api, engine = app
    _, a = api.handle("POST", "/auth/register", {},
                      {"email": "a@example.com", "password": "correct-horse"}, None)
    _, b = api.handle("POST", "/auth/register", {},
                      {"email": "b@example.com", "password": "correct-horse"}, None)
    api.handle("POST", "/notebooks", {}, {"title": "A's book"}, a["token"])

    assert api.handle("GET", "/notebooks", {}, {}, b["token"])[1]["notebooks"] == []
    assert api.handle("GET", "/gaps", {}, {}, b["token"])[1]["gaps"] == []
    assert api.handle("GET", "/progress", {}, {}, b["token"])[1]["attempts_total"] == 0


def test_the_same_file_twice_in_one_notebook_is_one_source(app, db):
    """
    Not merely a billing question. Two copies produce two sets of concepts
    feeding a single revision schedule, so the same fact is scheduled twice and
    the learner is drilled on it twice for no reason.
    """
    import base64
    api, engine = app
    _, auth = api.handle("POST", "/auth/register", {},
                         {"email": "dup@example.com", "password": "correct-horse"}, None)
    token = auth["token"]
    _, nb = api.handle("POST", "/notebooks", {},
                       {"title": "Renal", "subject": "Medicine"}, token)

    payload = {"kind": "text", "text": "The nephron filters plasma.",
               "filename": "notes.txt"}
    _, first = api.handle("POST", f"/notebooks/{nb['id']}/sources", {}, payload, token)
    engine.wait_idle()

    # A text source carries no file, so nothing is hashed and nothing dedupes:
    # two pastes are genuinely two sources.
    _, second = api.handle("POST", f"/notebooks/{nb['id']}/sources", {}, payload, token)
    engine.wait_idle()
    assert second["source_id"] != first["source_id"], \
        "dedupe is on stored bytes; pasted text has none"

    # A file, however, is hashed -- and the same bytes come back as the same source.
    content = base64.b64encode(b"%PDF-1.4 not really a pdf but stable bytes").decode()
    file_payload = {"kind": "pdf", "filename": "chapter.pdf", "content_base64": content}
    _, upload_one = api.handle("POST", f"/notebooks/{nb['id']}/sources", {},
                               file_payload, token)
    engine.wait_idle()
    _, upload_two = api.handle("POST", f"/notebooks/{nb['id']}/sources", {},
                               file_payload, token)
    engine.wait_idle()

    assert upload_two["source_id"] == upload_one["source_id"]
    assert upload_two.get("duplicate_of") == upload_one["source_id"]
    assert upload_two["bytes_stored"] == 0, "the second copy is not stored again"

    rows = db.query("SELECT id FROM sources WHERE notebook_id = ? AND kind = 'pdf'",
                    (nb["id"],))
    assert len(rows) == 1, "one row, not two"


def test_the_same_file_in_a_DIFFERENT_notebook_is_a_separate_source(app, db):
    """Different notebook means different subject and schedule -- genuinely separate."""
    import base64
    api, engine = app
    _, auth = api.handle("POST", "/auth/register", {},
                         {"email": "two-books@example.com", "password": "correct-horse"}, None)
    token = auth["token"]
    _, one = api.handle("POST", "/notebooks", {}, {"title": "Renal", "subject": "Med"}, token)
    _, two = api.handle("POST", "/notebooks", {}, {"title": "Cardio", "subject": "Med"}, token)

    content = base64.b64encode(b"%PDF-1.4 shared across notebooks").decode()
    payload = {"kind": "pdf", "filename": "shared.pdf", "content_base64": content}
    _, a = api.handle("POST", f"/notebooks/{one['id']}/sources", {}, payload, token)
    engine.wait_idle()
    _, b = api.handle("POST", f"/notebooks/{two['id']}/sources", {}, payload, token)
    engine.wait_idle()

    assert a["source_id"] != b["source_id"]
    assert "duplicate_of" not in b
