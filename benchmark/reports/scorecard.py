"""
Scorecard rendering.

The rule from docs/SCORECARD_SPEC.md is enforced here in code, not by
convention: if integrity fails, performance figures are not rendered at all --
not greyed out, not in an appendix, not under a JSON key with a warning flag.

report["scores"] is None on an invalid run, so a downstream consumer reading
report["scores"]["A_medical_qa"] raises rather than silently retrieving a number
from a run whose controls failed.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..gates import CONDITIONAL, FAIL, NOT_APPLICABLE, PASS, UNEVALUABLE, RunOutcome

W = 78

# Outcomes for which no performance figure may be rendered.
SUPPRESSED = {"INVALID_RUN", "INCOMPLETE"}

TRACK_LABELS = {
    "A_medical_qa": "A Medical QA",
    "B_concept_extraction": "B Concept extraction",
    "C_concept_resolution_f1": "C Concept resolution",
    "C_concept_false_merge": "C False merge",
    "D_relationships": "D Relationships",
    "E_generation": "E Generation",
    "F_validation": "F Validation",
    "G_cross_subject": "G Cross-subject",
    "H_fake_mastery_duplicates": "H Near-duplicate",
    "H_fake_mastery_coverage": "H Family coverage",
    "I_robustness": "I Robustness",
    "J_injection": "J Injection",
    "J_injection_tool_violations": "J Tool violations",
    "safety_override_cme": "SAFETY Confirmed CME",
}


def _line(text: str = "") -> str:
    return "║ " + text.ljust(W - 4) + " ║"


def _rule(left="╠", fill="═", right="╣") -> str:
    return left + fill * (W - 2) + right


def render_scorecard(outcome: RunOutcome, meta: dict) -> str:
    out = ["╔" + "═" * (W - 2) + "╗"]
    out.append(_line(f"PG REVISION BENCHMARK {meta.get('benchmark_version', 'v0.4')}"))
    out.append(_line(f"Candidate: {meta.get('candidate_id', 'unknown')[:56]}"))
    out.append(_line(f"Run: {meta.get('run_id', '-')}   Dataset: {meta.get('dataset_hash','-')[:16]}"))
    out.append(_rule())

    if outcome.outcome in SUPPRESSED:
        return _render_suppressed(out, outcome, meta)

    # Integrity line -- a precondition that was satisfied, not a scored row.
    out.append(_line("INTEGRITY PRECONDITIONS " + "." * 26 + " ALL SATISFIED"))
    out.append(_rule())

    ceiling = meta.get("max_attainable_outcome")
    if ceiling and ceiling != PASS:
        out.append(_line(f"OUTCOME CEILING: {ceiling}"))
        out.append(_line(meta.get("ceiling_reason", "")[:W - 6]))
        out.append(_rule())

    out.append(_line(f"{'TRACK':<24}{'METRIC':<14}{'EST':>7}{'95% CI':>17}{'n':>7}{'GATE':>6}"))
    for r in outcome.gate_results:
        label = TRACK_LABELS.get(r.track, r.track)[:23]
        metric = r.metric[:13]
        if r.status == NOT_APPLICABLE:
            out.append(_line(f"{label:<24}{metric:<14}{'—':>7}{'not applicable':>17}"
                             f"{'—':>7}{'N/A':>6}"))
            continue
        if r.status == UNEVALUABLE:
            why = ("n<" + str(r.required_n)) if r.n < r.required_n else "not computable"
            out.append(_line(f"{label:<24}{metric:<14}{'—':>7}"
                             f"{why:>17}{r.n:>7}{'UNEV':>6}"))
            continue
        est = f"{r.estimate:.3f}" if r.estimate is not None else "—"
        ci = f"[{r.ci_lower:.3f},{r.ci_upper:.3f}]" if r.ci_lower is not None else "—"
        tag = {PASS: "PASS", FAIL: "FAIL", CONDITIONAL: "COND"}.get(r.status, r.status[:4])
        out.append(_line(f"{label:<24}{metric:<14}{est:>7}{ci:>17}{r.n:>7}{tag:>6}"))

    if outcome.reliability_results:
        out.append(_rule())
        out.append(_line("RELIABILITY"))
        for k, v in outcome.reliability_results.items():
            shown = "UNEVALUABLE" if v is None else f"{v:.4f}"
            out.append(_line(f"  {k:<44} {shown}"))

    if outcome.reasons:
        out.append(_rule())
        out.append(_line("NOTES"))
        for reason in outcome.reasons[:8]:
            for chunk in _wrap(reason, W - 8):
                out.append(_line("  " + chunk))

    out.append(_rule())
    out.append(_line(f"FINAL: {outcome.outcome}"))
    out.append(_line(""))
    out.append(_line("Scope: engineering qualification of this candidate configuration on"))
    out.append(_line("this frozen corpus. NOT a claim of clinical safety or validity."))
    out.append("╚" + "═" * (W - 2) + "╝")
    return "\n".join(out)


def _render_suppressed(out: list[str], outcome: RunOutcome, meta: dict) -> str:
    out.append(_line(""))
    out.append(_line(outcome.outcome.replace("_", " ").center(W - 4)))
    out.append(_line(""))
    if outcome.outcome == "INVALID_RUN":
        out.append(_line("Failed integrity precondition(s):"))
    else:
        out.append(_line("Run did not complete required coverage:"))
    for reason in outcome.reasons[:6]:
        for chunk in _wrap(reason, W - 8):
            out.append(_line("  " + chunk))
    out.append(_line(""))
    out.append(_line("Performance metrics are WITHHELD. This run produced no"))
    out.append(_line("measurement of candidate capability and does not enter"))
    out.append(_line("rankings or comparisons."))
    out.append(_line(""))
    out.append(_line("Raw outputs are preserved for forensic review and are NOT"))
    out.append(_line("to be scored."))
    out.append("╚" + "═" * (W - 2) + "╝")
    return "\n".join(out)


def _wrap(text: str, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines or [""]


def build_report(outcome: RunOutcome, meta: dict, integrity: dict) -> dict:
    """
    Machine-readable report.

    scores is None -- not {} and not a populated dict with a flag -- whenever
    the run is suppressed.
    """
    suppressed = outcome.outcome in SUPPRESSED
    report = {
        "run_id": meta.get("run_id"),
        "benchmark_version": meta.get("benchmark_version"),
        "candidate_id": meta.get("candidate_id"),
        "candidate_manifest": meta.get("candidate_manifest"),
        "dataset_hash": meta.get("dataset_hash"),
        "gate_registry_hash": meta.get("gate_registry_hash"),
        "outcome": outcome.outcome,
        "rankable": outcome.rankable,
        "integrity": integrity,
        "max_attainable_outcome": meta.get("max_attainable_outcome"),
        "reasons": outcome.reasons,
    }
    if suppressed:
        report["scores"] = None
        report["scores_withheld_reason"] = (
            "integrity_precondition_failure" if outcome.outcome == "INVALID_RUN"
            else "incomplete_coverage")
    else:
        report["scores"] = {r.track: r.as_dict() for r in outcome.gate_results}
        report["reliability"] = outcome.reliability_results
    return report


def write_report(run_dir: str | Path, outcome: RunOutcome, meta: dict, integrity: dict) -> Path:
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    report = build_report(outcome, meta, integrity)
    (run_dir / "report.json").write_text(json.dumps(report, indent=2))
    (run_dir / "report.md").write_text(
        "```\n" + render_scorecard(outcome, meta) + "\n```\n")
    return run_dir / "report.json"
