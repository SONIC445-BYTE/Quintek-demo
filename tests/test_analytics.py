"""
Benchmark analytics data layer (sections 32-40 of the frontend/analytics spec).

These tests build report.json/manifest.json fixtures directly rather than
running a full Runner pass, so each test controls exactly which outcome
states and gate statuses it's exercising. The shape matches what
benchmark.reports.scorecard.build_report and benchmark.runner.Runner._meta
actually write -- see benchmark/analytics.py's RunArchive.load_run for the
field names this depends on.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmark import analytics as an


def _gate(track, gate_id, metric, status, estimate=None, n=0, required_n=0,
          ci_lower=None, ci_upper=None, mandatory=True, n_unit="item",
          direction="lower"):
    return {
        "gate_id": gate_id, "track": track, "metric": metric, "status": status,
        "estimate": estimate, "ci_lower": ci_lower, "ci_upper": ci_upper,
        "n": n, "required_n": required_n, "n_unit": n_unit, "threshold": 0.9,
        "direction": direction, "mandatory": mandatory, "reason": "",
    }


def _write_run(root: Path, run_id: str, candidate_id: str, outcome: str,
                scores: dict | None, *, rankable: bool = True,
                timestamp: str = "2026-08-15T00:00:00Z",
                manifest_extra: dict | None = None,
                candidate_manifest: dict | None = None):
    run_dir = root / run_id
    run_dir.mkdir(parents=True)
    report = {
        "run_id": run_id, "benchmark_version": "v0.4", "candidate_id": candidate_id,
        "candidate_manifest": candidate_manifest or {
            "provider": "acme", "model_id": "acme-1", "model_version": "2026-08",
        },
        "dataset_hash": "abc123", "gate_registry_hash": "def456",
        "outcome": outcome, "rankable": rankable,
        "integrity": {"satisfied": True, "failed_checks": []},
        "max_attainable_outcome": (manifest_extra or {}).get("max_attainable_outcome", "PASS"),
        "reasons": [],
        "scores": scores,
    }
    if scores is None:
        report["scores_withheld_reason"] = "integrity_precondition_failure"
    (run_dir / "report.json").write_text(json.dumps(report))
    manifest = {"run_id": run_id, "timestamp": timestamp}
    manifest.update(manifest_extra or {})
    (run_dir / "manifest.json").write_text(json.dumps(manifest))
    return run_dir


@pytest.fixture
def archive_root(tmp_path):
    return tmp_path / "runs"


# ---------------------------------------------------------------------------
# Status vocabulary mapping
# ---------------------------------------------------------------------------

def test_run_status_map_exhaustive_against_registry():
    reg = json.loads(Path("configs/gate_registry_v0_4.json").read_text())
    missing = set(reg["outcome_states"]) - set(an.RUN_STATUS_MAP)
    assert not missing, f"outcome states with no student-status mapping: {missing}"


def test_gate_status_map_covers_every_gates_module_status():
    from benchmark import gates
    module_statuses = {gates.PASS, gates.FAIL, gates.CONDITIONAL,
                       gates.UNEVALUABLE, gates.NOT_APPLICABLE}
    assert module_statuses <= set(an.GATE_STATUS_MAP)


def test_student_status_pass_review_fail_unavailable_are_the_only_values():
    for outcome in an.RUN_STATUS_MAP:
        assert an.student_status(outcome) in {"pass", "review", "fail", "unavailable"}


def test_unknown_outcome_defaults_to_unavailable_not_pass():
    """A future outcome state this map hasn't been updated for must never
    silently read as PASS."""
    assert an.student_status("SOME_FUTURE_STATE") == "unavailable"


# ---------------------------------------------------------------------------
# RunArchive / historical data (section 35)
# ---------------------------------------------------------------------------

def test_archive_loads_a_passing_run(archive_root):
    scores = {"A_medical_qa": _gate("A_medical_qa", "GATE-A-ACC", "accuracy", "PASS",
                                    estimate=0.94, n=500, required_n=500,
                                    ci_lower=0.918, ci_upper=0.96)}
    _write_run(archive_root, "run-1", "cand-a", "PASS", scores)
    arc = an.RunArchive(archive_root)
    runs = arc.all_runs()
    assert len(runs) == 1
    assert runs[0].run.outcome == "PASS"
    assert runs[0].student_status == "pass"
    assert runs[0].tracks[0].score == 0.94


def test_invalid_run_has_scores_withheld_and_no_tracks(archive_root):
    _write_run(archive_root, "run-2", "cand-a", "INVALID_RUN", None, rankable=False)
    arc = an.RunArchive(archive_root)
    result = arc.all_runs()[0]
    assert result.run.scores_withheld is True
    assert result.tracks == []
    assert result.student_status == "unavailable"


def test_candidate_to_runs_to_tracks_traversal(archive_root):
    """Section 35: candidate -> runs -> versions -> tracks."""
    scores1 = {"A_medical_qa": _gate("A_medical_qa", "GATE-A-ACC", "accuracy", "PASS",
                                     estimate=0.80, n=500, required_n=500)}
    scores2 = {"A_medical_qa": _gate("A_medical_qa", "GATE-A-ACC", "accuracy", "PASS",
                                     estimate=0.90, n=500, required_n=500)}
    _write_run(archive_root, "run-old", "cand-a", "PASS", scores1,
              timestamp="2026-01-01T00:00:00Z")
    _write_run(archive_root, "run-new", "cand-a", "PASS", scores2,
              timestamp="2026-06-01T00:00:00Z")
    arc = an.RunArchive(archive_root)
    history = arc.runs_for_candidate("cand-a")
    assert [r.run.run_id for r in history] == ["run-old", "run-new"]
    latest = arc.latest_run_for_candidate("cand-a")
    assert latest.run.run_id == "run-new"
    assert latest.tracks[0].score == 0.90


def test_archive_never_writes_to_run_directory(archive_root):
    _write_run(archive_root, "run-1", "cand-a", "PASS", {})
    run_dir = archive_root / "run-1"
    before = (run_dir / "report.json").read_text()
    arc = an.RunArchive(archive_root)
    arc.all_runs()
    arc.runs_for_candidate("cand-a")
    an.leaderboard(arc)
    after = (run_dir / "report.json").read_text()
    assert before == after


def test_malformed_run_is_skipped_not_raised(archive_root):
    good = {"A_medical_qa": _gate("A_medical_qa", "GATE-A-ACC", "accuracy", "PASS",
                                  estimate=0.9, n=500, required_n=500)}
    _write_run(archive_root, "run-good", "cand-a", "PASS", good)
    bad_dir = archive_root / "run-bad"
    bad_dir.mkdir()
    (bad_dir / "report.json").write_text("{not json")
    arc = an.RunArchive(archive_root)
    runs = arc.all_runs()
    assert len(runs) == 1
    assert runs[0].run.run_id == "run-good"


# ---------------------------------------------------------------------------
# Leaderboard: ranking != eligibility (section 36)
# ---------------------------------------------------------------------------

def test_ranking_never_implies_eligibility(archive_root):
    """
    The exact scenario section 36 calls out: a candidate can rank highly
    while being FAILED because it missed a mandatory gate.
    """
    high_score_but_failed = {
        "A_medical_qa": _gate("A_medical_qa", "GATE-A-ACC", "accuracy", "PASS",
                              estimate=0.99, n=500, required_n=500),
        "safety_override_cme": _gate("safety_override_cme", "GATE-SAFETY-CME",
                                     "confirmed_critical_medical_errors", "FAIL",
                                     estimate=0.02, n=500, required_n=500, direction="equal"),
    }
    lower_score_but_passed = {
        "A_medical_qa": _gate("A_medical_qa", "GATE-A-ACC", "accuracy", "PASS",
                              estimate=0.91, n=500, required_n=500),
    }
    _write_run(archive_root, "run-fail", "cand-danger", "FAIL", high_score_but_failed)
    _write_run(archive_root, "run-pass", "cand-safe", "PASS", lower_score_but_passed)

    arc = an.RunArchive(archive_root)
    board = an.leaderboard(arc)
    by_id = {e.candidate_id: e for e in board}

    # Ranks purely by ranking_score, so the higher (but FAILED) candidate
    # ranks first...
    assert board[0].candidate_id == "cand-danger"
    # ...yet is explicitly NOT production-eligible, while the lower-scoring
    # PASS candidate is.
    assert by_id["cand-danger"].production_eligible is False
    assert by_id["cand-safe"].production_eligible is True


def test_leaderboard_handles_candidate_with_no_measured_tracks(archive_root):
    _write_run(archive_root, "run-1", "cand-empty", "UNEVALUABLE", None, rankable=False)
    arc = an.RunArchive(archive_root)
    board = an.leaderboard(arc)
    assert board[0].ranking_score is None
    assert board[0].production_eligible is False


def test_candidate_result_production_eligible_uses_harness_definition(archive_root):
    """production_eligible must equal (outcome in {PASS, CONDITIONAL}) AND
    rankable -- exactly the harness's own definition, not derived from
    ranking_score."""
    scores = {"A_medical_qa": _gate("A_medical_qa", "GATE-A-ACC", "accuracy", "CONDITIONAL",
                                    estimate=0.895, n=500, required_n=500)}
    _write_run(archive_root, "run-1", "cand-a", "CONDITIONAL", scores, rankable=True)
    arc = an.RunArchive(archive_root)
    result = arc.all_runs()[0]
    assert result.production_eligible is True
    assert result.student_status == "review"


# ---------------------------------------------------------------------------
# Comparison API (section 37)
# ---------------------------------------------------------------------------

def test_compare_two_candidates(archive_root):
    a = {"A_medical_qa": _gate("A_medical_qa", "GATE-A-ACC", "accuracy", "PASS",
                               estimate=0.94, n=500, required_n=500)}
    b = {"A_medical_qa": _gate("A_medical_qa", "GATE-A-ACC", "accuracy", "PASS",
                               estimate=0.97, n=500, required_n=500)}
    _write_run(archive_root, "run-a", "cand-a", "PASS", a)
    _write_run(archive_root, "run-b", "cand-b", "PASS", b)
    arc = an.RunArchive(archive_root)
    result = an.compare(arc, "cand-a", "cand-b")
    d = result.as_dict()
    assert d["candidate_a"]["candidate_id"] == "cand-a"
    assert d["candidate_b"]["candidate_id"] == "cand-b"
    row = next(t for t in d["tracks"] if t["track"] == "A_medical_qa")
    assert row["a"]["score"] == 0.94
    assert row["b"]["score"] == 0.97


def test_compare_missing_candidate_raises(archive_root):
    _write_run(archive_root, "run-a", "cand-a", "PASS", {})
    arc = an.RunArchive(archive_root)
    with pytest.raises(KeyError):
        an.compare(arc, "cand-a", "cand-does-not-exist")


# ---------------------------------------------------------------------------
# Failure analytics (section 38)
# ---------------------------------------------------------------------------

def test_failure_analytics_filters_and_computes_rate():
    failures = [
        an.FailureRecord(run_id="r1", candidate_id="c1", track="A_medical_qa",
                         item_id="i1", category="wrong_answer", severity="low"),
        an.FailureRecord(run_id="r1", candidate_id="c1", track="A_medical_qa",
                         item_id="i2", category="wrong_answer", severity="high"),
        an.FailureRecord(run_id="r1", candidate_id="c2", track="D_relationships",
                         item_id="i3", category="missing_edge", severity="low"),
    ]
    result = an.failure_analytics(failures, total_n=100, candidate_id="c1")
    assert result.count == 2
    assert result.rate == pytest.approx(0.02)

    high_only = an.failure_analytics(failures, severity="high")
    assert high_only.count == 1
    assert high_only.cases[0]["item_id"] == "i2"


def test_failure_analytics_no_total_n_gives_no_rate():
    failures = [an.FailureRecord(run_id="r1", candidate_id="c1", track="t",
                                 item_id="i1", category="x")]
    result = an.failure_analytics(failures)
    assert result.count == 1
    assert result.rate is None


# ---------------------------------------------------------------------------
# Routing analytics (section 39) -- append-only
# ---------------------------------------------------------------------------

def test_routing_log_is_append_only_and_queryable(tmp_path):
    log = an.RoutingLog(tmp_path / "routing.jsonl")
    log.record(an.RoutingDecision(
        execution_id="exec-1", task="question_generation", selected_candidate="cand-b",
        eligible_candidates=["cand-a", "cand-b"], routing_policy="highest_track_score",
        benchmark_evidence={"run_id": "run-b", "track": "E_generation", "score": 0.97},
        timestamp="2026-08-15T00:00:00Z",
    ))
    log.record(an.RoutingDecision(
        execution_id="exec-2", task="validation", selected_candidate="cand-a",
        eligible_candidates=["cand-a"], routing_policy="highest_track_score",
        benchmark_evidence={"run_id": "run-a", "track": "F_validation", "score": 0.99},
        timestamp="2026-08-15T00:05:00Z", fallback=True, fallback_reason="only eligible candidate",
    ))
    all_decisions = log.all()
    assert len(all_decisions) == 2

    gen_only = log.query(task="question_generation")
    assert len(gen_only) == 1
    assert gen_only[0].selected_candidate == "cand-b"

    # Appending never rewrites a prior line.
    raw_lines = (tmp_path / "routing.jsonl").read_text().strip().splitlines()
    assert len(raw_lines) == 2
    first_line_before = raw_lines[0]
    log.record(an.RoutingDecision(
        execution_id="exec-3", task="robustness", selected_candidate="cand-a",
        eligible_candidates=["cand-a"], routing_policy="highest_track_score",
        benchmark_evidence={}, timestamp="2026-08-15T00:10:00Z",
    ))
    raw_lines_after = (tmp_path / "routing.jsonl").read_text().strip().splitlines()
    assert raw_lines_after[0] == first_line_before
    assert len(raw_lines_after) == 3


def test_routing_log_explains_why_a_model_was_used():
    """This is the concrete 'why did Quintek use this model' payload."""
    d = an.RoutingDecision(
        execution_id="exec-1", task="question_generation", selected_candidate="cand-b",
        eligible_candidates=["cand-a", "cand-b"], routing_policy="highest_track_score",
        benchmark_evidence={"run_id": "run-b", "track": "E_generation", "score": 0.97},
        timestamp="2026-08-15T00:00:00Z",
    )
    j = d.as_dict()
    assert j["benchmark_evidence"]["track"] == "E_generation"
    assert j["eligible_candidates"] == ["cand-a", "cand-b"]


# ---------------------------------------------------------------------------
# Report aggregation -- frontend data contract (section 40 / spec section 3)
# ---------------------------------------------------------------------------

def test_ai_overview_shape_matches_frontend_contract(archive_root):
    scores = {"A_medical_qa": _gate("A_medical_qa", "GATE-A-ACC", "accuracy", "PASS",
                                    estimate=0.94, n=500, required_n=500,
                                    ci_lower=0.918, ci_upper=0.96)}
    _write_run(archive_root, "run-1", "cand-a", "PASS", scores,
              timestamp="2026-08-12T00:00:00Z")
    arc = an.RunArchive(archive_root)
    result = arc.all_runs()[0]
    overview = an.ai_overview(result)
    for key in ("overallScore", "status", "benchmarkVersion", "evaluatedAt",
               "sampleSize", "confidenceInterval", "currentCandidate"):
        assert key in overview
    assert overview["status"] == "pass"
    assert overview["currentCandidate"] == "cand-a"
    assert overview["sampleSize"] == 500


def test_ai_overview_never_fabricates_a_score_when_withheld(archive_root):
    _write_run(archive_root, "run-1", "cand-a", "INVALID_RUN", None, rankable=False)
    arc = an.RunArchive(archive_root)
    result = arc.all_runs()[0]
    overview = an.ai_overview(result)
    assert overview["overallScore"] is None
    assert overview["status"] == "unavailable"


def test_student_track_results_groups_related_gates(archive_root):
    """Concept resolution's F1 and false-merge gates both feed the single
    student-facing 'Concept understanding' track; the worse status wins."""
    scores = {
        "C_concept_resolution_f1": _gate("C_concept_resolution_f1", "GATE-C-F1",
                                         "pairwise_macro_f1", "PASS",
                                         estimate=0.95, n=600, required_n=600),
        "C_concept_false_merge": _gate("C_concept_false_merge", "GATE-C-MERGE",
                                       "false_merge_rate", "FAIL",
                                       estimate=0.06, n=350, required_n=350, direction="upper"),
    }
    _write_run(archive_root, "run-1", "cand-a", "FAIL", scores)
    arc = an.RunArchive(archive_root)
    result = arc.all_runs()[0]
    tracks = an.student_track_results(result)
    concept = next(t for t in tracks if t["track"] == "Concept understanding")
    assert concept["status"] == "fail"  # the worse of PASS/FAIL wins
    assert concept["sampleSize"] == 600  # max n across the grouped gates
    # The false-merge rate (0.06, lower-is-better) must NOT be blended into
    # the score average with the F1 (0.95, higher-is-better) -- that would
    # silently drag a fine F1 score down by an unrelated error rate, the
    # same defect _ranking_score had. Only the F1 (direction="lower") feeds
    # the numeric score; the merge gate's failure still shows up via status.
    assert concept["score"] == pytest.approx(0.95)


def test_grouped_score_excludes_error_rate_gates_even_when_they_pass(archive_root):
    """Same check with the merge gate passing, to isolate the averaging bug
    from the status-selection logic: even a PASSING error-rate gate must not
    be blended into the numeric score."""
    scores = {
        "C_concept_resolution_f1": _gate("C_concept_resolution_f1", "GATE-C-F1",
                                         "pairwise_macro_f1", "PASS",
                                         estimate=0.80, n=600, required_n=600),
        "C_concept_false_merge": _gate("C_concept_false_merge", "GATE-C-MERGE",
                                       "false_merge_rate", "PASS",
                                       estimate=0.01, n=350, required_n=350, direction="upper"),
    }
    _write_run(archive_root, "run-1", "cand-a", "PASS", scores)
    arc = an.RunArchive(archive_root)
    result = arc.all_runs()[0]
    concept = next(t for t in an.student_track_results(result)
                  if t["track"] == "Concept understanding")
    # A naive mean would give (0.80 + 0.01) / 2 = 0.405.
    assert concept["score"] == pytest.approx(0.80)


def test_candidate_summary_shape_matches_frontend_contract(archive_root):
    scores = {"A_medical_qa": _gate("A_medical_qa", "GATE-A-ACC", "accuracy", "PASS",
                                    estimate=0.94, n=500, required_n=500)}
    _write_run(archive_root, "run-1", "cand-a", "PASS", scores,
              candidate_manifest={"provider": "acme", "model_id": "acme-1",
                                  "model_version": "2026-08"})
    arc = an.RunArchive(archive_root)
    result = arc.all_runs()[0]
    summary = an.candidate_summary(result)
    for key in ("candidateId", "provider", "model", "version", "overallScore",
               "status", "lastEvaluated"):
        assert key in summary
    assert summary["provider"] == "acme"
    assert summary["status"] == "pass"


# ---------------------------------------------------------------------------
# classify_case (section 34 correct/incorrect/invalid/failed)
# ---------------------------------------------------------------------------

def test_classify_case_failed_provider_error():
    assert an.classify_case({"error": "timeout", "parsed": None}, None) == "failed"


def test_classify_case_invalid_unparseable():
    assert an.classify_case({"error": None, "parsed": None}, None) == "invalid"


def test_classify_case_correct_and_incorrect():
    resp = {"error": None, "parsed": {"answer": "A"}}
    assert an.classify_case(resp, True) == "correct"
    assert an.classify_case(resp, False) == "incorrect"


def test_classify_case_requires_is_correct_when_parsed():
    with pytest.raises(ValueError):
        an.classify_case({"error": None, "parsed": {"answer": "A"}}, None)
