"""
The published-evaluation view (benchmark/eval_api.py) and the metric-scale fix
it depends on.

Two properties matter most here:

  1. A metric on a 0-4 scale is never averaged with proportions un-normalized.
     That defect produced an "overall score" of 1.35 on a 0-1 scale.
  2. Anything not measured reports null, never zero. `costPer1k` and
     `latencyMs` are commercial/telemetry facts the benchmark does not
     produce, and a plausible zero is indistinguishable from a verified one.
"""

from __future__ import annotations

import json

import pytest

from benchmark import analytics as an
from benchmark.analytics_api import AnalyticsAPI
from benchmark.gates import GateRegistry, Measurement, evaluate_run
from benchmark.reports.scorecard import write_report

ROOT_CONFIG = "configs/gate_registry_v0_4.json"


def _full_measurements(reg, accuracy=0.97, rubric=3.6):
    ms = {}
    for key, spec in reg.tracks.items():
        n = spec["min_n"]
        if spec["ci"].startswith("bootstrap") and "rubric" in spec["metric"]:
            ms[key] = Measurement(values=[rubric] * n, n=n)
        elif spec["direction"] == "lower":
            ms[key] = Measurement(successes=int(n * accuracy), n=n)
        else:
            ms[key] = Measurement(successes=0, n=n)
    return ms


def _meta(run_id, candidate, ts):
    return {
        "run_id": run_id, "benchmark_version": "v0.4", "candidate_id": candidate,
        "candidate_manifest": {"provider": "nvidia", "model_id": "openai/gpt-oss-120b",
                               "model_version": "1.0", "system_prompt_hash": "v2",
                               "decoding_config": {"temperature": 0.0}, "code_commit": "abc"},
        "dataset_hash": "d1", "gate_registry_hash": "g1", "calibration_state": "UNCALIBRATED",
        "review_mode": "developmental", "reviewer_count": 1, "kappa_computable": True,
        "max_attainable_outcome": "NOT_VALID_FOR_PRODUCTION_PASS",
        "ceiling_reason": "1 of 2 reviewers", "timestamp": ts,
    }


@pytest.fixture
def scored_runs(tmp_path):
    reg = GateRegistry(ROOT_CONFIG)
    runs = tmp_path / "runs"
    outcome = evaluate_run(reg, _full_measurements(reg), integrity_ok=True)
    write_report(runs / "r1", outcome, _meta("r1", "cand-a", "2026-08-02T00:00:00Z"),
                 {"satisfied": True, "failed_checks": [], "passed_checks": [], "details": {}})
    return runs


# ---------------------------------------------------------------------------
# The scale bug
# ---------------------------------------------------------------------------

def test_rubric_gate_declares_its_scale_in_the_report():
    """GATE-E-RUBRIC is a mean rating out of 4. The registry has always said
    so via `scale_max`; the report must carry it so consumers can honour it."""
    reg = GateRegistry(ROOT_CONFIG)
    assert reg.tracks["E_generation"]["scale_max"] == 4.0
    outcome = evaluate_run(reg, _full_measurements(reg), integrity_ok=True)
    rubric = next(r for r in outcome.gate_results if r.gate_id == "GATE-E-RUBRIC")
    assert rubric.as_dict()["scale_max"] == 4.0
    accuracy = next(r for r in outcome.gate_results if r.gate_id == "GATE-A-ACC")
    assert accuracy.as_dict()["scale_max"] == 1.0


def test_overall_score_stays_within_its_own_scale(scored_runs):
    """
    REGRESSION. A 3.6/4 rubric mean averaged raw with ~0.97 proportions gave
    an overall of 1.35 -- a "score" above the top of its own scale, which
    rendered as 134.6% on a percentage axis.
    """
    archive = an.RunArchive(scored_runs)
    result = archive.latest_run_for_candidate("cand-a")
    score = an.ai_overview(result)["overallScore"]
    assert score is not None
    assert 0.0 <= score <= 1.0, f"overall score {score} escaped the 0-1 scale"


def test_confidence_interval_stays_within_its_own_scale(scored_runs):
    """Same defect on the interval: the rubric's raw band put the displayed
    upper bound at 360 on a 0-100 chart."""
    archive = an.RunArchive(scored_runs)
    result = archive.latest_run_for_candidate("cand-a")
    lo, hi = an.ai_overview(result)["confidenceInterval"]
    assert 0.0 <= lo <= 1.0 and 0.0 <= hi <= 1.0


def test_generation_track_reports_the_rubric_as_a_fraction_of_four(scored_runs):
    archive = an.RunArchive(scored_runs)
    result = archive.latest_run_for_candidate("cand-a")
    gen = next(t for t in an.student_track_results(result)
              if t["track"] == "Question generation")
    assert gen["score"] == pytest.approx(0.9, abs=0.01)  # 3.6 / 4


def test_all_error_rate_track_reports_a_score_instead_of_a_blank(scored_runs):
    """
    "Question validation" is only GATE-F-FALSEAPPROVE, an error rate. The
    direction filter that protects mixed groups left it with nothing to
    average, so the disclosure screen showed an empty cell beside a real
    measurement.
    """
    archive = an.RunArchive(scored_runs)
    result = archive.latest_run_for_candidate("cand-a")
    val = next(t for t in an.student_track_results(result)
              if t["track"] == "Question validation")
    assert val["score"] is not None
    assert val["confidenceInterval"] is not None
    assert val["confidenceInterval"][0] is not None


# ---------------------------------------------------------------------------
# Honest nulls
# ---------------------------------------------------------------------------

def test_cost_and_latency_are_null_when_unmeasured(scored_runs):
    api = AnalyticsAPI(scored_runs, gate_registry_path=ROOT_CONFIG)
    row = api.eval.candidates()[0]
    assert row["costPer1k"] is None, "an unpriced model must not report a cost"
    assert row["latencyMs"] is None, "a never-executed candidate must not report a latency"


def test_latency_comes_from_real_recorded_executions(scored_runs, tmp_path):
    from benchmark.orchestration import ExecutionLog, ExecutionRecord

    log_path = tmp_path / "executions.jsonl"
    log = ExecutionLog(log_path)
    for i, lat in enumerate([100.0, 200.0, 300.0]):
        log.record(ExecutionRecord(
            execution_id=f"e{i}", task_type="MEDICAL_QA", candidate_id="cand-a",
            provider="nvidia", model="openai/gpt-oss-120b", model_version="1.0",
            prompt_version="v2", timestamp="2026-08-02T00:00:00Z", latency_ms=lat,
            input_tokens=10, output_tokens=5, status="ok", error=None,
            routing_policy="QUALITY_FIRST"))
    api = AnalyticsAPI(scored_runs, gate_registry_path=ROOT_CONFIG,
                       execution_log_path=log_path)
    assert api.eval.candidates()[0]["latencyMs"] == 200  # median, not mean


def test_cost_is_read_from_an_operator_price_list(scored_runs, tmp_path):
    costs = tmp_path / "costs.json"
    costs.write_text(json.dumps({
        "nvidia/openai/gpt-oss-120b": {"usd_per_1k_output_tokens": 0.42}}))
    api = AnalyticsAPI(scored_runs, gate_registry_path=ROOT_CONFIG, costs_path=costs)
    assert api.eval.candidates()[0]["costPer1k"] == 0.42


def test_unmeasured_outcome_categories_are_null_not_zero(scored_runs):
    """The gate engine records a pass count and an n. It does not produce an
    invalid/unsafe/failedValidation taxonomy, so those report null -- zero
    would assert that we looked and found none."""
    api = AnalyticsAPI(scored_runs, gate_registry_path=ROOT_CONFIG)
    detail = api.eval.track_detail("cand-a")
    outcomes = detail["Medical QA"]["outcomes"]
    assert outcomes["correct"] is not None and outcomes["total"] == 500
    for unmeasured in ("invalid", "unsafe", "failedValidation", "humanReview"):
        assert outcomes[unmeasured] is None, f"{unmeasured} must be null, not a fabricated 0"


def test_critical_errors_come_from_the_report_safety_block(scored_runs):
    api = AnalyticsAPI(scored_runs, gate_registry_path=ROOT_CONFIG)
    row = api.eval.candidates()[0]
    assert row["criticalErrors"] == 0
    assert row["criticalN"] == 500
    assert row["criticalRate"] == 0.0


def test_suppressed_run_reports_null_safety_not_zero_events(tmp_path):
    reg = GateRegistry(ROOT_CONFIG)
    runs = tmp_path / "runs"
    invalid = evaluate_run(reg, _full_measurements(reg), integrity_ok=False,
                           integrity_failures=["holdout_isolation_verified"])
    write_report(runs / "bad", invalid, _meta("bad", "cand-b", "2026-08-03T00:00:00Z"),
                 {"satisfied": False, "failed_checks": ["holdout_isolation_verified"],
                  "passed_checks": [], "details": {}})
    api = AnalyticsAPI(runs, gate_registry_path=ROOT_CONFIG)
    row = api.eval.candidates()[0]
    assert row["criticalErrors"] is None, "a suppressed run measured no safety events"
    assert row["criticalRate"] is None


# ---------------------------------------------------------------------------
# Bundle / routes
# ---------------------------------------------------------------------------

def test_bundle_carries_every_key_the_frontend_module_exports(scored_runs):
    api = AnalyticsAPI(scored_runs, gate_registry_path=ROOT_CONFIG)
    status, body = api.handle("/ai/eval", {})
    assert status == 200
    for key in ("state", "overview", "tracks", "candidates", "history",
                "failures", "cases", "runs", "trackDetail", "overallByCandidate"):
        assert key in body, f"quintek-eval-api.js exports '{key}' with nothing to bind it to"


def test_percentages_are_on_the_hundred_scale(scored_runs):
    api = AnalyticsAPI(scored_runs, gate_registry_path=ROOT_CONFIG)
    _, body = api.handle("/ai/eval", {})
    assert 0 <= body["overview"]["overallScore"] <= 100
    for track in body["tracks"]:
        if track["score"] is not None:
            assert 0 <= track["score"] <= 100


def test_empty_archive_reports_empty_state_not_a_fake_candidate(tmp_path):
    api = AnalyticsAPI(tmp_path / "runs", gate_registry_path=ROOT_CONFIG)
    _, body = api.handle("/ai/eval", {})
    assert body["state"] == "empty"
    assert body["candidates"] == []
    assert body["overview"] is None
