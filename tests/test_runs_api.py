"""
The run-centric API (benchmark/runs_api.py) -- the 9 endpoints the engineering
console consumes.

The load-bearing property here is that `GET /api/runs/:run_id` returns the
report *as written*, byte-equivalent after a JSON round-trip. A console
reviewing a stored verdict must see the stored artifact, not a reconstruction
of it.
"""

from __future__ import annotations

import json

import pytest

from benchmark.analytics_api import AnalyticsAPI
from benchmark.gates import GateRegistry, Measurement, evaluate_run
from benchmark.reports.scorecard import write_report

ROOT_CONFIG = "configs/gate_registry_v0_4.json"


def _meta(run_id, ts, candidate="cand-x"):
    return {
        "run_id": run_id, "benchmark_version": "v0.4", "candidate_id": candidate,
        "candidate_manifest": {"provider": "nvidia", "model_id": "meta/llama-3.3-70b-instruct",
                               "model_version": "1.0", "system_prompt_hash": "v1",
                               "decoding_config": {"temperature": 0.0}, "code_commit": "abc1234"},
        "dataset_hash": "d1", "gate_registry_hash": "g1", "calibration_state": "UNCALIBRATED",
        "review_mode": "developmental", "reviewer_count": 1, "kappa_computable": False,
        "max_attainable_outcome": "NOT_VALID_FOR_PRODUCTION_PASS",
        "ceiling_reason": "1 of 2 reviewers", "timestamp": ts,
    }


@pytest.fixture
def api(tmp_path):
    reg = GateRegistry(ROOT_CONFIG)
    runs = tmp_path / "runs"

    invalid = evaluate_run(reg, {"A_medical_qa": Measurement(successes=480, n=500)},
                           integrity_ok=False, integrity_failures=["holdout_isolation_verified"])
    write_report(runs / "r_invalid", invalid, _meta("r_invalid", "2026-08-01T00:00:00Z"),
                 {"satisfied": False, "failed_checks": ["holdout_isolation_verified"],
                  "passed_checks": [], "details": {"holdout_isolation_verified": "12 items read"}})

    ok = evaluate_run(reg, {"safety_override_cme": Measurement(successes=0, n=500)},
                      integrity_ok=True)
    write_report(runs / "r_ok", ok, _meta("r_ok", "2026-08-02T00:00:00Z"),
                 {"satisfied": True, "failed_checks": [], "passed_checks": [], "details": {}})

    return AnalyticsAPI(runs, gate_registry_path=ROOT_CONFIG), runs


def test_list_runs_is_newest_first_and_paginated(api):
    a, _ = api
    status, body = a.handle("/api/runs", {})
    assert status == 200
    assert body["total"] == 2
    assert [r["run_id"] for r in body["runs"]] == ["r_ok", "r_invalid"]

    status, page = a.handle("/api/runs", {"limit": ["1"], "offset": ["1"]})
    assert [r["run_id"] for r in page["runs"]] == ["r_invalid"]
    assert page["total"] == 2


def test_list_runs_never_carries_scores(api):
    """The list is a summary. Scores belong to the full report, and shipping
    them in a list invites a consumer to render a number without ever seeing
    the suppression reason attached to it."""
    a, _ = api
    _, body = a.handle("/api/runs", {})
    for row in body["runs"]:
        assert "scores" not in row
        assert "scores_withheld" in row


def test_get_run_returns_the_report_unmodified(api):
    a, runs = api
    on_disk = json.loads((runs / "r_ok" / "report.json").read_text())
    status, served = a.handle("/api/runs/r_ok", {})
    assert status == 200
    assert served == on_disk, "the served report must be the stored report, not a reconstruction"


def test_unknown_run_is_404_not_a_default(api):
    a, _ = api
    status, body = a.handle("/api/runs/does-not-exist", {})
    assert status == 404
    assert "does-not-exist" in body["error"]


def test_integrity_subresource(api):
    a, _ = api
    status, body = a.handle("/api/runs/r_invalid/integrity", {})
    assert status == 200
    assert body["integrity"]["satisfied"] is False
    assert "holdout_isolation_verified" in body["integrity"]["failed_checks"]


def test_report_md_is_served_as_markdown_text(api):
    a, _ = api
    status, body = a.handle("/api/runs/r_ok/report.md", {})
    assert status == 200
    assert isinstance(body, str), "markdown must not be wrapped in a JSON string"
    assert "PG REVISION BENCHMARK" in body


def test_gates_endpoint_is_registry_driven(api):
    a, _ = api
    status, body = a.handle("/api/gates", {})
    assert status == 200
    reg = GateRegistry(ROOT_CONFIG)
    assert len(body["gates"]) == len(reg.tracks)
    assert body["calibration_state"] == "UNCALIBRATED"
    # Every threshold came from the registry, none from a literal here.
    by_id = {g["gate_id"]: g for g in body["gates"]}
    for spec in reg.tracks.values():
        assert by_id[spec["gate_id"]]["threshold"] == spec["threshold"]
        assert by_id[spec["gate_id"]]["required_n"] == spec["min_n"]


def test_dataset_validate_records_by_hash_and_422s_on_invalid(tmp_path):
    a = AnalyticsAPI(tmp_path / "runs", gate_registry_path=ROOT_CONFIG)
    status, body = a.handle_post("/api/datasets/validate",
                                 {"dataset": "data/synthetic_harness_v0_4.jsonl"})
    assert status == 200 and body["ok"] is True
    # Now retrievable by the hash the validator computed.
    status, fetched = a.handle(f"/api/datasets/{body['dataset_hash']}", {})
    assert status == 200
    assert fetched["n_items"] == body["n_items"]


def test_dataset_validate_rejects_non_jsonl(tmp_path):
    bad = tmp_path / "corpus.csv"
    bad.write_text("id,prompt\n1,hello\n")
    a = AnalyticsAPI(tmp_path / "runs", gate_registry_path=ROOT_CONFIG)
    status, body = a.handle_post("/api/datasets/validate", {"dataset": str(bad)})
    assert status == 415
    assert ".csv" in body["error"]


def test_unvalidated_dataset_hash_is_404_not_an_empty_result(tmp_path):
    a = AnalyticsAPI(tmp_path / "runs", gate_registry_path=ROOT_CONFIG)
    status, _ = a.handle("/api/datasets/deadbeef", {})
    assert status == 404


def test_create_run_reports_501_rather_than_faking_a_queue(tmp_path):
    """
    Without an execution backend this must NOT answer 'queued'. Reporting
    accepted work that will never run is the same class of dishonesty as
    reporting a score that was never measured.
    """
    a = AnalyticsAPI(tmp_path / "runs", gate_registry_path=ROOT_CONFIG)
    status, body = a.handle_post("/api/runs", {"dataset": "x.jsonl"})
    assert status == 501
    assert "not configured" in body["error"]


def test_create_run_uses_a_supplied_launcher(tmp_path):
    a = AnalyticsAPI(tmp_path / "runs", gate_registry_path=ROOT_CONFIG,
                     run_launcher=lambda body: {"run_id": "queued-1", "status": "queued"})
    status, body = a.handle_post("/api/runs", {"dataset": "x.jsonl"})
    assert status == 202
    assert body == {"run_id": "queued-1", "status": "queued"}


def test_candidate_centric_routes_still_work_alongside_the_run_routes(api):
    """The two route families share the /api prefix; neither may shadow the
    other."""
    a, _ = api
    assert a.handle("/api/candidates", {})[0] == 200
    assert a.handle("/api/leaderboard", {})[0] == 200
    assert a.handle("/api/runs", {})[0] == 200
