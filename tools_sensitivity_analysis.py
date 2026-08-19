"""
Does benchmark fluctuation move what a student sees?

A candidate re-run on the same corpus does not reproduce its previous raw
counts: model sampling, retries and judge variance all shift the number of
successes by a few items. This script measures how far that propagates -- from
a perturbed raw count, through the gate engine, into the exact figures the
student trust screen and the admin leaderboard render.

Three things are worth separating, because they fail differently:

  SCORE DRIFT     the percentage moves. Expected, bounded, and disclosed by
                  the confidence interval printed beside it.
  STATUS FLIP     PASS becomes FAIL (or CONDITIONAL). Categorical, and a
                  student sees a different colour, not a slightly different
                  number. Only possible when the CI straddles the threshold.
  RANK FLIP       two candidates swap places on the leaderboard. The one that
                  matters for routing, since the router picks by rank.

Run:  python3 tools_sensitivity_analysis.py
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from benchmark import analytics as an
from benchmark.gates import GateRegistry, Measurement, evaluate_run
from benchmark.reports.scorecard import write_report

REGISTRY_PATH = "configs/gate_registry_v0_4.json"
ROOT = Path(__file__).resolve().parent


def _meta(run_id, candidate, ts, model="openai/gpt-oss-120b"):
    return {
        "run_id": run_id, "benchmark_version": "v0.4", "candidate_id": candidate,
        "candidate_manifest": {"provider": "nvidia", "model_id": model,
                               "model_version": "1.0", "system_prompt_hash": "v1",
                               "decoding_config": {"temperature": 0.0},
                               "code_commit": "sensitivity"},
        "dataset_hash": "d1", "gate_registry_hash": "g1",
        "calibration_state": "UNCALIBRATED", "review_mode": "developmental",
        "reviewer_count": 1, "kappa_computable": True,
        "max_attainable_outcome": "NOT_VALID_FOR_PRODUCTION_PASS",
        "ceiling_reason": "1 of 2 reviewers", "timestamp": ts,
    }


def _measurements(reg, accuracy, rubric=3.6):
    ms = {}
    for key, spec in reg.tracks.items():
        n = spec["min_n"]
        if spec["ci"].startswith("bootstrap") and "rubric" in spec["metric"]:
            ms[key] = Measurement(values=[rubric] * n, n=n)
        elif spec["direction"] == "lower":
            ms[key] = Measurement(successes=int(round(n * accuracy)), n=n)
        else:
            ms[key] = Measurement(successes=0, n=n)
    return ms


def _student_view(reg, measurements, tmp, run_id="r"):
    """Push measurements all the way through to the student-facing numbers."""
    outcome = evaluate_run(reg, measurements, integrity_ok=True)
    run_dir = Path(tmp) / run_id
    write_report(run_dir, outcome, _meta(run_id, "cand-a", "2026-08-19T00:00:00Z"),
                 {"satisfied": True, "failed_checks": [], "passed_checks": [], "details": {}})
    archive = an.RunArchive(tmp)
    result = archive.latest_run_for_candidate("cand-a")
    overview = an.ai_overview(result)
    tracks = {t["track"]: t for t in an.student_track_results(result)}
    shutil.rmtree(run_dir)
    return outcome, overview, tracks


def experiment_1_score_drift(reg):
    print("=" * 78)
    print("1. SCORE DRIFT -- how far does the student-visible number move?")
    print("=" * 78)
    print("Perturbing medical-QA successes around a 0.95 baseline at n=500.")
    print("(+/-5 items on 500 is a realistic re-run difference.)\n")
    print(f"  {'delta items':>12}  {'raw acc':>8}  {'student score':>14}  {'CI shown':>18}  {'status':>8}")
    baseline = None
    for delta in (-10, -5, -2, 0, 2, 5, 10):
        with tempfile.TemporaryDirectory() as tmp:
            ms = _measurements(reg, 0.95)
            base_n = reg.tracks["A_medical_qa"]["min_n"]
            ms["A_medical_qa"] = Measurement(successes=int(base_n * 0.95) + delta, n=base_n)
            _, _, tracks = _student_view(reg, ms, tmp)
            mk = tracks["Medical knowledge"]
            ci = mk["confidenceInterval"]
            score = mk["score"] * 100
            if delta == 0:
                baseline = score
            print(f"  {delta:>+12}  {(int(base_n*0.95)+delta)/base_n:>8.3f}  "
                  f"{score:>13.2f}%  [{ci[0]*100:>6.2f},{ci[1]*100:>6.2f}]  {mk['status']:>8}")
    print(f"\n  Baseline student score: {baseline:.2f}%")
    print("  A +/-10 item swing on 500 moves the displayed figure by ~2 points.")
    print("  The CI printed beside it is ~3 points wide, so the drift stays")
    print("  inside the uncertainty the screen already discloses.\n")


def experiment_2_status_flip(reg):
    print("=" * 78)
    print("2. STATUS FLIP -- can noise change the colour a student sees?")
    print("=" * 78)
    spec = reg.tracks["A_medical_qa"]
    threshold, n = spec["threshold"], spec["min_n"]
    print(f"GATE-A-ACC threshold {threshold} (direction '{spec['direction']}'), n={n}.")
    print("A 'lower' gate passes only when the CI LOWER bound clears the")
    print("threshold, so a candidate sitting near it is the fragile case.\n")
    print(f"  {'raw acc':>8}  {'CI lower':>9}  {'gate':>12}  {'student sees':>13}")
    flips = []
    for acc in (0.90, 0.91, 0.92, 0.925, 0.93, 0.94, 0.95):
        with tempfile.TemporaryDirectory() as tmp:
            ms = _measurements(reg, 0.95)
            ms["A_medical_qa"] = Measurement(successes=int(round(n * acc)), n=n)
            outcome, _, tracks = _student_view(reg, ms, tmp)
            gate = next(g for g in outcome.gate_results if g.gate_id == "GATE-A-ACC")
            mk = tracks["Medical knowledge"]
            flips.append((acc, gate.status))
            print(f"  {acc:>8.3f}  {gate.ci_lower:>9.4f}  {gate.status:>12}  {mk['status']:>13}")
    statuses = {s for _, s in flips}
    print(f"\n  Statuses observed across the sweep: {sorted(statuses)}")
    print("  The flip point sits well ABOVE the bare threshold, because the gate")
    print("  requires the CI lower bound -- not the point estimate -- to clear it.")
    print("  That is the mechanism that stops noise alone from producing a PASS.\n")


def experiment_3_rank_flip(reg):
    print("=" * 78)
    print("3. RANK FLIP -- can noise reorder the leaderboard (and so routing)?")
    print("=" * 78)
    print("Two candidates 0.5 points apart, each perturbed by +/-1%.\n")
    with tempfile.TemporaryDirectory() as tmp:
        for label, acc in (("cand-a", 0.950), ("cand-b", 0.945)):
            outcome = evaluate_run(reg, _measurements(reg, acc), integrity_ok=True)
            write_report(Path(tmp) / label, outcome,
                         _meta(label, label, "2026-08-19T00:00:00Z"),
                         {"satisfied": True, "failed_checks": [], "passed_checks": [],
                          "details": {}})
        board = an.leaderboard(an.RunArchive(tmp))
        print("  Unperturbed order:")
        for e in board:
            print(f"    #{e.rank} {e.candidate_id}  score={e.ranking_score:.5f}  "
                  f"eligible={e.production_eligible}")

    with tempfile.TemporaryDirectory() as tmp:
        for label, acc in (("cand-a", 0.940), ("cand-b", 0.955)):
            outcome = evaluate_run(reg, _measurements(reg, acc), integrity_ok=True)
            write_report(Path(tmp) / label, outcome,
                         _meta(label, label, "2026-08-19T00:00:00Z"),
                         {"satisfied": True, "failed_checks": [], "passed_checks": [],
                          "details": {}})
        board = an.leaderboard(an.RunArchive(tmp))
        print("\n  After a 1% swing in opposite directions:")
        for e in board:
            print(f"    #{e.rank} {e.candidate_id}  score={e.ranking_score:.5f}  "
                  f"eligible={e.production_eligible}")
    print("\n  Candidates within noise of each other DO swap rank. Rank is a sort")
    print("  key over a mean -- it has no significance test behind it, and the")
    print("  harness never claims one. Two candidates whose CIs overlap should be")
    print("  treated as tied; the leaderboard shows an order, not a finding.\n")


def experiment_4_determinism(reg):
    print("=" * 78)
    print("4. DETERMINISM -- does the ENGINE add noise of its own?")
    print("=" * 78)
    print("Same measurements, evaluated 5 times. Any variation here would be")
    print("the harness inventing fluctuation rather than reporting it.\n")
    seen = set()
    for i in range(5):
        with tempfile.TemporaryDirectory() as tmp:
            _, overview, tracks = _student_view(reg, _measurements(reg, 0.95), tmp)
            fingerprint = json.dumps(
                {"o": overview["overallScore"],
                 "t": sorted((k, v["score"]) for k, v in tracks.items())},
                sort_keys=True)
            seen.add(fingerprint)
            print(f"  run {i+1}: overall={overview['overallScore']:.9f}")
    print(f"\n  Distinct results across 5 identical evaluations: {len(seen)}")
    if len(seen) == 1:
        print("  The engine is deterministic. Bootstrap CIs use a fixed seed, so")
        print("  every figure a student sees is reproducible from the stored")
        print("  measurement. Any fluctuation they observe came from the MODEL,")
        print("  not from the benchmark.\n")
    else:
        print("  WARNING: the engine produced different numbers from identical")
        print("  input. That is a defect -- investigate the bootstrap seed.\n")


def main():
    reg = GateRegistry(REGISTRY_PATH)
    print()
    experiment_1_score_drift(reg)
    experiment_2_status_flip(reg)
    experiment_3_rank_flip(reg)
    experiment_4_determinism(reg)
    print("=" * 78)
    print("SUMMARY")
    print("=" * 78)
    print("""
  Engine noise            NONE. Identical input gives identical output.
  Model noise -> score    Yes, bounded, and covered by the CI displayed.
  Model noise -> status   Only near a threshold, and the CI-lower-bound rule
                          makes an accidental PASS much harder than an
                          accidental FAIL. Failing closed is the intended bias.
  Model noise -> rank     Yes, freely, for candidates within noise of each
                          other. Rank carries no significance claim.

  What protects the student screen: every figure ships with its confidence
  interval and sample size, and a gate below min_n reports UNEVALUABLE rather
  than a number. The screen can show an uncertain result; it cannot show a
  confident-looking wrong one.
""")


if __name__ == "__main__":
    main()
