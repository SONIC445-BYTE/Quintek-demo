"""
Orchestration layer: routing -> provider call -> provenance, with fallback.

Uses ScriptedProvider (already built for harness testing) as the
provider_factory's return value, so these tests exercise the real
Orchestrator/CallLimiter/ExecutionLog logic without any network dependency.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from benchmark import analytics as an
from benchmark.orchestration import CallLimiter, ExecutionLog, Orchestrator
from benchmark.providers.scripted import ScriptedProvider
from benchmark.registry import Registry, Status
from benchmark.router import RoutingPolicy
from benchmark.tasks import TaskType

from test_router import _gate, _promote, _register, _write_run  # reuse fixtures' helpers


@pytest.fixture
def env(tmp_path):
    registry = Registry(tmp_path / "registry.json")
    runs_root = tmp_path / "runs"
    archive = an.RunArchive(runs_root)
    execution_log = ExecutionLog(tmp_path / "executions.jsonl")
    routing_log = an.RoutingLog(tmp_path / "routing.jsonl")
    return registry, runs_root, archive, execution_log, routing_log


def _seed_candidate(registry, runs_root, model_id, score=0.95, capabilities=None,
                    accuracy=1.0, error_items=None):
    c = _register(registry, model_id, capabilities or ["question_generation"])
    _promote(registry, c.candidate_id)
    _write_run(runs_root, f"run-{model_id}", c.candidate_id, "PASS",
              {"E_generation": _gate("E_generation", "GATE-E-RUBRIC", "mean_rubric_score",
                                     "PASS", estimate=score)})
    return c


def test_successful_generation_records_ok_execution(env):
    registry, runs_root, archive, exec_log, routing_log = env
    cand = _seed_candidate(registry, runs_root, "model-a")

    def factory(candidate):
        return ScriptedProvider(accuracy=1.0)

    orch = Orchestrator(registry, archive, factory, exec_log, routing_log)
    resp, rec = orch.generate(TaskType.QUESTION_GENERATION, "generate a question")

    assert resp is not None
    assert resp.ok
    assert rec.status == "ok"
    assert rec.candidate_id == cand.candidate_id
    assert rec.fallback is False

    logged = exec_log.all()
    assert len(logged) == 1
    assert logged[0].execution_id == rec.execution_id


def test_no_eligible_candidate_records_and_returns_none(env):
    registry, runs_root, archive, exec_log, routing_log = env
    # nothing registered at all

    def factory(candidate):
        raise AssertionError("provider factory must not be called with no eligible candidate")

    orch = Orchestrator(registry, archive, factory, exec_log, routing_log)
    resp, rec = orch.generate(TaskType.QUESTION_GENERATION, "x")

    assert resp is None
    assert rec.status == "no_eligible_candidate"
    assert rec.candidate_id is None
    assert exec_log.all()[0].status == "no_eligible_candidate"


def test_provider_failure_falls_back_to_next_eligible_candidate(env):
    registry, runs_root, archive, exec_log, routing_log = env
    good = _seed_candidate(registry, runs_root, "model-good", score=0.90)
    bad = _seed_candidate(registry, runs_root, "model-bad", score=0.99)  # scores higher...

    class AlwaysFails(ScriptedProvider):
        def _call(self, request, timeout_seconds):
            raise ConnectionError("simulated upstream outage")

    def factory(candidate):
        if candidate.candidate_id == bad.candidate_id:
            p = AlwaysFails()
            p.retry_policy.max_retries = 0
            return p
        return ScriptedProvider(accuracy=1.0)

    orch = Orchestrator(registry, archive, factory, exec_log, routing_log)
    resp, rec = orch.generate(TaskType.QUESTION_GENERATION, "x", max_fallbacks=2)

    assert resp is not None
    assert resp.ok
    assert rec.candidate_id == good.candidate_id
    assert rec.fallback is True
    assert bad.candidate_id in rec.fallback_reason

    # Both the failed attempt and the successful fallback are on record --
    # nothing about the failure was silently dropped.
    all_records = exec_log.all()
    assert len(all_records) == 2
    assert all_records[0].candidate_id == bad.candidate_id
    assert all_records[0].status == "error"
    assert all_records[1].candidate_id == good.candidate_id
    assert all_records[1].status == "ok"


def test_exhausting_all_fallbacks_returns_none_but_logs_every_attempt(env):
    registry, runs_root, archive, exec_log, routing_log = env
    _seed_candidate(registry, runs_root, "model-a")
    _seed_candidate(registry, runs_root, "model-b")

    class AlwaysFails(ScriptedProvider):
        def _call(self, request, timeout_seconds):
            raise ConnectionError("simulated outage")

    def factory(candidate):
        p = AlwaysFails()
        p.retry_policy.max_retries = 0
        return p

    orch = Orchestrator(registry, archive, factory, exec_log, routing_log)
    resp, rec = orch.generate(TaskType.QUESTION_GENERATION, "x", max_fallbacks=1)

    assert resp is None
    assert rec.status == "error"
    assert len(exec_log.all()) == 2  # one per candidate tried


def test_routing_decisions_are_logged_alongside_executions(env):
    registry, runs_root, archive, exec_log, routing_log = env
    _seed_candidate(registry, runs_root, "model-a")

    def factory(candidate):
        return ScriptedProvider(accuracy=1.0)

    orch = Orchestrator(registry, archive, factory, exec_log, routing_log)
    resp, rec = orch.generate(TaskType.QUESTION_GENERATION, "x")

    decisions = routing_log.all()
    assert len(decisions) == 1
    assert decisions[0].execution_id == rec.execution_id
    assert decisions[0].selected_candidate == rec.candidate_id


# ---------------------------------------------------------------------------
# CallLimiter
# ---------------------------------------------------------------------------

def test_call_limiter_enforces_max_calls():
    limiter = CallLimiter(max_calls=2)
    limiter.acquire(); limiter.check_and_record(); limiter.release()
    limiter.acquire(); limiter.check_and_record(); limiter.release()
    limiter.acquire()
    with pytest.raises(RuntimeError):
        limiter.check_and_record()
    limiter.release()


def test_call_limiter_enforces_max_tokens():
    limiter = CallLimiter(max_tokens=100)
    limiter.acquire(); limiter.check_and_record(tokens_used=60); limiter.release()
    limiter.acquire()
    with pytest.raises(RuntimeError):
        limiter.check_and_record(tokens_used=60)
    limiter.release()


def test_call_limiter_bounds_concurrency():
    limiter = CallLimiter(max_concurrency=1)
    limiter.acquire()
    acquired_second = threading.Event()

    def try_acquire():
        limiter.acquire()
        acquired_second.set()
        limiter.release()

    t = threading.Thread(target=try_acquire, daemon=True)
    t.start()
    t.join(timeout=0.2)
    assert not acquired_second.is_set()  # blocked while the first holder has it
    limiter.release()
    t.join(timeout=1)
    assert acquired_second.is_set()


def test_orchestrator_stops_on_budget_exhaustion_without_retrying_forever(env):
    registry, runs_root, archive, exec_log, routing_log = env
    _seed_candidate(registry, runs_root, "model-a")

    def factory(candidate):
        return ScriptedProvider(accuracy=1.0)

    limiter = CallLimiter(max_calls=0)  # already exhausted
    orch = Orchestrator(registry, archive, factory, exec_log, routing_log, call_limiter=limiter)
    resp, rec = orch.generate(TaskType.QUESTION_GENERATION, "x")

    assert resp is None
    assert rec.status == "error"
    assert "budget exhausted" in rec.error


# ---------------------------------------------------------------------------
# ExecutionLog
# ---------------------------------------------------------------------------

def test_execution_log_is_append_only_jsonl(tmp_path):
    log = ExecutionLog(tmp_path / "executions.jsonl")
    from benchmark.orchestration import ExecutionRecord
    log.record(ExecutionRecord(
        execution_id="e1", task_type="QUESTION_GENERATION", candidate_id="c1",
        provider="nvidia", model="m", model_version="v", prompt_version="p1",
        timestamp="2026-08-15T00:00:00Z", latency_ms=12.0, input_tokens=10,
        output_tokens=5, status="ok", error=None, routing_policy="QUALITY_FIRST",
    ))
    lines_before = (tmp_path / "executions.jsonl").read_text().strip().splitlines()
    log.record(ExecutionRecord(
        execution_id="e2", task_type="QUESTION_GENERATION", candidate_id="c1",
        provider="nvidia", model="m", model_version="v", prompt_version="p1",
        timestamp="2026-08-15T00:01:00Z", latency_ms=15.0, input_tokens=10,
        output_tokens=6, status="ok", error=None, routing_policy="QUALITY_FIRST",
    ))
    lines_after = (tmp_path / "executions.jsonl").read_text().strip().splitlines()
    assert lines_after[0] == lines_before[0]
    assert len(lines_after) == 2
    assert len(log.for_candidate("c1")) == 2
