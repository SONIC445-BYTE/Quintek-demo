"""
The published-evaluation view: the shapes `quintek-eval-api.js` exports and
the student trust screen + admin analytics screens render.

This is the *candidate*-centric counterpart to `runs_api.py`'s run-centric
view. Where that module answers "what happened in run X", this one answers
"what do we currently know about candidate Y" -- overall score, per-track
detail, safety record, cost, latency, and how those changed across benchmark
versions.

Three fields in the frontend contract describe things this repository does not
measure, and each is handled the same way -- **null, never zero**:

  * `costPer1k`   priced from `configs/model_costs.json` if an operator has
                  supplied a price list, otherwise null. A price is a
                  commercial fact about a contract, not something derivable
                  from a benchmark run.
  * `latencyMs`   the median of real recorded executions, or null when the
                  candidate has never been executed through the orchestrator.
  * the `invalid` / `unsafe` / `failedValidation` / `humanReview` breakdown
                  inside `trackDetail[].outcomes` -- the gate engine records
                  a pass count and an n, not a taxonomy of failure modes.

Zero would assert "we looked and found none". Null says "not measured". The
UI already renders the null case (its own fixtures carry a candidate with
every figure null), so reporting honestly costs nothing on the frontend and
misreporting would corrupt the one screen whose whole purpose is disclosure.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

from . import analytics as an


# Student-facing labels for the safety/injection gates that the student track
# grouping folds into broader names -- the admin candidate screen wants them
# broken out, matching the frontend's `trackScores` keys.
ADMIN_TRACK_LABELS: dict[str, str] = {
    "A_medical_qa": "Medical QA",
    "B_concept_extraction": "Concept extraction",
    "C_concept_resolution_f1": "Concept resolution",
    "C_concept_false_merge": "Concept resolution",
    "D_relationships": "Relationships",
    "E_generation": "Question generation",
    "F_validation": "Question validation",
    "G_cross_subject": "Cross-notebook",
    "H_fake_mastery_duplicates": "Knowledge-gap detection",
    "H_fake_mastery_coverage": "Knowledge-gap detection",
    "I_robustness": "Robustness",
    "J_injection": "Prompt-injection resistance",
    "J_tool_violations": "Prompt-injection resistance",
    "safety_override_cme": "Safety",
}


def _pct(x: float | None) -> float | None:
    """0-1 internal scale -> the 0-100 scale the frontend renders."""
    return None if x is None else round(x * 100, 1)


def _label_for(index: int) -> str:
    """Candidate A, B, ... Z, then AA. Stable for a stable candidate order."""
    letters = ""
    n = index
    while True:
        letters = chr(ord("A") + n % 26) + letters
        n = n // 26 - 1
        if n < 0:
            break
    return f"Candidate {letters}"


class EvalAPI:
    def __init__(self, archive: an.RunArchive, *,
                 registry=None,
                 execution_log_path: str | Path | None = None,
                 costs_path: str | Path | None = None,
                 failures: list[an.FailureRecord] | None = None):
        self.archive = archive
        self.registry = registry
        self.execution_log_path = Path(execution_log_path) if execution_log_path else None
        self._failures = failures or []
        self.costs = {}
        if costs_path and Path(costs_path).exists():
            self.costs = json.loads(Path(costs_path).read_text())

    # ---------- telemetry ----------

    def _executions(self):
        if self.execution_log_path is None or not self.execution_log_path.exists():
            return []
        from .orchestration import ExecutionLog

        return ExecutionLog(self.execution_log_path).all()

    def _latency_for(self, candidate_id: str) -> float | None:
        lats = [r.latency_ms for r in self._executions()
                if r.candidate_id == candidate_id and r.latency_ms is not None]
        return round(statistics.median(lats)) if lats else None

    def _cost_per_1k(self, manifest: dict) -> float | None:
        """
        Price per 1k output tokens, read from an operator-supplied price list
        keyed by "provider/model_id". Absent price list or absent entry -> None.
        """
        key = f"{manifest.get('provider')}/{manifest.get('model_id')}"
        entry = self.costs.get(key)
        if entry is None:
            return None
        return entry.get("usd_per_1k_output_tokens")

    # ---------- per-candidate ----------

    def _safety_of(self, result) -> dict:
        """
        Confirmed critical medical errors, straight from the run's safety block.
        A run that never measured safety reports null counts, not zeroes.
        """
        raw = self.archive.raw_report_for(result.run.run_id) if hasattr(
            self.archive, "raw_report_for") else None
        block = (raw or {}).get("safety")
        if not block:
            return {"criticalErrors": None, "criticalRate": None, "criticalN": 0}
        n = block.get("n") or 0
        events = block.get("confirmed_events")
        return {
            "criticalErrors": events,
            "criticalRate": None if events is None or not n else round(events / n * 100, 3),
            "criticalN": n,
        }

    def _eligibility(self, result) -> str:
        if result.safety_status == "fail":
            return "BLOCKED — SAFETY GATE"
        if self.registry is not None:
            entry = self.registry.get(result.run.candidate_id)
            if entry is not None:
                return entry.status
        return "ELIGIBLE" if result.production_eligible else "NOT ELIGIBLE"

    def _track_scores(self, result) -> dict:
        """Admin-facing per-track percentage map, worst-wins where several
        gates share a label, and direction-aware so an error-rate gate is
        never reported as if it were an accuracy."""
        grouped: dict[str, list] = {}
        for t in result.tracks:
            grouped.setdefault(ADMIN_TRACK_LABELS.get(t.track, t.track), []).append(t)
        out = {}
        for label, members in grouped.items():
            vals = [an.normalized_track_score(m) for m in members]
            vals = [v for v in vals if v is not None]
            out[label] = _pct(min(vals)) if vals else None
        return out

    def candidates(self) -> list[dict]:
        rows = []
        ids = sorted(self.archive.candidates())
        for i, cid in enumerate(ids):
            result = self.archive.latest_run_for_candidate(cid)
            if result is None:
                continue
            manifest = result.run.candidate_manifest or {}
            summary = an.candidate_summary(result)
            row = {
                "candidateId": cid,
                "label": _label_for(i),
                "provider": manifest.get("provider"),
                "model": manifest.get("model_id"),
                "version": manifest.get("model_version"),
                "overallScore": _pct(summary.get("overallScore")),
                "status": summary.get("status"),
                "eligibility": self._eligibility(result),
                "lastEvaluated": result.run.timestamp,
                "latencyMs": self._latency_for(cid),
                "costPer1k": self._cost_per_1k(manifest),
                "trackScores": self._track_scores(result),
            }
            row.update(self._safety_of(result))
            rows.append(row)
        return rows

    def overview(self, candidate_id: str) -> dict | None:
        result = self.archive.latest_run_for_candidate(candidate_id)
        if result is None:
            return None
        ov = an.ai_overview(result)
        manifest = result.run.candidate_manifest or {}
        ci = ov.get("confidenceInterval") or [None, None]
        return {
            "overallScore": _pct(ov.get("overallScore")),
            "status": ov.get("status"),
            "benchmarkVersion": ov.get("benchmarkVersion"),
            "evaluatedAt": ov.get("evaluatedAt"),
            "sampleSize": ov.get("sampleSize"),
            "confidenceInterval": [_pct(ci[0]), _pct(ci[1])],
            "currentCandidate": {
                "candidateId": candidate_id,
                "provider": manifest.get("provider"),
                "model": manifest.get("model_id"),
                "version": manifest.get("model_version"),
            },
        }

    def tracks(self, candidate_id: str) -> list[dict] | None:
        result = self.archive.latest_run_for_candidate(candidate_id)
        if result is None:
            return None
        out = []
        for t in an.student_track_results(result):
            ci = t.get("confidenceInterval") or [None, None]
            out.append({
                "track": t.get("track"),
                "score": _pct(t.get("score")),
                "status": (t.get("status") or "").upper(),
                "sampleSize": t.get("sampleSize"),
                "confidenceInterval": [_pct(ci[0]), _pct(ci[1])],
            })
        return out

    def track_detail(self, candidate_id: str) -> dict | None:
        """
        Per-track stored detail. `outcomes` reports only what the gate engine
        actually records -- a pass count, its complement, and n. The frontend's
        other outcome categories (invalid / unsafe / failedValidation /
        humanReview) have no corresponding measurement here and are null rather
        than zero: see this module's docstring.
        """
        result = self.archive.latest_run_for_candidate(candidate_id)
        if result is None:
            return None
        out: dict[str, dict] = {}
        for t in result.tracks:
            label = ADMIN_TRACK_LABELS.get(t.track, t.track)
            norm = an.normalized_track_score(t)
            if norm is None or not t.n:
                correct = incorrect = None
            else:
                correct = round(norm * t.n)
                incorrect = t.n - correct
            ci = [t.ci_lower, t.ci_upper]
            entry = {
                "score": _pct(norm),
                "n": t.n,
                "ci": [_pct(ci[0]), _pct(ci[1])],
                "outcomes": {
                    "correct": correct,
                    "incorrect": incorrect,
                    "invalid": None,
                    "unsafe": None,
                    "failedValidation": None,
                    "humanReview": None,
                    "total": t.n,
                },
                "measured": norm is not None,
            }
            # Worst-wins when several gates share one label.
            prior = out.get(label)
            if prior is None or (entry["score"] is not None and prior["score"] is not None
                                 and entry["score"] < prior["score"]):
                out[label] = entry
        return out

    def overall_by_candidate(self) -> dict:
        out = {}
        for cid in sorted(self.archive.candidates()):
            result = self.archive.latest_run_for_candidate(cid)
            if result is None:
                out[cid] = None
                continue
            ov = an.ai_overview(result)
            ci = ov.get("confidenceInterval") or [None, None]
            score = _pct(ov.get("overallScore"))
            out[cid] = None if score is None else {
                "score": score,
                "ci": [_pct(ci[0]), _pct(ci[1])],
                "n": ov.get("sampleSize"),
            }
        return out

    # ---------- cross-run ----------

    def history(self) -> list[dict]:
        """
        Score per candidate per benchmark version, oldest first.

        `comparable` is False for the earliest version present because there is
        nothing before it to compare against, and a version boundary is exactly
        where scores stop being comparable -- a changed corpus or changed
        threshold set makes two numbers different measurements, not progress.
        """
        by_version: dict[str, dict] = {}
        for result in self.archive.all_runs():
            v = result.run.benchmark_version
            slot = by_version.setdefault(v, {"version": v, "date": result.run.timestamp,
                                             "scores": {}, "criticalRate": {}})
            score = _pct(an.ai_overview(result).get("overallScore"))
            if score is not None:
                slot["scores"][result.run.candidate_id] = score
            safety = self._safety_of(result)
            if safety["criticalRate"] is not None:
                slot["criticalRate"][result.run.candidate_id] = safety["criticalRate"]
            if result.run.timestamp and result.run.timestamp < (slot["date"] or "~"):
                slot["date"] = result.run.timestamp
        ordered = sorted(by_version.values(), key=lambda s: s["date"] or "")
        for i, slot in enumerate(ordered):
            slot["comparable"] = i > 0
        return ordered

    def runs(self) -> list[dict]:
        """
        The run-history rows the admin console renders.

        `cases` is the largest sample size the run actually reached, which is
        a real figure. `calls`, `cost` and `duration` are null: per-run
        provider-call counts and wall-clock are not recorded in report.json,
        and spend additionally needs a price list. The console renders null as
        "n/a" -- a zero here would claim a run made no model calls and cost
        nothing, which is a measurement, not an absence of one.
        """
        rows = []
        for result in self.archive.all_runs():
            rows.append({
                "runId": result.run.run_id,
                "date": result.run.timestamp,
                "version": result.run.benchmark_version,
                "candidates": 1,
                "cases": max((t.n for t in result.tracks), default=0),
                "calls": None,
                "cost": None,
                "duration": None,
                "status": result.run.outcome,
                "scoresWithheld": result.run.scores_withheld,
            })
        rows.sort(key=lambda r: r.get("date") or "", reverse=True)
        return rows

    def failures(self) -> list[dict]:
        if not self._failures:
            return []
        res = an.failure_analytics(self._failures)
        return [c if isinstance(c, dict) else c.as_dict() for c in res.categories]

    def state(self) -> str:
        runs = self.archive.all_runs()
        if not runs:
            return "empty"
        if all(r.run.scores_withheld for r in runs):
            return "incomplete"
        return "ok"

    def bundle(self, candidate_id: str | None = None) -> dict:
        """
        Everything `quintek-eval-api.js` exports, in one response, so the
        module can fetch once and re-export rather than issuing a dozen
        round-trips to paint one screen.
        """
        cands = self.candidates()
        current = candidate_id or (cands[0]["candidateId"] if cands else None)
        return {
            "state": self.state(),
            "overview": self.overview(current) if current else None,
            "tracks": self.tracks(current) if current else [],
            "candidates": cands,
            "history": self.history(),
            "failures": self.failures(),
            "cases": [],
            "runs": self.runs(),
            "trackDetail": {c["candidateId"]: self.track_detail(c["candidateId"]) or {}
                            for c in cands},
            "overallByCandidate": self.overall_by_candidate(),
        }
