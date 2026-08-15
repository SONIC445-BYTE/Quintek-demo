"""
The /ai/* endpoints added for the NVIDIA model-registry/router architecture.
Separate file from test_analytics_api.py so the original frontend-contract
endpoints' test suite stays untouched (per the architecture spec: add, don't
break existing routes).
"""

from __future__ import annotations

import pytest

from benchmark import analytics as an
from benchmark.analytics_api import AnalyticsAPI
from benchmark.registry import Registry, Status
from benchmark.tasks import TaskType

from test_analytics import _gate, _write_run
from test_router import _promote, _register


@pytest.fixture
def api_with_registry(tmp_path):
    runs_root = tmp_path / "runs"
    registry_path = tmp_path / "registry.json"
    registry = Registry(registry_path)

    a = _register(registry, "model-a", ["question_generation"])
    _promote(registry, a.candidate_id)
    _write_run(runs_root, "run-a", a.candidate_id, "PASS",
              {"E_generation": _gate("E_generation", "GATE-E-RUBRIC", "mean_rubric_score",
                                     "PASS", estimate=0.94, n=300, required_n=300)},
              candidate_manifest={"provider": "nvidia", "model_id": a.candidate_id,
                                  "model_version": "1.0"})

    b = _register(registry, "model-b", ["question_generation"])
    _promote(registry, b.candidate_id)
    _write_run(runs_root, "run-b", b.candidate_id, "PASS",
              {"E_generation": _gate("E_generation", "GATE-E-RUBRIC", "mean_rubric_score",
                                     "PASS", estimate=0.97, n=300, required_n=300)},
              candidate_manifest={"provider": "nvidia", "model_id": b.candidate_id,
                                  "model_version": "1.0"})

    api = AnalyticsAPI(runs_root, registry_path=registry_path)
    return api, a.candidate_id, b.candidate_id


def test_ai_reliability_alias_matches_ai_overview(api_with_registry):
    api, a_id, _ = api_with_registry
    status, body = api.handle("/ai/reliability", {"candidate": [a_id]})
    assert status == 200
    assert body["currentCandidate"] == a_id
    assert body["status"] == "pass"


def test_ai_candidates_alias(api_with_registry):
    api, a_id, b_id = api_with_registry
    status, body = api.handle("/ai/candidates", {})
    assert status == 200
    ids = {c["candidateId"] for c in body["candidates"]}
    assert ids == {a_id, b_id}


def test_ai_how_it_works_is_static_and_never_names_a_winner(api_with_registry):
    api, _, _ = api_with_registry
    status, body = api.handle("/ai/how-it-works", {})
    assert status == 200
    assert len(body["steps"]) == 5
    text = str(body).lower()
    assert "best ai model" not in text
    assert "globally best" not in text


def test_ai_benchmark_summary_includes_registry_counts(api_with_registry):
    api, a_id, b_id = api_with_registry
    status, body = api.handle("/ai/benchmark", {})
    assert status == 200
    assert body["candidate_count"] == 2
    assert body["registry_status_counts"].get(Status.ELIGIBLE) == 2


def test_ai_leaderboard_overall_includes_provider_and_model(api_with_registry):
    api, a_id, b_id = api_with_registry
    status, body = api.handle("/ai/leaderboard", {})
    assert status == 200
    top = body["leaderboard"][0]
    assert top["candidate_id"] == b_id  # higher score
    assert top["provider"] == "nvidia"
    assert top["model"] == b_id


def test_ai_leaderboard_task_specific(api_with_registry):
    api, a_id, b_id = api_with_registry
    status, body = api.handle("/ai/leaderboard", {"task": ["QUESTION_GENERATION"]})
    assert status == 200
    assert body["task"] == "QUESTION_GENERATION"
    assert body["leaderboard"][0]["candidate_id"] == b_id
    assert body["leaderboard"][0]["rank"] == 1


def test_ai_leaderboard_unknown_task_is_400(api_with_registry):
    api, _, _ = api_with_registry
    status, body = api.handle("/ai/leaderboard", {"task": ["NOT_A_REAL_TASK"]})
    assert status == 400


def test_ai_routing_current_reflects_actual_router_decision(api_with_registry):
    api, a_id, b_id = api_with_registry
    status, body = api.handle("/ai/routing/current", {})
    assert status == 200
    assert body["routing_current"]["QUESTION_GENERATION"]["selected_candidate"] == b_id


def test_ai_routing_current_without_registry_configured(tmp_path):
    runs_root = tmp_path / "runs"
    _write_run(runs_root, "run-a", "cand-a", "PASS", {})
    api = AnalyticsAPI(runs_root)  # no registry_path
    status, body = api.handle("/ai/routing/current", {})
    assert status == 400


def test_ai_candidate_detail_includes_tracks_and_registry_entry(api_with_registry):
    api, a_id, _ = api_with_registry
    status, body = api.handle(f"/ai/candidates/{a_id}", {})
    assert status == 200
    assert body["candidateId"] == a_id
    assert any(t["track"] == "Question generation" for t in body["tracks"])
    assert body["registry"]["status"] == Status.ELIGIBLE


def test_ai_candidate_detail_unknown_candidate_404(api_with_registry):
    api, _, _ = api_with_registry
    status, body = api.handle("/ai/candidates/nope", {})
    assert status == 404


def test_ai_candidate_tasks_lists_what_it_currently_powers(api_with_registry):
    api, a_id, b_id = api_with_registry
    status, body = api.handle(f"/ai/candidates/{b_id}/tasks", {})
    assert status == 200
    assert body["candidate_id"] == b_id
    assert "QUESTION_GENERATION" in body["currently_routed_for"]

    status_a, body_a = api.handle(f"/ai/candidates/{a_id}/tasks", {})
    assert "QUESTION_GENERATION" not in body_a["currently_routed_for"]


def test_unknown_ai_candidates_subpath_is_404(api_with_registry):
    api, a_id, _ = api_with_registry
    status, body = api.handle(f"/ai/candidates/{a_id}/nonsense", {})
    assert status == 404


def test_original_api_routes_still_work_unchanged(api_with_registry):
    """The architecture spec is explicit: add /ai/*, don't break /api/*."""
    api, a_id, _ = api_with_registry
    status, body = api.handle("/api/ai-overview", {"candidate": [a_id]})
    assert status == 200
    status, body = api.handle("/api/candidates", {})
    assert status == 200
