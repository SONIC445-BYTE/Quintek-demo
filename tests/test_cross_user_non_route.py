"""
Cross-user coverage for the paths a route-derived test class cannot reach.

`test_cross_user_access.py` walks the route table, which is the right shape for
anything a learner can call directly. It structurally cannot cover three
things, and this file is about those:

  * BACKGROUND WORKERS -- `IngestionEngine` runs on its own thread with no user
    context at all. Nothing it does is a "request", so no route probe reaches
    it.
  * HELPER FUNCTIONS -- `QuestionValidator.validate_pending`,
    `QuestionGenerator._store_question` and friends take ids and no owner.
    Their safety is a property of who calls them, which a route probe cannot
    observe.
  * AGGREGATES -- billing's revenue, cost and usage totals are cross-user ON
    PURPOSE. Asserting they are scoped would be asserting the wrong thing.

The audit behind this file enumerated all 195 SQL statements in `student/` and
`billing/`. 162 touch user-owned tables; 84 of those carry no owner column.
Almost all are safe because a caller established ownership first -- the
"gate, then query by the gated id" shape. This file pins the gates that
argument depends on, so removing one fails a test rather than passing silently.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from student.ai import AIEngine
from student.api import StudentAPI
from student.db import Database
from student.generation import AIConceptExtractor, QuestionGenerator
from student.ingestion import IngestionEngine
from student.notifications import NotificationService
from student.validation import QuestionValidator

from test_student_e2e import _Scripted, _reply_for

ROOT = pathlib.Path(__file__).resolve().parent.parent
SENTINEL = "ZZ-NON-ROUTE-SENTINEL-ZZ"


@pytest.fixture
def world(tmp_path):
    db = Database(tmp_path / "q.db")
    provider = _Scripted(_reply_for)
    ai = AIEngine(db, provider_factory=lambda c: provider, development_candidate="cand-gen")
    engine = IngestionEngine(db, concept_extractor=AIConceptExtractor(db, ai),
                             storage_dir=tmp_path / "src")
    vai = AIEngine(db, provider_factory=lambda c: provider, development_candidate="cand-val")
    api = StudentAPI(db, engine=engine, ai=ai, generator=QuestionGenerator(db, ai),
                     validator=QuestionValidator(db, vai),
                     notifier=NotificationService(db, sender=lambda p: True))

    def register(email):
        return api.handle("POST", "/auth/register", {},
                          {"email": email, "password": "correct-horse"}, None)[1]["token"]

    a, b = register("a@example.com"), register("b@example.com")
    text = (f"{SENTINEL}. Pre-renal acute kidney injury arises from hypoperfusion. The "
            "fractional excretion of sodium is below one percent in pre-renal disease, "
            "whereas intrinsic renal injury shows values above two percent. " * 4)

    _, nb_a = api.handle("POST", "/notebooks", {}, {"title": "A", "subject": "Medicine"}, a)
    api.handle("POST", f"/notebooks/{nb_a['id']}/sources", {},
               {"kind": "text", "text": text}, a)
    _, nb_b = api.handle("POST", "/notebooks", {}, {"title": "B", "subject": "Medicine"}, b)
    api.handle("POST", f"/notebooks/{nb_b['id']}/sources", {}, {
        "kind": "text",
        "text": "Hepatic portal circulation and its tributaries described plainly. " * 12}, b)
    assert engine.wait_idle(60)

    yield {"api": api, "db": db, "engine": engine, "a": a, "b": b,
           "nb_a": nb_a["id"], "nb_b": nb_b["id"]}
    engine.stop()


# ---------------------------------------------------------------------------
# The background worker
# ---------------------------------------------------------------------------

def test_the_ingestion_worker_has_exactly_one_entry_point():
    """
    `IngestionEngine` queries `sources` and `source_chunks` by id with no owner
    column anywhere, and runs on a thread that has no user context. That is
    safe for exactly one reason: the only thing that enqueues work is
    `add_source`, which calls `_owned_notebook` first.

    Asserted structurally, because it is an argument about callers and a
    second caller added later would quietly invalidate it.
    """
    callers = []
    for path in sorted(pathlib.Path("student").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and getattr(node.func, "attr", "") in ("enqueue_source", "process_source")):
                fn = next((f.name for f in ast.walk(tree)
                           if isinstance(f, ast.FunctionDef)
                           and f.lineno <= node.lineno <= (f.end_lineno or f.lineno)), "?")
                callers.append(f"{path.name}:{fn}")

    assert sorted(set(callers)) == ["api.py:add_source", "ingestion.py:_worker"], (
        "something other than add_source now feeds the ingestion worker. The worker "
        "does no ownership checking of its own, so a new entry point needs one:\n  "
        + "\n  ".join(sorted(set(callers))))


def test_a_source_ingested_for_one_learner_produces_no_chunks_the_other_can_reach(world):
    api, db = world["api"], world["db"]
    rows = db.query(
        "SELECT ch.id FROM source_chunks ch"
        " JOIN sources s ON s.id = ch.source_id"
        " JOIN notebooks n ON n.id = s.notebook_id"
        " WHERE n.owner_id = (SELECT owner_id FROM notebooks WHERE id = ?)"
        "   AND ch.text LIKE ?", (world["nb_b"], f"%{SENTINEL}%"))
    assert rows == [], "A's ingested text landed under B's ownership"


# ---------------------------------------------------------------------------
# Helper functions whose safety is a property of their caller
# ---------------------------------------------------------------------------

def test_validate_pending_without_a_notebook_reaches_every_learners_questions(world):
    """
    A LATENT cross-user path, recorded rather than left implicit.

    `validate_pending(notebook_id=None)` selects every pending question in the
    database. Today the only caller is `generate_questions`, which passes a
    notebook it has already gated, so nothing reaches this. The default is the
    same shape as `_passages(owner_id="")` before it was made required: a
    caller that omits the argument gets everything.

    This test documents the current behaviour so that closing it is a
    deliberate change, and so that a second caller that omits the argument is
    not the moment anyone finds out.
    """
    api, db = world["api"], world["db"]
    for token, nb in ((world["a"], world["nb_a"]), (world["b"], world["nb_b"])):
        api.handle("POST", f"/notebooks/{nb}/questions", {},
                   {"count": 2, "validate": False}, token)

    pending = db.query("SELECT id, primary_notebook_id FROM questions"
                       " WHERE validation_status = 'pending'")
    owners = {r["primary_notebook_id"] for r in pending}
    assert owners == {world["nb_a"], world["nb_b"]}, "both learners must have pending work"

    scoped = api.validator.validate_pending(notebook_id=world["nb_a"], limit=50)
    assert sum(scoped.values()) <= 2, "scoped by notebook, it touches only that notebook"

    unscoped = api.validator.validate_pending(limit=50)
    assert sum(unscoped.values()) >= 1, (
        "validate_pending with no notebook_id crosses the ownership boundary. It is "
        "unreachable today because generate_questions always passes one -- if that "
        "stops being true, this is a cross-user write.")


def test_the_only_caller_of_validate_pending_passes_a_notebook():
    """The gate the test above depends on, pinned."""
    tree = ast.parse((ROOT / "student" / "api.py").read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "validate_pending"]
    assert calls, "validate_pending is no longer called from the API"
    for call in calls:
        assert any(kw.arg == "notebook_id" for kw in call.keywords), (
            f"api.py:{call.lineno} calls validate_pending without a notebook_id, which "
            "selects every learner's pending questions")


# ---------------------------------------------------------------------------
# Aggregates that are cross-user on purpose
# ---------------------------------------------------------------------------

def test_billing_aggregates_are_not_reachable_from_a_learner_route():
    """
    `cost_per_accepted`, `by_model`, `totals`, `revenue` and the budget figures
    sum across every user by design -- they answer "what does this cost us",
    not "what does this user owe". Scoping them would be wrong.

    What matters is that a learner cannot call them. They live behind
    `_admin`, and none of the admin routes takes a caller-supplied batch id.
    """
    source = (ROOT / "billing" / "api.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    admin = next(n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "_admin")
    admin_src = ast.get_source_segment(source, admin) or ""

    for aggregate in ("cost_per_500_accepted", "by_model", "plan_economics", "daily"):
        assert aggregate in admin_src, f"{aggregate} moved off the admin surface"
    assert "batch_id" not in admin_src, (
        "an admin route now takes a caller-supplied batch id; batches are written "
        "without an ownership check, so that would expose one learner's figures "
        "filed under another's batch")
