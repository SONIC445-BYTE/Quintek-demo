"""
The suite must not write to the shared execution log.

This is a guard, not a unit test. It asserts a property of the test RUN itself:
that nothing appended to the real `executions.jsonl` while the suite executed.

It earns its place because the contamination it guards against was invisible.
The log is gitignored, so `git status` stayed clean; the records named a real
provider, so nothing looked foreign; and the suite was green throughout.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from benchmark.orchestration import (PRODUCTION, ExecutionLog, ExecutionRecord,
                                     UnknownProviderName)
from conftest import REAL_EXECUTION_LOG

ROOT = Path(__file__).resolve().parent.parent


def _record(**kw) -> ExecutionRecord:
    base = dict(execution_id="e1", task_type="question_generation", candidate_id="c",
                provider="scripted", model="m", model_version="1", prompt_version="v1",
                timestamp="2026-01-01T00:00:00Z", latency_ms=1.0, input_tokens=1,
                output_tokens=1, status="ok", error=None, routing_policy="TEST")
    base.update(kw)
    return ExecutionRecord(**base)


# ---------------------------------------------------------------------------
# The redirect is actually in force
# ---------------------------------------------------------------------------

def test_the_execution_log_is_redirected_away_from_the_real_one():
    configured = os.environ.get("QUINTEK_EXECUTION_LOG")
    assert configured, "conftest must set QUINTEK_EXECUTION_LOG for the whole session"
    assert Path(configured).resolve() != Path(REAL_EXECUTION_LOG).resolve(), (
        "the suite is pointed at the real execution log")


def test_records_written_during_the_suite_are_labelled_test_origin():
    assert os.environ.get("QUINTEK_EXECUTION_ORIGIN") == "test-harness"


# ---------------------------------------------------------------------------
# origin separates measurements from everything else
# ---------------------------------------------------------------------------

def test_a_record_defaults_to_production():
    """The explicit claim is 'this is NOT real', never the other way round."""
    assert _record().origin == PRODUCTION


def test_measurements_excludes_non_production_records(tmp_path):
    log = ExecutionLog(tmp_path / "x.jsonl")
    log.record(_record(execution_id="real", latency_ms=1840.0))
    log.record(_record(execution_id="fake", latency_ms=12.0, origin="test-harness"))
    log.record(_record(execution_id="seed", latency_ms=99.0, origin="fixture"))

    assert [r.execution_id for r in log.all()] == ["real", "fake", "seed"], \
        "all() must still show everything, or the log stops being auditable"
    assert [r.execution_id for r in log.measurements()] == ["real"]
    assert [r.execution_id for r in log.for_candidate("c")] == ["real"], \
        "per-candidate telemetry must not average a fixture into a real latency"


def test_the_learner_facing_latency_ignores_test_records(tmp_path):
    """
    The concrete harm: EvalAPI._latency_for takes a MEDIAN, and that figure is
    served to a learner through /ai/eval. One scripted 12.0 ms among real
    calls moves it.
    """
    from benchmark.eval_api import EvalAPI

    path = tmp_path / "exec.jsonl"
    log = ExecutionLog(path)
    for i, lat in enumerate((1800.0, 1900.0, 2000.0)):
        log.record(_record(execution_id=f"r{i}", latency_ms=lat))
    for i in range(20):
        log.record(_record(execution_id=f"t{i}", latency_ms=12.0, origin="test-harness"))

    api = EvalAPI.__new__(EvalAPI)
    api.execution_log_path = path
    assert api._latency_for("c") == 1900, (
        "twenty test records dragged the median a learner is shown")


# ---------------------------------------------------------------------------
# a provider name that does not exist cannot be written
# ---------------------------------------------------------------------------

def test_an_unregistered_provider_name_is_refused(tmp_path):
    log = ExecutionLog(tmp_path / "x.jsonl")
    with pytest.raises(UnknownProviderName, match="no builder is registered"):
        log.record(_record(provider="totally-made-up"))
    assert not (tmp_path / "x.jsonl").exists(), "nothing may be written for a bogus name"


def test_every_registered_provider_name_is_accepted(tmp_path):
    from benchmark.providers.registry import available

    log = ExecutionLog(tmp_path / "x.jsonl")
    for name in available():
        log.record(_record(provider=name))
    assert len(log.all()) == len(available())


def test_the_guard_would_not_have_caught_the_openrouter_records(tmp_path):
    """
    Stated as a test so the limitation cannot be quietly forgotten.

    `openrouter` is a REAL registered provider -- benchmark/providers/registry.py
    builds an OpenRouter gateway under that name. The 229 contaminating records
    were a scripted double naming itself after it, so the name check accepts
    them and always would have. What separates them is `origin`, not the name.
    """
    log = ExecutionLog(tmp_path / "x.jsonl")
    log.record(_record(provider="openrouter", latency_ms=12.0))     # accepted
    assert len(log.measurements()) == 1, "the name check alone does not filter these"

    log2 = ExecutionLog(tmp_path / "y.jsonl")
    log2.record(_record(provider="openrouter", latency_ms=12.0, origin="test-harness"))
    assert log2.measurements() == [], "origin is what actually excludes them"


# ---------------------------------------------------------------------------
# the marking tool
# ---------------------------------------------------------------------------

def test_the_marking_tool_marks_and_never_deletes(tmp_path):
    path = tmp_path / "exec.jsonl"
    log = ExecutionLog(path)
    log.record(_record(execution_id="real", provider="nvidia", candidate_id="cand-real",
                       latency_ms=1840.0))
    for i in range(3):
        # The full signature the rule matches on. Deliberately exact: a rule
        # loose enough to be convenient here would be loose enough to mark a
        # real call in the production log.
        log.record(_record(execution_id=f"t{i}", provider="openrouter",
                           candidate_id="cand_x", latency_ms=12.0,
                           input_tokens=524, output_tokens=137,
                           model="inclusionai/ling-2.6-flash"))
    before = path.read_text().count("\n")

    out = subprocess.run(
        [sys.executable, str(ROOT / "tools_mark_test_records.py"), str(path), "--apply"],
        capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stderr

    after = ExecutionLog(path).all()
    assert len(after) == before, "retain-and-mark: the row count must not change"
    marked = {r.execution_id: r.origin for r in after}
    assert marked["real"] == PRODUCTION, "a real call must not be marked"
    assert all(marked[f"t{i}"] == "test-harness" for i in range(3))
    assert [r.execution_id for r in ExecutionLog(path).measurements()] == ["real"]


def test_the_marking_tool_reports_before_it_writes(tmp_path):
    path = tmp_path / "exec.jsonl"
    ExecutionLog(path).record(_record(execution_id="t0", provider="openrouter",
                                      candidate_id="cand_x", latency_ms=12.0,
                                      input_tokens=524, output_tokens=137,
                                      model="inclusionai/ling-2.6-flash"))
    original = path.read_text()
    out = subprocess.run(
        [sys.executable, str(ROOT / "tools_mark_test_records.py"), str(path)],
        capture_output=True, text=True, check=False)
    assert "1" in out.stdout
    assert path.read_text() == original, "a dry run must change nothing"


def test_a_real_call_that_merely_resembles_the_signature_is_left_alone(tmp_path):
    """
    The rule is narrow on purpose. A record from the same provider and the same
    candidate, but with a latency a real call could actually have, must survive
    untouched -- otherwise the tool is a way to quietly relabel measurements it
    finds inconvenient.
    """
    path = tmp_path / "exec.jsonl"
    ExecutionLog(path).record(_record(
        execution_id="plausible", provider="openrouter", candidate_id="cand_x",
        latency_ms=1743.2, input_tokens=524, output_tokens=137,
        model="inclusionai/ling-2.6-flash"))

    subprocess.run([sys.executable, str(ROOT / "tools_mark_test_records.py"),
                    str(path), "--apply"], capture_output=True, text=True, check=False)

    only = ExecutionLog(path).all()[0]
    assert only.origin == PRODUCTION, "a plausible latency must not be marked"
    assert ExecutionLog(path).measurements() == [only]
