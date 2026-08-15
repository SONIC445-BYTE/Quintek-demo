"""
Reference analytics JSON API (benchmark/analytics_api.py).

Tests the framework-agnostic AnalyticsAPI.handle() core directly (fast, no
socket), plus one real end-to-end test that starts the stdlib HTTP server on
a background thread and hits it over a real socket with urllib, so the
transport layer isn't just assumed to work.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from benchmark import analytics as an
from benchmark.analytics_api import AnalyticsAPI, make_handler

from test_analytics import _gate, _write_run  # reuse fixtures' builders


@pytest.fixture
def api(tmp_path):
    runs_root = tmp_path / "runs"
    scores_a = {"A_medical_qa": _gate("A_medical_qa", "GATE-A-ACC", "accuracy", "PASS",
                                      estimate=0.94, n=500, required_n=500,
                                      ci_lower=0.918, ci_upper=0.96)}
    scores_b = {"A_medical_qa": _gate("A_medical_qa", "GATE-A-ACC", "accuracy", "PASS",
                                      estimate=0.97, n=500, required_n=500)}
    _write_run(runs_root, "run-a", "cand-a", "PASS", scores_a,
              candidate_manifest={"provider": "acme", "model_id": "m1", "model_version": "v1"})
    _write_run(runs_root, "run-b", "cand-b", "PASS", scores_b,
              candidate_manifest={"provider": "acme", "model_id": "m2", "model_version": "v1"})
    return AnalyticsAPI(runs_root)


def test_candidates_endpoint(api):
    status, body = api.handle("/api/candidates", {})
    assert status == 200
    ids = {c["candidateId"] for c in body["candidates"]}
    assert ids == {"cand-a", "cand-b"}


def test_leaderboard_endpoint(api):
    status, body = api.handle("/api/leaderboard", {})
    assert status == 200
    assert len(body["leaderboard"]) == 2
    assert body["leaderboard"][0]["rank"] == 1


def test_ai_overview_requires_candidate_param(api):
    status, body = api.handle("/api/ai-overview", {})
    assert status == 400
    assert "candidate" in body["error"]


def test_ai_overview_unknown_candidate_is_404(api):
    status, body = api.handle("/api/ai-overview", {"candidate": ["nope"]})
    assert status == 404


def test_ai_overview_known_candidate(api):
    status, body = api.handle("/api/ai-overview", {"candidate": ["cand-a"]})
    assert status == 200
    assert body["currentCandidate"] == "cand-a"
    assert body["status"] == "pass"


def test_tracks_endpoint(api):
    status, body = api.handle("/api/tracks", {"candidate": ["cand-a"]})
    assert status == 200
    assert any(t["track"] == "Medical knowledge" for t in body["tracks"])


def test_compare_endpoint(api):
    status, body = api.handle("/api/compare", {"a": ["cand-a"], "b": ["cand-b"]})
    assert status == 200
    assert body["candidate_a"]["candidate_id"] == "cand-a"
    assert body["candidate_b"]["candidate_id"] == "cand-b"


def test_compare_missing_param(api):
    status, body = api.handle("/api/compare", {"a": ["cand-a"]})
    assert status == 400


def test_compare_unknown_candidate_404(api):
    status, body = api.handle("/api/compare", {"a": ["cand-a"], "b": ["ghost"]})
    assert status == 404


def test_failures_endpoint_with_records(tmp_path):
    runs_root = tmp_path / "runs"
    _write_run(runs_root, "run-a", "cand-a", "PASS", {})
    failures = [
        an.FailureRecord(run_id="run-a", candidate_id="cand-a", track="A_medical_qa",
                         item_id="i1", category="wrong_answer", severity="high"),
    ]
    api = AnalyticsAPI(runs_root, failures=failures)
    status, body = api.handle("/api/failures", {"candidate": ["cand-a"], "total_n": ["50"]})
    assert status == 200
    assert body["count"] == 1
    assert body["rate"] == pytest.approx(0.02)


def test_failures_endpoint_empty_by_default(api):
    status, body = api.handle("/api/failures", {})
    assert status == 200
    assert body["count"] == 0
    assert body["cases"] == []


def test_routing_endpoint_without_log_configured(api):
    status, body = api.handle("/api/routing", {})
    assert status == 200
    assert body["routing"] == []


def test_routing_endpoint_with_log(tmp_path):
    runs_root = tmp_path / "runs"
    _write_run(runs_root, "run-a", "cand-a", "PASS", {})
    log_path = tmp_path / "routing.jsonl"
    log = an.RoutingLog(log_path)
    log.record(an.RoutingDecision(
        execution_id="e1", task="question_generation", selected_candidate="cand-a",
        eligible_candidates=["cand-a"], routing_policy="highest_track_score",
        benchmark_evidence={}, timestamp="2026-08-15T00:00:00Z",
    ))
    api = AnalyticsAPI(runs_root, routing_log_path=log_path)
    status, body = api.handle("/api/routing", {"task": ["question_generation"]})
    assert status == 200
    assert len(body["routing"]) == 1


def test_unknown_endpoint_is_404(api):
    status, body = api.handle("/api/nonsense", {})
    assert status == 404


def test_handler_error_returns_500_not_a_fabricated_body(tmp_path):
    """An internal error must surface as an error response, never as data
    that looks like a real result."""
    api = AnalyticsAPI(tmp_path / "does-not-exist")
    # archive over a nonexistent root just returns no candidates -- exercise
    # a genuine exception path via a malformed failures filter instead.
    status, body = api.handle("/api/failures", {"total_n": ["not-a-number"]})
    assert status == 500
    assert "error" in body


# ---------------------------------------------------------------------------
# Real HTTP round trip
# ---------------------------------------------------------------------------

def test_real_http_server_round_trip(tmp_path):
    runs_root = tmp_path / "runs"
    scores = {"A_medical_qa": _gate("A_medical_qa", "GATE-A-ACC", "accuracy", "PASS",
                                    estimate=0.94, n=500, required_n=500)}
    _write_run(runs_root, "run-a", "cand-a", "PASS", scores)

    api = AnalyticsAPI(runs_root)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(api))
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/ai-overview?candidate=cand-a", timeout=5
        ) as resp:
            assert resp.status == 200
            body = json.loads(resp.read())
            assert body["currentCandidate"] == "cand-a"
            assert body["status"] == "pass"

        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ai-overview", timeout=5)
        assert exc_info.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
