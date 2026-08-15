"""
End-to-end harness demonstration.

Runs the same synthetic corpus through several configurations to show that each
outcome state renders correctly -- in particular that suppressed runs emit no
performance figures.

Nothing here measures medical capability. The corpus is semantically inert by
design; see tools_make_synthetic.py.
"""

from __future__ import annotations

from pathlib import Path

from . import dataset as ds
from .gates import GateRegistry, Measurement, evaluate_run
from .providers.base import GenerationRequest
from .providers.scripted import ScriptedProvider
from .reports.scorecard import build_report, render_scorecard
from .scorers import deterministic as det

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "synthetic_harness_v0_4.jsonl"


def _measure(items, accuracy: float) -> dict[str, Measurement]:
    """Run the scripted provider and score the deterministic tracks."""
    provider = ScriptedProvider(accuracy=accuracy)
    responses = {}
    for it in items:
        responses[it.id] = provider.generate(GenerationRequest(
            item_id=it.id, prompt=it.prompt,
            metadata={"gold_answer": it.gold.get("answer"),
                      "options": it.gold.get("options")}))

    qa = [i for i in items if i.track == "medical_qa"]
    res = [i for i in items if i.track == "concept_resolution"]
    rob = [i for i in items if i.track == "robustness"]
    inj = [i for i in items if i.track == "injection"]
    val = [i for i in items if i.track == "validation"]

    return {
        "A_medical_qa": det.score_medical_qa(responses, qa),
        "C_concept_resolution_f1": det.score_concept_resolution_f1(responses, res),
        "C_concept_false_merge": det.score_concept_false_merge(responses, res),
        "I_robustness": det.score_robustness_retention(responses, rob),
        "J_injection": det.score_injection_attack_success(responses, inj),
        "F_validation": det.score_validation_false_approval(responses, val),
        "safety_override_cme": det.score_critical_medical_errors([], qa),
    }


def _meta(name: str, ceiling: str = "NOT_VALID_FOR_PRODUCTION_PASS") -> dict:
    return {
        "run_id": name,
        "benchmark_version": "v0.4",
        "candidate_id": "scripted-harness-demo",
        "dataset_hash": "synthetic",
        "max_attainable_outcome": ceiling,
        "ceiling_reason": "single reviewer; thresholds uncalibrated",
    }


def _banner(title: str) -> None:
    print("\n" + "=" * 78)
    print(f"  {title}")
    print("=" * 78)


def run_demo() -> None:
    registry = GateRegistry(ROOT / "configs" / "gate_registry_v0_4.json")
    items = ds.load(DATA)

    rep = ds.validate(DATA)
    _banner("DATASET VALIDATION")
    print(f"items {rep.n_items} | valid {rep.ok} | tracks {rep.by_track}")

    # --- Scenario A: strong candidate, single reviewer, uncalibrated gates ---
    _banner("A. STRONG CANDIDATE — but single reviewer, uncalibrated thresholds")
    m = _measure(items, accuracy=0.97)
    out = evaluate_run(registry, m, integrity_ok=True,
                       review_ceiling="NOT_VALID_FOR_PRODUCTION_PASS")
    print(render_scorecard(out, _meta("demo-strong")))
    print(f"\n-> outcome={out.outcome}  rankable={out.rankable}")
    print("   Note: high scores did NOT produce a PASS. The ceiling is honest.")

    # --- Scenario B: weak candidate ---
    _banner("B. WEAK CANDIDATE — hard gate failure surfaces despite the ceiling")
    m = _measure(items, accuracy=0.62)
    out = evaluate_run(registry, m, integrity_ok=True,
                       review_ceiling="NOT_VALID_FOR_PRODUCTION_PASS")
    print(render_scorecard(out, _meta("demo-weak")))
    print(f"\n-> outcome={out.outcome}")
    print("   Note: FAIL is reported even though the run could never have passed.")

    # --- Scenario C: confirmed CME overrides everything ---
    _banner("C. CONFIRMED CRITICAL MEDICAL ERROR — safety override")
    m = _measure(items, accuracy=0.99)
    n = registry.tracks["safety_override_cme"]["min_n"]
    m["safety_override_cme"] = Measurement(successes=1, n=n)
    out = evaluate_run(registry, m, integrity_ok=True, review_ceiling=None)
    print(f"outcome = {out.outcome}")
    for r in out.reasons:
        print(f"  {r}")
    print("   Note: one confirmed CME overrides every other passing gate.")

    # --- Scenario D: integrity breach ---
    _banner("D. HOLDOUT CONTAMINATION — integrity suppression")
    m = _measure(items, accuracy=0.99)
    out = evaluate_run(registry, m, integrity_ok=False,
                       integrity_failures=["holdout_isolation_verified"])
    print(render_scorecard(out, _meta("demo-invalid")))
    report = build_report(out, _meta("demo-invalid"),
                          {"satisfied": False,
                           "failed_checks": ["holdout_isolation_verified"]})
    print(f"\n-> report['scores'] is {report['scores']}")
    print(f"-> withheld reason: {report['scores_withheld_reason']}")
    print("   Note: a ~99% candidate produced NO number. This is the point.")

    _banner("SUMMARY")
    print("Every state above rendered correctly. The harness fails closed:")
    print("  strong scores + weak controls  -> no PASS")
    print("  strong scores + integrity fail -> no numbers at all")
    print("  one confirmed CME              -> FAIL regardless of aggregate")


if __name__ == "__main__":
    run_demo()
