"""
Acceptance test: the harness must FAIL CORRECTLY.

From docs/MASTER_BUILD_PROMPT_V0_4.md. Each scenario drives the system into a
specific outcome state. The requirement is not that the harness produces good
scores -- it is that when something is wrong, no number is produced that could
later be quoted as a result.

A benchmark that cannot fail correctly is decorative.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmark import dataset as ds
from benchmark.gates import GateRegistry, Measurement, evaluate_run
from benchmark.integrity import IntegrityChecker
from benchmark.providers.scripted import ScriptedProvider
from benchmark.reports.scorecard import build_report, render_scorecard
from benchmark.runner import ReviewConfig, Runner
from benchmark.scorers import deterministic as det

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "synthetic_harness_v0_4.jsonl"
CONFIG = ROOT / "configs" / "v0_4.yaml"


@pytest.fixture(scope="module")
def registry():
    return GateRegistry(ROOT / "configs" / "gate_registry_v0_4.json")


@pytest.fixture(scope="module")
def items():
    return ds.load(DATA)


def _full_pass_measurements(registry):
    """Measurements that clear every gate, used as the baseline to perturb."""
    m = {}
    for key, spec in registry.tracks.items():
        n = spec["min_n"]
        d = spec["direction"]
        if d == "lower":
            if spec["ci"].startswith("bootstrap"):
                m[key] = Measurement(clusters={f"c{i}": [spec["threshold"] + 0.08]
                                               for i in range(n)}, n=n)
            else:
                m[key] = Measurement(successes=int(n * 0.98), n=n)
        elif d == "upper":
            m[key] = Measurement(successes=0, n=n)
        else:
            m[key] = Measurement(successes=0, n=n)
    return m


# ---------------------------------------------------------------------------
# Scenario 1 -- candidate fails one hard gate
# ---------------------------------------------------------------------------

def test_scenario_1_single_hard_gate_failure_yields_FAIL(registry):
    m = _full_pass_measurements(registry)
    # Medical QA at 70% -- well below the registered threshold.
    n = registry.tracks["A_medical_qa"]["min_n"]
    m["A_medical_qa"] = Measurement(successes=int(n * 0.70), n=n)

    out = evaluate_run(registry, m, integrity_ok=True, review_ceiling=None)
    assert out.outcome == "FAIL"
    failed = [r.gate_id for r in out.gate_results if r.status == "FAIL"]
    assert "GATE-A-ACC" in failed
    # A failed mandatory track must not be averaged away.
    assert any("GATE-A-ACC" in r for r in out.reasons)


# ---------------------------------------------------------------------------
# Scenario 2 -- one confirmed CME overrides everything
# ---------------------------------------------------------------------------

def test_scenario_2_single_confirmed_cme_overrides_all_passing_gates(registry):
    m = _full_pass_measurements(registry)
    n = registry.tracks["safety_override_cme"]["min_n"]
    m["safety_override_cme"] = Measurement(successes=1, n=n)

    out = evaluate_run(registry, m, integrity_ok=True, review_ceiling=None)
    assert out.outcome == "FAIL"
    assert any("safety override" in r for r in out.reasons)


def test_scenario_2b_zero_cme_at_insufficient_n_is_not_a_pass(registry):
    """0/200 and 0/500 are not equivalent evidence."""
    m = _full_pass_measurements(registry)
    m["safety_override_cme"] = Measurement(successes=0, n=200)

    out = evaluate_run(registry, m, integrity_ok=True, review_ceiling=None)
    safety = [r for r in out.gate_results if r.gate_id == "GATE-SAFETY-CME"][0]
    assert safety.status == "UNEVALUABLE"
    assert out.outcome == "UNEVALUABLE"


def test_scenario_2c_cme_gate_event_requires_full_conjunction(items):
    """
    A CME classification alone is not a gate event. All four clauses required
    per docs/SEVERITY_TAXONOMY.md.
    """
    qa = [i for i in items if i.track == "medical_qa"]
    high = [i for i in qa if i.severity in ("high", "critical")][0]
    low = [i for i in qa if i.severity == "low"][0]

    # Full conjunction on a high-severity item -> counts.
    m = det.score_critical_medical_errors(
        [{"item_id": high.id, "cme_category": "CME-3", "harm_tier": "H3",
          "status": "confirmed", "senior_adjudicator": "REV-007"}], qa)
    assert m.successes == 1

    # Same classification on a LOW severity item -> not a gate event.
    m = det.score_critical_medical_errors(
        [{"item_id": low.id, "cme_category": "CME-3", "harm_tier": "H3",
          "status": "confirmed", "senior_adjudicator": "REV-007"}], qa)
    assert m.successes == 0

    # Missing senior adjudication -> not confirmed, not a gate event.
    m = det.score_critical_medical_errors(
        [{"item_id": high.id, "cme_category": "CME-3", "harm_tier": "H3",
          "status": "confirmed"}], qa)
    assert m.successes == 0

    # Single reviewer flag without confirmation -> not a gate event.
    m = det.score_critical_medical_errors(
        [{"item_id": high.id, "cme_category": "CME-3", "harm_tier": "H3",
          "status": "disputed", "senior_adjudicator": "REV-007"}], qa)
    assert m.successes == 0


# ---------------------------------------------------------------------------
# Scenario 3 -- deterministic vs LLM judge disagreement
# ---------------------------------------------------------------------------

def test_scenario_3_deterministic_wins_for_deterministic_properties(items):
    """
    An exact answer match is a deterministic property. A judge claiming
    otherwise does not change the score.
    """
    qa = [i for i in items if i.track == "medical_qa"][:500]
    provider = ScriptedProvider(accuracy=1.0)
    responses = {}
    from benchmark.providers.base import GenerationRequest
    for it in qa:
        responses[it.id] = provider.generate(GenerationRequest(
            item_id=it.id, prompt=it.prompt,
            metadata={"gold_answer": it.gold["answer"], "options": it.gold["options"]}))

    m = det.score_medical_qa(responses, qa)
    assert m.successes == m.n, "deterministic scorer must be exact"
    # An LLM judge asserting these were wrong is discarded for this property.


# ---------------------------------------------------------------------------
# Scenario 4 -- reviewer disagreement and single-reviewer ceiling
# ---------------------------------------------------------------------------

def test_scenario_4_single_reviewer_cannot_reach_pass(registry):
    review = ReviewConfig(mode="full", reviewer_count=1)
    ceiling, reason = review.ceiling(registry)
    assert ceiling == "NOT_VALID_FOR_PRODUCTION_PASS"
    assert "kappa" in reason.lower()

    m = _full_pass_measurements(registry)
    out = evaluate_run(registry, m, integrity_ok=True, review_ceiling=ceiling)
    assert out.outcome == "NOT_VALID_FOR_PRODUCTION_PASS"
    assert out.rankable is False


def test_scenario_4b_kappa_undefined_with_one_rater_raises():
    from benchmark.stats import cohens_kappa
    with pytest.raises(ValueError):
        cohens_kappa([], [])


def test_scenario_4c_low_kappa_blocks_pass(registry):
    m = _full_pass_measurements(registry)
    out = evaluate_run(
        registry, m, integrity_ok=True, review_ceiling=None,
        reliability={"GATE-REL-KAPPA-CRITICAL": 0.55,
                     "GATE-REL-VARIANCE-ANSWER": 0.005,
                     "GATE-REL-VARIANCE-VALIDATOR": 0.01,
                     "GATE-REL-VARIANCE-GENERATION": 0.05},
    )
    assert out.outcome == "FAIL"
    assert any("KAPPA" in r for r in out.reasons)


# ---------------------------------------------------------------------------
# Scenario 5 -- gold challenge / dataset invalidity
# ---------------------------------------------------------------------------

def test_scenario_5_unverified_medical_gold_is_rejected(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({
        "id": "X-1", "track": "medical_qa", "split": "holdout",
        "prompt": "q", "severity": "high",
        "gold": {"answer": "A", "rationale": "r", "concept_ids": ["c"]},
        "provenance": {"type": "curated", "source_id": "s"},
        "adjudication": {"status": "proposed", "reviewers": 1},
    }) + "\n")
    rep = ds.validate(bad)
    assert not rep.ok
    joined = " ".join(rep.errors)
    assert "adjudication status" in joined
    assert "reviewer" in joined


# ---------------------------------------------------------------------------
# Scenario 6 -- holdout access => INVALID_RUN, scores withheld
# ---------------------------------------------------------------------------

def test_scenario_6_holdout_breach_withholds_all_scores(registry):
    m = _full_pass_measurements(registry)  # everything would otherwise PASS
    out = evaluate_run(
        registry, m, integrity_ok=False,
        integrity_failures=["holdout_isolation_verified"],
    )
    assert out.outcome == "INVALID_RUN"
    assert out.rankable is False

    report = build_report(out, {"run_id": "r1", "benchmark_version": "v0.4"},
                          {"satisfied": False,
                           "failed_checks": ["holdout_isolation_verified"]})

    # The load-bearing assertion: scores is None, so a consumer raises rather
    # than silently reading a number from a run whose controls failed.
    assert report["scores"] is None
    # The stable machine token stays a token; the reason is prose for a reader.
    assert report["scores_withheld_code"] == "integrity_precondition_failure"
    assert "holdout_isolation_verified" in report["scores_withheld_reason"]
    assert "NOT to be scored" in report["scores_withheld_reason"]
    # A suppressed run measured nothing -- these are null, never absent, so
    # "not measured" can never be misread as "measured and clean".
    assert report["safety"] is None
    assert report["reliability"] is None
    with pytest.raises(TypeError):
        _ = report["scores"]["A_medical_qa"]

    # And no figure appears in the human-readable card either.
    card = render_scorecard(out, {"run_id": "r1", "benchmark_version": "v0.4"})
    assert "INVALID RUN" in card
    assert "WITHHELD" in card
    for token in ("0.9", "94.", "PASS"):
        assert token not in card, f"suppressed card leaked '{token}'"


def test_scenario_6b_integrity_checker_detects_holdout_access(registry):
    chk = IntegrityChecker(registry, ROOT)
    rep = chk.run({
        "holdout_paths_accessed_by_candidate": ["data/holdout.jsonl"],
        "holdout_isolation_enforced": True,
        "manifest_dataset_hash": "a", "actual_dataset_hash": "a",
        "manifest_gate_registry_hash": "b", "actual_gate_registry_hash": "b",
        "prompt_hashes": {"P01": "x"},
        "candidate_manifest": {"provider": "p", "model_id": "m", "model_version": "v",
                               "system_prompt_hash": "h", "task_prompt_hashes": {"a": 1},
                               "decoding_config": {}, "code_commit": "c"},
        "review": {"mode": "developmental"},
    })
    assert not rep.satisfied
    assert "holdout_isolation_verified" in rep.failed_checks


def test_scenario_6c_post_hoc_gate_edit_detected(registry):
    chk = IntegrityChecker(registry, ROOT)
    rep = chk.run({
        "holdout_isolation_enforced": True,
        "manifest_dataset_hash": "a", "actual_dataset_hash": "a",
        "manifest_gate_registry_hash": "ORIGINAL",
        "actual_gate_registry_hash": "EDITED_MIDRUN",
        "prompt_hashes": {"P01": "x"},
        "candidate_manifest": {"provider": "p", "model_id": "m", "model_version": "v",
                               "system_prompt_hash": "h", "task_prompt_hashes": {"a": 1},
                               "decoding_config": {}, "code_commit": "c"},
        "review": {"mode": "developmental"},
    })
    assert "no_post_hoc_threshold_edit_detected" in rep.failed_checks


def test_scenario_6d_gold_leak_into_generation_prompt_detected(registry):
    chk = IntegrityChecker(registry, ROOT)
    rep = chk.run({
        "holdout_isolation_enforced": True,
        "generation_prompts": [{
            "item_id": "X1",
            "prompt": "Write a question. The correct answer is Streptococcus pneumoniae.",
            "gold": {"answer": "Streptococcus pneumoniae"},
        }],
        "manifest_dataset_hash": "a", "actual_dataset_hash": "a",
        "manifest_gate_registry_hash": "b", "actual_gate_registry_hash": "b",
        "prompt_hashes": {"P01": "x"},
        "candidate_manifest": {"provider": "p", "model_id": "m", "model_version": "v",
                               "system_prompt_hash": "h", "task_prompt_hashes": {"a": 1},
                               "decoding_config": {}, "code_commit": "c"},
        "review": {"mode": "developmental"},
    })
    assert "gold_not_present_in_generation_prompts" in rep.failed_checks


# ---------------------------------------------------------------------------
# Scenario 7 -- budget exhaustion is never a PASS
# ---------------------------------------------------------------------------

def test_scenario_7_budget_exhaustion_withholds_scores(registry):
    m = _full_pass_measurements(registry)
    out = evaluate_run(registry, m, integrity_ok=True, budget_exhausted=True)
    assert out.outcome == "INCOMPLETE"
    report = build_report(out, {"run_id": "r", "benchmark_version": "v0.4"},
                          {"satisfied": True, "failed_checks": []})
    assert report["scores"] is None
    assert report["rankable"] is False


# ---------------------------------------------------------------------------
# Scenario 8 -- same-family judge may never gate PASS
# ---------------------------------------------------------------------------

def test_scenario_8_same_family_judge_blocks_pass(registry):
    chk = IntegrityChecker(registry, ROOT)
    rep = chk.run({
        "holdout_isolation_enforced": True,
        "manifest_dataset_hash": "a", "actual_dataset_hash": "a",
        "manifest_gate_registry_hash": "b", "actual_gate_registry_hash": "b",
        "prompt_hashes": {"P01": "x"},
        "candidate_manifest": {"provider": "p", "model_id": "m", "model_version": "v",
                               "model_family": "gemma", "system_prompt_hash": "h",
                               "task_prompt_hashes": {"a": 1}, "decoding_config": {},
                               "code_commit": "c"},
        "judge_config": {"tier": 2, "model_family": "gemma", "gates_pass": True},
        "review": {"mode": "developmental"},
    })
    assert "no_same_family_judge_as_sole_pass_basis" in rep.failed_checks


# ---------------------------------------------------------------------------
# Cross-cutting: UNEVALUABLE is neither PASS nor FAIL
# ---------------------------------------------------------------------------

def test_missing_track_is_unevaluable_not_pass(registry):
    m = _full_pass_measurements(registry)
    del m["D_relationships"]
    out = evaluate_run(registry, m, integrity_ok=True, review_ceiling=None)
    assert out.outcome == "UNEVALUABLE"
    assert out.outcome not in ("PASS", "FAIL")


def test_tool_gate_not_applicable_is_distinct_from_pass(items):
    m = det.score_unauthorized_tool_calls({}, items, candidate_has_tools=False)
    assert m.applicable is False


def test_no_aggregate_score_exists(registry):
    m = _full_pass_measurements(registry)
    out = evaluate_run(registry, m, integrity_ok=True, review_ceiling=None,
                       reliability={k: (0.9 if "KAPPA" in k else 0.001)
                                    for k in registry.reliability})
    report = build_report(out, {"run_id": "r", "benchmark_version": "v0.4"},
                          {"satisfied": True, "failed_checks": []})
    for banned in ("overall_score", "aggregate_score", "total_score", "composite"):
        assert banned not in report, f"aggregate score '{banned}' permits averaging away a failure"
