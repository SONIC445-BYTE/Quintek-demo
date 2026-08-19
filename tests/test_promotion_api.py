"""
Tests for the benchmark -> production gate as an admin surface.

The interesting cases are all refusals. Promoting a passing run is the easy
path and is tested once; the rest of this file is about the ways a promotion
can be wrong and whether the system says so instead of accepting it.
"""

from __future__ import annotations

import json

import pytest

from benchmark.analytics import RunArchive
from benchmark.promotion_api import PromotionAPI, PromotionError
from student.ai import AIEngine
from student.db import Database


def write_run(root, run_id, candidate_id, outcome, *, integrity=True, scores=True,
              timestamp="2026-01-01T00:00:00Z"):
    d = root / run_id
    d.mkdir(parents=True, exist_ok=True)
    report = {
        "run_id": run_id,
        "candidate_id": candidate_id,
        "benchmark_version": "0.4",
        "outcome": outcome,
        "rankable": outcome in {"PASS", "CONDITIONAL"},
        "timestamp": timestamp,
        "integrity": {"satisfied": integrity},
        "dataset_hash": "abc",
        "gate_registry_hash": "def",
    }
    if scores:
        report["scores"] = {}
    (d / "report.json").write_text(json.dumps(report))
    return run_id


@pytest.fixture()
def api(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    db = Database(tmp_path / "student.db")
    engine = AIEngine(db)
    return PromotionAPI(engine, RunArchive(runs)), runs, engine


# ---------- the happy path ----------

def test_a_passing_run_can_be_promoted(api):
    promo, runs, engine = api
    write_run(runs, "run-pass", "cand-a", "PASS")
    result = promo.promote(task_type="QUESTION_GENERATION", run_id="run-pass",
                           activated_by="admin@example.test")
    assert result["candidate_id"] == "cand-a"
    assert engine.active_deployment("QUESTION_GENERATION")["candidate_id"] == "cand-a"


def test_promotion_changes_what_the_engine_resolves(api):
    promo, runs, engine = api
    with pytest.raises(Exception):
        engine.resolve("QUESTION_GENERATION")
    write_run(runs, "run-pass", "cand-a", "PASS")
    promo.promote(task_type="QUESTION_GENERATION", run_id="run-pass")
    assert engine.resolve("QUESTION_GENERATION") == ("cand-a", "promoted")


# ---------- refusals ----------

def test_a_failing_run_is_refused_with_its_outcome_named(api):
    promo, runs, _ = api
    write_run(runs, "run-fail", "cand-b", "FAIL")
    with pytest.raises(PromotionError, match="FAIL"):
        promo.promote(task_type="QUESTION_GENERATION", run_id="run-fail")


def test_a_conditional_run_without_signoff_is_refused(api):
    promo, runs, _ = api
    write_run(runs, "run-cond", "cand-c", "CONDITIONAL")
    with pytest.raises(PromotionError, match="own the accepted shortfall"):
        promo.promote(task_type="QUESTION_GENERATION", run_id="run-cond")


def test_a_conditional_run_with_a_named_signoff_is_allowed(api):
    promo, runs, _ = api
    write_run(runs, "run-cond", "cand-c", "CONDITIONAL")
    result = promo.promote(task_type="QUESTION_GENERATION", run_id="run-cond",
                           signoff_name="Dr Bose",
                           signoff_rationale="Latency shortfall accepted for the pilot cohort.")
    assert result["signoff_name"] == "Dr Bose"


def test_a_run_whose_integrity_failed_is_refused_even_if_it_says_pass(api):
    promo, runs, _ = api
    write_run(runs, "run-broken", "cand-d", "PASS", integrity=False)
    with pytest.raises(PromotionError, match="integrity"):
        promo.promote(task_type="QUESTION_GENERATION", run_id="run-broken")


def test_a_run_with_withheld_scores_is_refused(api):
    promo, runs, _ = api
    write_run(runs, "run-withheld", "cand-e", "PASS", scores=False)
    with pytest.raises(PromotionError, match="withheld"):
        promo.promote(task_type="QUESTION_GENERATION", run_id="run-withheld")


def test_an_unknown_run_is_refused(api):
    promo, _, _ = api
    with pytest.raises(PromotionError, match="no run"):
        promo.promote(task_type="QUESTION_GENERATION", run_id="run-imaginary")


def test_an_unknown_task_type_is_refused(api):
    promo, runs, _ = api
    write_run(runs, "run-pass", "cand-a", "PASS")
    with pytest.raises(PromotionError, match="unknown task type"):
        promo.promote(task_type="MAKE_TEA", run_id="run-pass")


def test_the_candidate_comes_from_the_run_not_the_request(api):
    """
    The API takes no candidate_id. There is no request that can promote model
    B on model A's passing run, because the field does not exist.
    """
    promo, runs, engine = api
    write_run(runs, "run-pass", "cand-a", "PASS")
    result = promo.promote(task_type="QUESTION_GENERATION", run_id="run-pass")
    assert result["candidate_id"] == "cand-a"


# ---------- explanation ----------

def test_eligible_lists_unpromotable_runs_with_reasons(api):
    promo, runs, _ = api
    write_run(runs, "run-pass", "cand-a", "PASS", timestamp="2026-01-02T00:00:00Z")
    write_run(runs, "run-fail", "cand-b", "FAIL", timestamp="2026-01-01T00:00:00Z")
    listing = promo.eligible("QUESTION_GENERATION")
    assert listing["promotable_count"] == 1
    by_id = {r["run_id"]: r for r in listing["runs"]}
    assert by_id["run-pass"]["promotable"] is True
    assert by_id["run-fail"]["promotable"] is False
    assert "FAIL" in by_id["run-fail"]["blocking_reason"]


def test_eligible_says_so_plainly_when_nothing_is_promotable(api):
    promo, runs, _ = api
    write_run(runs, "run-fail", "cand-b", "FAIL")
    listing = promo.eligible()
    assert listing["promotable_count"] == 0
    assert "development_override" in listing["note"]


def test_eligible_is_newest_first(api):
    promo, runs, _ = api
    write_run(runs, "old", "cand-a", "PASS", timestamp="2026-01-01T00:00:00Z")
    write_run(runs, "new", "cand-a", "PASS", timestamp="2026-06-01T00:00:00Z")
    assert [r["run_id"] for r in promo.eligible()["runs"]] == ["new", "old"]


# ---------- current state ----------

def test_current_reports_every_task_even_when_nothing_is_promoted(api):
    from benchmark.tasks import TaskType

    promo, _, _ = api
    state = promo.current()
    assert len(state["tasks"]) == len(list(TaskType))
    assert state["promoted_count"] == 0
    assert state["unresolved_count"] == len(list(TaskType))
    assert all(t["evidence_backed"] is False for t in state["tasks"])


def test_current_marks_a_development_override_as_not_evidence_backed(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    engine = AIEngine(Database(tmp_path / "s.db"), development_candidate="cand-dev")
    promo = PromotionAPI(engine, RunArchive(runs))
    task = promo.current()["tasks"][0]
    assert task["source"] == "development_override"
    assert task["evidence_backed"] is False


def test_current_marks_a_promoted_task_as_evidence_backed(api):
    promo, runs, _ = api
    write_run(runs, "run-pass", "cand-a", "PASS")
    promo.promote(task_type="EXPLANATION", run_id="run-pass")
    task = next(t for t in promo.current()["tasks"] if t["task_type"] == "EXPLANATION")
    assert task["source"] == "promoted"
    assert task["evidence_backed"] is True


# ---------- deactivation ----------

def test_deactivation_returns_the_task_to_the_router_and_keeps_the_record(api):
    promo, runs, engine = api
    write_run(runs, "run-pass", "cand-a", "PASS")
    promo.promote(task_type="EXPLANATION", run_id="run-pass")
    result = promo.deactivate("EXPLANATION", deactivated_by="admin@example.test")

    assert result["now_serving"] == "unresolved"
    assert engine.active_deployment("EXPLANATION") is None
    # The record survives: who deployed it and when must stay answerable.
    history = promo.history("EXPLANATION")["deployments"]
    assert len(history) == 1
    assert history[0]["deactivated_at"]
    assert history[0]["deactivated_by"] == "admin@example.test"


def test_deactivating_nothing_is_an_error_not_a_silent_success(api):
    promo, _, _ = api
    with pytest.raises(PromotionError, match="nothing is promoted"):
        promo.deactivate("EXPLANATION")


def test_promoting_twice_supersedes_rather_than_duplicates(api):
    promo, runs, engine = api
    write_run(runs, "run-1", "cand-a", "PASS", timestamp="2026-01-01T00:00:00Z")
    write_run(runs, "run-2", "cand-b", "PASS", timestamp="2026-02-01T00:00:00Z")
    promo.promote(task_type="EXPLANATION", run_id="run-1")
    promo.promote(task_type="EXPLANATION", run_id="run-2")

    assert engine.active_deployment("EXPLANATION")["candidate_id"] == "cand-b"
    history = promo.history("EXPLANATION")["deployments"]
    assert len(history) == 2
    assert sum(1 for d in history if d["deactivated_at"] is None) == 1


# ---------- HTTP adapter ----------

def test_http_get_routes(api):
    promo, runs, _ = api
    write_run(runs, "run-pass", "cand-a", "PASS")
    assert promo.handle_get("/api/promotions", {})[0] == 200
    assert promo.handle_get("/api/promotions/eligible", {"task": ["EXPLANATION"]})[0] == 200
    assert promo.handle_get("/api/promotions/history", {})[0] == 200
    assert promo.handle_get("/api/nope", {}) is None


def test_http_post_promote_and_deactivate(api):
    promo, runs, _ = api
    write_run(runs, "run-pass", "cand-a", "PASS")
    status, body = promo.handle_post("/api/promotions", {
        "task_type": "EXPLANATION", "run_id": "run-pass", "activated_by": "admin"})
    assert status == 201 and body["candidate_id"] == "cand-a"

    status, body = promo.handle_post("/api/promotions/deactivate", {"task_type": "EXPLANATION"})
    assert status == 200 and body["now_serving"] == "unresolved"


def test_http_post_refusal_is_a_400_with_a_reason(api):
    promo, runs, _ = api
    write_run(runs, "run-fail", "cand-b", "FAIL")
    status, body = promo.handle_post("/api/promotions", {
        "task_type": "EXPLANATION", "run_id": "run-fail"})
    assert status == 400
    assert "FAIL" in body["error"]
