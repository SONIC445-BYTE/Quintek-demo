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
from student.notifications import (NotificationError, NotificationService,
                                   next_occurrence, validate_time)
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
# Phase 10: the daily trigger
# ---------------------------------------------------------------------------

def test_trigger_time_and_timezone_are_validated(db):
    uid = db.create_user("l@example.com", "correct-horse")
    svc = NotificationService(db)
    for bad in ["25:00", "8:00", "20:60", "evening", ""]:
        with pytest.raises(NotificationError):
            svc.set_prefs(uid, trigger_time=bad)
    with pytest.raises(NotificationError, match="unknown timezone"):
        svc.set_prefs(uid, tz="Mars/Olympus")
    assert svc.set_prefs(uid, trigger_time="20:00", tz="Asia/Kolkata")["trigger_time"] == "20:00"


def test_the_next_occurrence_is_computed_in_the_learners_own_zone(db):
    """A learner in IST who sets 20:00 means 20:00 where they are. Adding a
    fixed offset to UTC is wrong for half the year anywhere with DST."""
    ist = next_occurrence("20:00", "Asia/Kolkata")
    from zoneinfo import ZoneInfo
    local = ist.astimezone(ZoneInfo("Asia/Kolkata"))
    assert (local.hour, local.minute) == (20, 0)

    ny = next_occurrence("20:00", "America/New_York")
    local_ny = ny.astimezone(ZoneInfo("America/New_York"))
    assert (local_ny.hour, local_ny.minute) == (20, 0)
    assert ist != ny


def test_the_system_never_moves_the_time_the_learner_chose(db):
    """Not on failure, not on a delayed send. A revision habit is built on a
    fixed hour."""
    uid = db.create_user("l@example.com", "correct-horse")
    svc = NotificationService(db, sender=lambda p: False)   # every send fails
    svc.set_prefs(uid, trigger_time="20:00", tz="UTC")

    for _ in range(3):
        db.execute("UPDATE notification_prefs SET next_scheduled_at = ? WHERE user_id = ?",
                   ("2000-01-01T00:00:00Z", uid))
        svc.fire(uid)
    assert svc.get_prefs(uid)["trigger_time"] == "20:00"


def test_a_firing_is_logged_whether_it_succeeded_or_not(db):
    """A trigger that fails quietly is indistinguishable from a learner
    ignoring it."""
    uid = db.create_user("l@example.com", "correct-horse")
    sent: list[dict] = []
    svc = NotificationService(db, sender=lambda p: sent.append(p) or True)
    svc.set_prefs(uid, trigger_time="20:00", tz="UTC", push=True)
    svc.fire(uid)

    failing = NotificationService(db, sender=lambda p: (_ for _ in ()).throw(RuntimeError("down")))
    failing.fire(uid)

    history = svc.history(uid)
    assert {h["status"] for h in history} == {"sent", "failed"}
    assert any("down" in h["detail"] for h in history)
    assert len(sent) == 1


def test_with_no_sender_configured_the_firing_is_recorded_as_failed(db):
    uid = db.create_user("l@example.com", "correct-horse")
    svc = NotificationService(db)
    svc.set_prefs(uid, trigger_time="20:00", tz="UTC")
    result = svc.fire(uid)
    assert result["ok"] is False and "no notification sender" in result["detail"]


def test_the_notification_announces_the_queue_and_does_not_start_it(db):
    uid = db.create_user("l@example.com", "correct-horse")
    svc = NotificationService(db, sender=lambda p: True)
    svc.set_prefs(uid, trigger_time="20:00", tz="UTC")
    payload = svc.fire(uid)
    assert "ready" in payload["message"]
    assert db.query_one("SELECT COUNT(*) c FROM revision_sessions")["c"] == 0


def test_only_users_whose_time_has_arrived_are_fired(db):
    early = db.create_user("early@example.com", "correct-horse")
    later = db.create_user("later@example.com", "correct-horse")
    svc = NotificationService(db, sender=lambda p: True)
    svc.set_prefs(early, trigger_time="20:00", tz="UTC")
    svc.set_prefs(later, trigger_time="20:00", tz="UTC")
    db.execute("UPDATE notification_prefs SET next_scheduled_at='2000-01-01T00:00:00Z'"
               " WHERE user_id=?", (early,))
    assert svc.due_users() == [early]
    assert svc.run_due() == {"fired": 1, "sent": 1, "failed": 0}


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
                     notifier=NotificationService(db, sender=lambda p: True))
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
