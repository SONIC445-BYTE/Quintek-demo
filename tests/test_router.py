"""
Deterministic Model Router.

This includes the exact acceptance scenario from the architecture spec
(section 24): three candidates, per-task scores, and a mandatory-safety-gate
failure that must exclude the highest scorer from routing regardless of its
raw score.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmark import analytics as an
from benchmark.registry import Registry, Status
from benchmark.router import Router, RoutingPolicy
from benchmark.tasks import TaskType


def _gate(track, gate_id, metric, status, estimate=None, n=500, required_n=500,
          direction="lower"):
    return {
        "gate_id": gate_id, "track": track, "metric": metric, "status": status,
        "estimate": estimate, "ci_lower": None, "ci_upper": None,
        "n": n, "required_n": required_n, "n_unit": "item", "threshold": 0.9,
        "direction": direction, "mandatory": True, "reason": "",
    }


def _write_run(root: Path, run_id: str, candidate_id: str, outcome: str, scores: dict,
                rankable: bool = True):
    run_dir = root / run_id
    run_dir.mkdir(parents=True)
    report = {
        "run_id": run_id, "benchmark_version": "v0.4", "candidate_id": candidate_id,
        "candidate_manifest": {"provider": "nvidia", "model_id": candidate_id},
        "dataset_hash": "x", "gate_registry_hash": "y", "outcome": outcome,
        "rankable": rankable, "integrity": {"satisfied": True, "failed_checks": []},
        "max_attainable_outcome": "PASS", "reasons": [], "scores": scores,
    }
    (run_dir / "report.json").write_text(json.dumps(report))
    (run_dir / "manifest.json").write_text(json.dumps(
        {"run_id": run_id, "timestamp": "2026-08-15T00:00:00Z"}))


def _promote(registry: Registry, candidate_id: str):
    for s in (Status.BENCHMARK_REQUIRED, Status.EVALUATING, Status.ELIGIBLE):
        registry.transition(candidate_id, s)


@pytest.fixture
def env(tmp_path):
    registry = Registry(tmp_path / "registry.json")
    runs_root = tmp_path / "runs"
    archive = an.RunArchive(runs_root)
    return registry, runs_root, archive


def _register(registry, model_id, capabilities):
    return registry.register("nvidia", model_id, "1.0", capabilities=capabilities,
                             candidate_id=model_id)


def test_no_candidates_registered_returns_none_with_reason(env):
    registry, runs_root, archive = env
    router = Router(registry, archive)
    result = router.select(TaskType.QUESTION_GENERATION)
    assert result.selected_candidate is None
    assert result.eligible_candidates == []
    assert "no candidate" in result.reason


def test_quality_first_picks_highest_task_score(env):
    registry, runs_root, archive = env
    a = _register(registry, "model-a", ["question_generation"])
    b = _register(registry, "model-b", ["question_generation"])
    _promote(registry, a.candidate_id)
    _promote(registry, b.candidate_id)

    _write_run(runs_root, "run-a", a.candidate_id, "PASS",
              {"E_generation": _gate("E_generation", "GATE-E-RUBRIC", "mean_rubric_score",
                                     "PASS", estimate=0.94)})
    _write_run(runs_root, "run-b", b.candidate_id, "PASS",
              {"E_generation": _gate("E_generation", "GATE-E-RUBRIC", "mean_rubric_score",
                                     "PASS", estimate=0.97)})

    router = Router(registry, archive)
    result = router.select(TaskType.QUESTION_GENERATION)
    assert result.selected_candidate == b.candidate_id
    assert set(result.eligible_candidates) == {a.candidate_id, b.candidate_id}


def test_acceptance_scenario_safety_override_beats_raw_score(env):
    """
    Exact scenario from the architecture spec section 24/9: candidate B
    scores highest on question generation but fails a mandatory safety gate
    -- it must be excluded from routing even though its raw score wins.
    """
    registry, runs_root, archive = env
    a = _register(registry, "model-a", ["question_generation", "knowledge_gap_detection"])
    b = _register(registry, "model-b", ["question_generation", "knowledge_gap_detection"])
    c = _register(registry, "model-c", ["question_generation", "knowledge_gap_detection"])
    for cand in (a, b, c):
        _promote(registry, cand.candidate_id)

    _write_run(runs_root, "run-a", a.candidate_id, "PASS", {
        "E_generation": _gate("E_generation", "GATE-E-RUBRIC", "mean_rubric_score", "PASS", estimate=0.94),
        "G_cross_subject": _gate("G_cross_subject", "GATE-G-LINK", "incorrect_link_rate", "PASS",
                                 estimate=0.04, direction="upper"),
    })
    # B has the best raw question-generation score...
    _write_run(runs_root, "run-b", b.candidate_id, "FAIL", {
        "E_generation": _gate("E_generation", "GATE-E-RUBRIC", "mean_rubric_score", "PASS", estimate=0.97),
        "safety_override_cme": _gate("safety_override_cme", "GATE-SAFETY-CME",
                                     "confirmed_critical_medical_errors", "FAIL",
                                     estimate=0.01, direction="equal"),
    })
    _write_run(runs_root, "run-c", c.candidate_id, "PASS", {
        "E_generation": _gate("E_generation", "GATE-E-RUBRIC", "mean_rubric_score", "PASS", estimate=0.91),
        "G_cross_subject": _gate("G_cross_subject", "GATE-G-LINK", "incorrect_link_rate", "PASS",
                                 estimate=0.09, direction="upper"),
    })

    router = Router(registry, archive)

    gen_result = router.select(TaskType.QUESTION_GENERATION)
    # ...but B is not even in the eligible pool, because its run outcome is
    # FAIL (confirmed CME). A wins despite a lower raw score than B.
    assert b.candidate_id not in gen_result.eligible_candidates
    assert gen_result.selected_candidate == a.candidate_id

    gap_result = router.select(TaskType.KNOWLEDGE_GAP_EXTRACTION)
    assert b.candidate_id not in gap_result.eligible_candidates
    # C has a worse incorrect-link rate than A, so A wins here too.
    assert gap_result.selected_candidate == a.candidate_id


def test_candidate_with_no_benchmark_run_is_never_selected(env):
    registry, runs_root, archive = env
    a = _register(registry, "model-a", ["question_generation"])
    _promote(registry, a.candidate_id)
    # No run written for model-a at all.
    router = Router(registry, archive)
    result = router.select(TaskType.QUESTION_GENERATION)
    assert result.selected_candidate is None


def test_registered_but_not_yet_eligible_candidate_is_excluded(env):
    registry, runs_root, archive = env
    a = _register(registry, "model-a", ["question_generation"])  # stays REGISTERED
    _write_run(runs_root, "run-a", a.candidate_id, "PASS",
              {"E_generation": _gate("E_generation", "GATE-E-RUBRIC", "mean_rubric_score",
                                     "PASS", estimate=0.99)})
    router = Router(registry, archive)
    result = router.select(TaskType.QUESTION_GENERATION)
    assert result.selected_candidate is None
    assert result.eligible_candidates == []


def test_missing_required_capability_excludes_candidate(env):
    registry, runs_root, archive = env
    a = _register(registry, "model-a", ["concept_extraction"])  # wrong capability
    _promote(registry, a.candidate_id)
    _write_run(runs_root, "run-a", a.candidate_id, "PASS",
              {"E_generation": _gate("E_generation", "GATE-E-RUBRIC", "mean_rubric_score",
                                     "PASS", estimate=0.99)})
    router = Router(registry, archive)
    result = router.select(TaskType.QUESTION_GENERATION)
    assert result.selected_candidate is None


def test_cost_optimized_picks_cheapest_among_eligible(env):
    registry, runs_root, archive = env
    a = _register(registry, "model-a", ["question_generation"])
    b = _register(registry, "model-b", ["question_generation"])
    _promote(registry, a.candidate_id)
    _promote(registry, b.candidate_id)
    _write_run(runs_root, "run-a", a.candidate_id, "PASS",
              {"E_generation": _gate("E_generation", "GATE-E-RUBRIC", "mean_rubric_score",
                                     "PASS", estimate=0.90)})
    _write_run(runs_root, "run-b", b.candidate_id, "PASS",
              {"E_generation": _gate("E_generation", "GATE-E-RUBRIC", "mean_rubric_score",
                                     "PASS", estimate=0.97)})
    router = Router(registry, archive)
    result = router.select(TaskType.QUESTION_GENERATION, policy=RoutingPolicy.COST_OPTIMIZED,
                           cost_hint={a.candidate_id: 0.002, b.candidate_id: 0.02})
    # B scores higher, but A is far cheaper and still cleared the benchmark.
    assert result.selected_candidate == a.candidate_id


def test_cost_optimized_without_hint_falls_back_to_quality_first(env):
    registry, runs_root, archive = env
    a = _register(registry, "model-a", ["question_generation"])
    _promote(registry, a.candidate_id)
    _write_run(runs_root, "run-a", a.candidate_id, "PASS",
              {"E_generation": _gate("E_generation", "GATE-E-RUBRIC", "mean_rubric_score",
                                     "PASS", estimate=0.9)})
    router = Router(registry, archive)
    result = router.select(TaskType.QUESTION_GENERATION, policy=RoutingPolicy.COST_OPTIMIZED)
    assert result.selected_candidate == a.candidate_id
    assert "fell back to QUALITY_FIRST" in result.reason


def test_experimental_policy_is_seeded_and_reproducible(env):
    registry, runs_root, archive = env
    a = _register(registry, "model-a", ["question_generation"])
    b = _register(registry, "model-b", ["question_generation"])
    _promote(registry, a.candidate_id)
    _promote(registry, b.candidate_id)
    for cid, cand in ((a.candidate_id, a), (b.candidate_id, b)):
        _write_run(runs_root, f"run-{cid}", cid, "PASS",
                  {"E_generation": _gate("E_generation", "GATE-E-RUBRIC", "mean_rubric_score",
                                         "PASS", estimate=0.9)})
    router = Router(registry, archive)
    r1 = router.select(TaskType.QUESTION_GENERATION, policy=RoutingPolicy.EXPERIMENTAL, seed=7)
    r2 = router.select(TaskType.QUESTION_GENERATION, policy=RoutingPolicy.EXPERIMENTAL, seed=7)
    assert r1.selected_candidate == r2.selected_candidate


def test_exclude_set_removes_a_candidate_for_fallback(env):
    registry, runs_root, archive = env
    a = _register(registry, "model-a", ["question_generation"])
    b = _register(registry, "model-b", ["question_generation"])
    _promote(registry, a.candidate_id)
    _promote(registry, b.candidate_id)
    _write_run(runs_root, "run-a", a.candidate_id, "PASS",
              {"E_generation": _gate("E_generation", "GATE-E-RUBRIC", "mean_rubric_score",
                                     "PASS", estimate=0.97)})
    _write_run(runs_root, "run-b", b.candidate_id, "PASS",
              {"E_generation": _gate("E_generation", "GATE-E-RUBRIC", "mean_rubric_score",
                                     "PASS", estimate=0.90)})
    router = Router(registry, archive)
    normal = router.select(TaskType.QUESTION_GENERATION)
    assert normal.selected_candidate == a.candidate_id

    fallback = router.select(TaskType.QUESTION_GENERATION, exclude={a.candidate_id})
    assert fallback.selected_candidate == b.candidate_id
    assert a.candidate_id not in fallback.eligible_candidates


def test_no_llm_call_anywhere_in_selection(env):
    """Structural check: Router has no provider/model-call dependency at all."""
    import inspect
    from benchmark import router as router_module
    source = inspect.getsource(router_module)
    for forbidden in ("generate(", "urlopen", "provider.generate"):
        assert forbidden not in source
