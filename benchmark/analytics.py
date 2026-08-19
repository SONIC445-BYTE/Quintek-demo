"""
Benchmark analytics data layer.

This module exists because a single `report.json` per run is not enough to
answer the questions an admin dashboard needs to ask: which candidate ranks
where, how does a track compare across two candidates, what does the failure
pattern look like across runs, and why did production route a given task to
a given model. Those are aggregation and query concerns over MANY runs, not
scoring concerns -- this module never recomputes a gate, never invents a
threshold, and never derives a number `benchmark.gates` didn't already
produce. It reads what the harness already wrote to `runs/` and reshapes it.

Two rules carried over from the rest of this harness, because they are load-
bearing here too:

  1. RANKING IS NOT ELIGIBILITY. A leaderboard needs a sortable number, but
     `docs/SCORECARD_SPEC.md` prohibits an aggregate score that could avg a
     failed mandatory gate away *on the scorecard*. The resolution: a
     `ranking_score` exists here ONLY as a leaderboard sort key, is never
     written into a run's own report.json, and never participates in
     `production_eligible` -- that stays exactly `outcome in {PASS,
     CONDITIONAL}` with `rankable=True`, same as the harness's own
     definition. `test_ranking_never_implies_eligibility` in
     tests/test_analytics.py exists specifically to catch a future edit that
     conflates the two.

  2. HISTORY IS IMMUTABLE. Every function here is read-only over `runs/`.
     Nothing in this module writes into an existing run directory or
     overwrites a report.json. New information (a routing decision, a
     human-review record) is appended to its own log, never merged into a
     prior run's record.

Student-facing status vocabulary: the frontend contract asks for
PASS/REVIEW/FAIL (plus "unavailable" for anything that must render no
number at all). That vocabulary does not exist in
`configs/gate_registry_v0_4.json` -- the registry's outcome_states are
PASS/CONDITIONAL/FAIL/NO_PASS_CAPABILITY_GAP/INCOMPLETE/INVALID_RUN/
NOT_VALID_FOR_PRODUCTION_PASS/UNEVALUABLE. `STUDENT_STATUS_MAP` below is the
one place that translation happens, and it is a presentation-layer relabeling
of already-computed categorical states, not a new gate or threshold --
nothing here decides PASS vs FAIL, it only renames a decision already made.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Student-facing status vocabulary (presentation layer only -- see module
# docstring). Every registry outcome_state and gate status must appear here;
# test_analytics.py asserts the mapping stays exhaustive as the registry
# changes.
# ---------------------------------------------------------------------------

RUN_STATUS_MAP: dict[str, str] = {
    "PASS": "pass",
    "CONDITIONAL": "review",
    "NOT_VALID_FOR_PRODUCTION_PASS": "review",
    "FAIL": "fail",
    "NO_PASS_CAPABILITY_GAP": "fail",
    "UNEVALUABLE": "unavailable",
    "INVALID_RUN": "unavailable",
    "INCOMPLETE": "unavailable",
}

GATE_STATUS_MAP: dict[str, str] = {
    "PASS": "pass",
    "CONDITIONAL": "review",
    "FAIL": "fail",
    "UNEVALUABLE": "unavailable",
    "NOT_APPLICABLE": "unavailable",
}

ELIGIBLE_OUTCOMES = {"PASS", "CONDITIONAL"}


def student_status(outcome: str) -> str:
    return RUN_STATUS_MAP.get(outcome, "unavailable")


def student_gate_status(status: str) -> str:
    return GATE_STATUS_MAP.get(status, "unavailable")


# ---------------------------------------------------------------------------
# 33. Required analytics entities
# ---------------------------------------------------------------------------

@dataclass
class TrackResult:
    """One gate's result within one run. Wraps `benchmark.gates.GateResult`
    plus the presentation-layer status and the per-item breakdown from
    section 34, where available."""

    track: str
    gate_id: str
    metric: str
    status: str                     # registry vocabulary: PASS/FAIL/...
    student_status: str             # pass/review/fail/unavailable
    score: float | None
    n: int
    required_n: int
    ci_lower: float | None
    ci_upper: float | None
    mandatory: bool
    direction: str = "lower"
    # Top of this metric's scale (see gates.GateResult.scale_max). Anything
    # that averages or percentages a score MUST divide by this first.
    scale_max: float = 1.0
    n_unit: str = ""
    correct: int | None = None
    incorrect: int | None = None
    invalid: int | None = None
    failed: int | None = None
    critical_error_count: int = 0
    critical_error_rate: float | None = None
    latency_ms_mean: float | None = None
    token_usage: dict | None = None
    cost_usd: float | None = None

    @classmethod
    def from_gate_result(cls, gr: dict) -> "TrackResult":
        return cls(
            track=gr["track"], gate_id=gr["gate_id"], metric=gr["metric"],
            status=gr["status"], student_status=student_gate_status(gr["status"]),
            score=gr.get("estimate"), n=gr.get("n", 0), required_n=gr.get("required_n", 0),
            ci_lower=gr.get("ci_lower"), ci_upper=gr.get("ci_upper"),
            mandatory=gr.get("mandatory", True), direction=gr.get("direction", "lower"),
            scale_max=float(gr.get("scale_max") or 1.0), n_unit=gr.get("n_unit", ""),
        )

    def as_dict(self) -> dict:
        return dict(
            track=self.track, gate_id=self.gate_id, metric=self.metric,
            status=self.status, student_status=self.student_status,
            score=self.score, n=self.n, required_n=self.required_n,
            ci_lower=self.ci_lower, ci_upper=self.ci_upper, mandatory=self.mandatory,
            direction=self.direction, scale_max=self.scale_max,
            n_unit=self.n_unit, correct=self.correct, incorrect=self.incorrect,
            invalid=self.invalid, failed=self.failed,
            critical_error_count=self.critical_error_count,
            critical_error_rate=self.critical_error_rate,
            latency_ms_mean=self.latency_ms_mean, token_usage=self.token_usage,
            cost_usd=self.cost_usd,
        )


@dataclass
class BenchmarkRun:
    """One run of the harness, as recorded in runs/<run_id>/."""

    run_id: str
    benchmark_version: str
    candidate_id: str
    candidate_manifest: dict
    dataset_hash: str
    gate_registry_hash: str
    outcome: str
    rankable: bool
    timestamp: str
    integrity_satisfied: bool
    max_attainable_outcome: str | None
    scores_withheld: bool

    def as_dict(self) -> dict:
        return dict(
            run_id=self.run_id, benchmark_version=self.benchmark_version,
            candidate_id=self.candidate_id, candidate_manifest=self.candidate_manifest,
            dataset_hash=self.dataset_hash, gate_registry_hash=self.gate_registry_hash,
            outcome=self.outcome, rankable=self.rankable, timestamp=self.timestamp,
            integrity_satisfied=self.integrity_satisfied,
            max_attainable_outcome=self.max_attainable_outcome,
            scores_withheld=self.scores_withheld,
        )


@dataclass
class CandidateBenchmarkResult:
    """One candidate's full result within one run: the run plus every track."""

    run: BenchmarkRun
    tracks: list[TrackResult] = field(default_factory=list)

    @property
    def student_status(self) -> str:
        return student_status(self.run.outcome)

    @property
    def production_eligible(self) -> bool:
        """Exactly the harness's own definition -- see module docstring
        rule 1. This is NOT derived from ranking_score."""
        return self.run.outcome in ELIGIBLE_OUTCOMES and self.run.rankable

    @property
    def safety_status(self) -> str | None:
        for t in self.tracks:
            if t.gate_id == "GATE-SAFETY-CME":
                return t.status
        return None

    def as_dict(self) -> dict:
        d = self.run.as_dict()
        d["tracks"] = [t.as_dict() for t in self.tracks]
        d["student_status"] = self.student_status
        d["production_eligible"] = self.production_eligible
        d["safety_status"] = self.safety_status
        return d


@dataclass
class BenchmarkCaseResult:
    """One scored item within one track within one run (section 34's most
    granular unit, for drill-down). `outcome` is one of correct/incorrect/
    invalid/failed -- see the module-level classifier below."""

    run_id: str
    track: str
    item_id: str
    outcome: str                    # correct | incorrect | invalid | failed
    predicted: Any = None
    expected: Any = None
    error: str | None = None
    latency_ms: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None

    def as_dict(self) -> dict:
        return dict(
            run_id=self.run_id, track=self.track, item_id=self.item_id,
            outcome=self.outcome, predicted=self.predicted, expected=self.expected,
            error=self.error, latency_ms=self.latency_ms,
            input_tokens=self.input_tokens, output_tokens=self.output_tokens,
        )


@dataclass
class FailureRecord:
    """A non-safety scoring failure, for the failure-analytics API."""

    run_id: str
    candidate_id: str
    track: str
    item_id: str
    category: str
    severity: str = "medium"
    detail: str = ""

    def as_dict(self) -> dict:
        return dict(
            run_id=self.run_id, candidate_id=self.candidate_id, track=self.track,
            item_id=self.item_id, category=self.category, severity=self.severity,
            detail=self.detail,
        )


@dataclass
class CriticalErrorRecord:
    """A confirmed CME gate event -- mirrors the four-clause conjunction in
    docs/SEVERITY_TAXONOMY.md. Built from adjudicated records, never
    constructed with gate_event=True by hand (same rule as
    scorers.deterministic.score_critical_medical_errors)."""

    run_id: str
    candidate_id: str
    item_id: str
    item_severity: str
    cme_category: str
    harm_tier: str
    senior_adjudicator: str
    rationale: str = ""

    def as_dict(self) -> dict:
        return dict(
            run_id=self.run_id, candidate_id=self.candidate_id, item_id=self.item_id,
            item_severity=self.item_severity, cme_category=self.cme_category,
            harm_tier=self.harm_tier, senior_adjudicator=self.senior_adjudicator,
            rationale=self.rationale,
        )


# GateResult is deliberately not redefined here -- benchmark.gates.GateResult
# is the entity; TrackResult above is its analytics-layer wrapper.


@dataclass
class RoutingDecision:
    """One production task-routing event (section 39). Persisted append-only
    via RoutingLog below -- see module docstring rule 2."""

    execution_id: str
    task: str
    selected_candidate: str
    eligible_candidates: list[str]
    routing_policy: str
    benchmark_evidence: dict          # e.g. {"run_id": ..., "track": ..., "score": ...}
    timestamp: str
    fallback: bool = False
    fallback_reason: str | None = None

    def as_dict(self) -> dict:
        return dict(
            execution_id=self.execution_id, task=self.task,
            selected_candidate=self.selected_candidate,
            eligible_candidates=self.eligible_candidates,
            routing_policy=self.routing_policy, benchmark_evidence=self.benchmark_evidence,
            timestamp=self.timestamp, fallback=self.fallback,
            fallback_reason=self.fallback_reason,
        )


@dataclass
class HumanReviewResult:
    """Analytics-layer view of an adjudication.queue.AdjudicationRecord."""

    run_id: str
    item_id: str
    rater_labels: dict
    disagreement: bool
    status: str
    final_label: Any = None
    senior_adjudicator: str | None = None

    @classmethod
    def from_adjudication_record(cls, run_id: str, rec) -> "HumanReviewResult":
        return cls(
            run_id=run_id, item_id=rec.item_id, rater_labels=rec.rater_labels,
            disagreement=rec.disagreement, status=rec.status,
            final_label=rec.final_label, senior_adjudicator=rec.senior_adjudicator,
        )

    def as_dict(self) -> dict:
        return dict(
            run_id=self.run_id, item_id=self.item_id, rater_labels=self.rater_labels,
            disagreement=self.disagreement, status=self.status,
            final_label=self.final_label, senior_adjudicator=self.senior_adjudicator,
        )


def classify_case(response: dict, is_correct: bool | None) -> str:
    """
    Section 34's correct/incorrect/invalid/failed split, as a single shared
    rule so every track classifies cases the same way instead of each caller
    inventing its own definition.

      failed   -- the provider call itself did not succeed (network/timeout
                  after retries). Not a knowledge failure -- see
                  scorers/deterministic.py's score_medical_qa docstring.
      invalid  -- the call succeeded but produced no parseable answer.
      correct / incorrect -- a parseable answer was scored against gold.
    """
    if not response.get("ok", response.get("error") is None):
        return "failed"
    if response.get("parsed") is None:
        return "invalid"
    if is_correct is None:
        raise ValueError("is_correct must be supplied for a parsed, ok response")
    return "correct" if is_correct else "incorrect"


# ---------------------------------------------------------------------------
# 35. Historical data -- read-only archive over runs/
# ---------------------------------------------------------------------------

class RunArchive:
    """
    Loads every run under `root` without ever writing to it. Supports the
    candidate -> runs -> versions -> tracks traversal section 35 requires
    for historical charting.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _run_dirs(self):
        if not self.root.exists():
            return
        for d in sorted(self.root.iterdir()):
            if d.is_dir() and (d / "report.json").exists():
                yield d

    def load_run(self, run_dir: Path) -> CandidateBenchmarkResult | None:
        report = json.loads((run_dir / "report.json").read_text())
        manifest_path = run_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}

        run = BenchmarkRun(
            run_id=report.get("run_id") or run_dir.name,
            benchmark_version=report.get("benchmark_version", "unknown"),
            candidate_id=report.get("candidate_id", "unknown"),
            candidate_manifest=report.get("candidate_manifest") or {},
            dataset_hash=report.get("dataset_hash", ""),
            gate_registry_hash=report.get("gate_registry_hash", ""),
            outcome=report.get("outcome", "UNEVALUABLE"),
            rankable=bool(report.get("rankable", False)),
            # report.json now carries its own timestamp; manifest.json remains
            # the fallback for runs written before that field existed.
            timestamp=report.get("timestamp") or manifest.get("timestamp", ""),
            integrity_satisfied=bool((report.get("integrity") or {}).get("satisfied", False)),
            max_attainable_outcome=report.get("max_attainable_outcome")
                or manifest.get("max_attainable_outcome"),
            scores_withheld=report.get("scores") is None,
        )
        tracks = []
        if report.get("scores"):
            for gr in report["scores"].values():
                tracks.append(TrackResult.from_gate_result(gr))
        return CandidateBenchmarkResult(run=run, tracks=tracks)

    def raw_report_for(self, run_id: str) -> dict | None:
        """
        The unparsed report.json for one run.

        `load_run` projects a report into dataclasses shaped for scoring and
        ranking, which deliberately drops blocks nothing in that path consumes
        -- the safety summary among them. A caller that needs a field the
        projection omits reads it here rather than widening the dataclasses to
        carry everything any consumer might one day want.
        """
        for d in self._run_dirs():
            try:
                report = json.loads((d / "report.json").read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if (report.get("run_id") or d.name) == run_id:
                return report
        return None

    def all_runs(self) -> list[CandidateBenchmarkResult]:
        out = []
        for d in self._run_dirs():
            try:
                r = self.load_run(d)
            except (json.JSONDecodeError, KeyError):
                continue
            if r is not None:
                out.append(r)
        return out

    def runs_for_candidate(self, candidate_id: str) -> list[CandidateBenchmarkResult]:
        return sorted(
            (r for r in self.all_runs() if r.run.candidate_id == candidate_id),
            key=lambda r: r.run.timestamp,
        )

    def latest_run_for_candidate(self, candidate_id: str) -> CandidateBenchmarkResult | None:
        runs = self.runs_for_candidate(candidate_id)
        return runs[-1] if runs else None

    def candidates(self) -> list[str]:
        return sorted({r.run.candidate_id for r in self.all_runs()})


# ---------------------------------------------------------------------------
# 36. Leaderboard -- ranking separate from eligibility
# ---------------------------------------------------------------------------

@dataclass
class LeaderboardEntry:
    candidate_id: str
    rank: int
    ranking_score: float | None       # sort key ONLY -- see module docstring rule 1
    student_status: str
    production_eligible: bool
    safety_status: str | None
    latest_run_id: str
    track_count_measured: int

    def as_dict(self) -> dict:
        return dict(
            candidate_id=self.candidate_id, rank=self.rank,
            ranking_score=self.ranking_score, student_status=self.student_status,
            production_eligible=self.production_eligible, safety_status=self.safety_status,
            latest_run_id=self.latest_run_id, track_count_measured=self.track_count_measured,
        )


def normalized_track_score(track: TrackResult) -> float | None:
    """
    Puts a gate estimate on a 'higher is better' scale by inverting
    lower-is-better (`upper`/`equal` direction) gates: `1 - estimate`.

    This is a DIFFERENT fix from `_ranking_score`'s: that function excludes
    error-rate gates from its average entirely, because the official
    ranking number should never blend incompatible scales. This function
    exists for callers that need SOME signal from every gate relevant to a
    task (benchmark/router.py's task scoring, `task_leaderboard` below) --
    they invert rather than drop, producing an approximate, direction-
    consistent tie-breaker, not a scored result. Never use this for a
    number that could be read as an official score.
    """
    if track.score is None:
        return None
    scaled = track.score / (track.scale_max or 1.0)
    return scaled if track.direction == "lower" else 1.0 - scaled


def task_leaderboard(archive: "RunArchive", gate_ids: list[str]) -> list[dict]:
    """
    Ranks every candidate's latest run by normalized mean score over the
    given gate_ids, independent of Model Registry status -- this reflects
    benchmark evidence, not production eligibility. For eligibility-aware
    production selection, use `benchmark.router.Router`, which additionally
    excludes any candidate whose latest run is not `production_eligible`.
    """
    rows = []
    for cid in archive.candidates():
        result = archive.latest_run_for_candidate(cid)
        if result is None:
            continue
        relevant = [t for t in result.tracks if t.gate_id in gate_ids]
        normalized = [normalized_track_score(t) for t in relevant]
        normalized = [n for n in normalized if n is not None]
        score = (sum(normalized) / len(normalized)) if normalized else None
        manifest = result.run.candidate_manifest or {}
        rows.append({
            "candidate_id": cid, "provider": manifest.get("provider"),
            "model": manifest.get("model_id"), "score": score,
            "status": result.student_status,
            "production_eligible": result.production_eligible,
        })
    rows.sort(key=lambda r: (r["score"] is None, -(r["score"] or 0.0)))
    for i, r in enumerate(rows, start=1):
        r["rank"] = i
    return rows


def _ranking_score(result: CandidateBenchmarkResult) -> float | None:
    """
    Mean of measured track estimates. Sort key only -- never written back
    into a run's report.json, never consulted by production_eligible.

    Only `direction == "lower"` tracks are averaged (accuracy, F1, retention,
    rubric mean/4 -- gates where a HIGHER raw estimate is better). `upper`/
    `equal`-direction gates (false-merge rate, injection attack-success rate,
    confirmed CME rate) are error rates where a LOWER raw estimate is
    better; averaging a 0.99 accuracy with a 0.02 CME rate produces 0.505,
    which silently punishes the safer candidate for reporting a small
    number. That is the exact "aggregate that averages a failure away"
    failure mode docs/SCORECARD_SPEC.md prohibits on the real scorecard, and
    it would have been just as wrong here even though this is only a sort
    key -- test_ranking_never_implies_eligibility exists to catch it.

    Scale matters as much as direction. `E_generation` is a mean rubric
    rating on a 0-4 scale, not a rate: averaging its raw 3.6 with a 0.97
    accuracy produced an "overall score" of 1.35 -- above 1.0, and therefore
    obviously wrong the moment anyone rendered it as a percentage. Each
    estimate is divided by its registered `scale_max` first, so every term in
    this mean is a 0-1 fraction of its own scale.
    """
    scored = [t.score / (t.scale_max or 1.0) for t in result.tracks
             if t.score is not None and t.direction == "lower"]
    return sum(scored) / len(scored) if scored else None


def leaderboard(archive: RunArchive) -> list[LeaderboardEntry]:
    latest = [archive.latest_run_for_candidate(c) for c in archive.candidates()]
    latest = [r for r in latest if r is not None]

    def sort_key(r: CandidateBenchmarkResult):
        score = _ranking_score(r)
        return (score is None, -(score or 0.0))

    latest.sort(key=sort_key)
    entries = []
    for i, r in enumerate(latest, start=1):
        entries.append(LeaderboardEntry(
            candidate_id=r.run.candidate_id, rank=i, ranking_score=_ranking_score(r),
            student_status=r.student_status, production_eligible=r.production_eligible,
            safety_status=r.safety_status, latest_run_id=r.run.run_id,
            track_count_measured=len([t for t in r.tracks if t.score is not None]),
        ))
    return entries


# ---------------------------------------------------------------------------
# 37. Comparison API
# ---------------------------------------------------------------------------

@dataclass
class ComparisonResult:
    candidate_a: CandidateBenchmarkResult
    candidate_b: CandidateBenchmarkResult

    def as_dict(self) -> dict:
        by_track_a = {t.track: t for t in self.candidate_a.tracks}
        by_track_b = {t.track: t for t in self.candidate_b.tracks}
        tracks = []
        for key in sorted(set(by_track_a) | set(by_track_b)):
            tracks.append({
                "track": key,
                "a": by_track_a[key].as_dict() if key in by_track_a else None,
                "b": by_track_b[key].as_dict() if key in by_track_b else None,
            })
        return dict(
            candidate_a=self.candidate_a.as_dict(),
            candidate_b=self.candidate_b.as_dict(),
            tracks=tracks,
        )


def compare(archive: RunArchive, candidate_a: str, candidate_b: str) -> ComparisonResult:
    ra = archive.latest_run_for_candidate(candidate_a)
    rb = archive.latest_run_for_candidate(candidate_b)
    if ra is None or rb is None:
        missing = candidate_a if ra is None else candidate_b
        raise KeyError(f"no run found for candidate '{missing}'")
    return ComparisonResult(candidate_a=ra, candidate_b=rb)


# ---------------------------------------------------------------------------
# 38. Failure analytics API
# ---------------------------------------------------------------------------

@dataclass
class FailureAnalyticsResult:
    count: int
    rate: float | None
    cases: list[dict]


def failure_analytics(
    failures: list[FailureRecord],
    total_n: int | None = None,
    *,
    candidate_id: str | None = None,
    track: str | None = None,
    severity: str | None = None,
    category: str | None = None,
    run_id: str | None = None,
) -> FailureAnalyticsResult:
    def matches(f: FailureRecord) -> bool:
        if candidate_id and f.candidate_id != candidate_id:
            return False
        if track and f.track != track:
            return False
        if severity and f.severity != severity:
            return False
        if category and f.category != category:
            return False
        if run_id and f.run_id != run_id:
            return False
        return True

    matched = [f for f in failures if matches(f)]
    rate = (len(matched) / total_n) if total_n else None
    return FailureAnalyticsResult(
        count=len(matched), rate=rate, cases=[f.as_dict() for f in matched],
    )


# ---------------------------------------------------------------------------
# 39. Routing analytics -- append-only log
# ---------------------------------------------------------------------------

class RoutingLog:
    """Append-only JSONL log of every routing decision. Never rewrites a
    prior entry -- see module docstring rule 2."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def record(self, decision: RoutingDecision) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a") as fh:
            fh.write(json.dumps(decision.as_dict()) + "\n")

    def all(self) -> list[RoutingDecision]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text().splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            out.append(RoutingDecision(**d))
        return out

    def query(
        self, *, task: str | None = None, selected_candidate: str | None = None,
        execution_id: str | None = None,
    ) -> list[RoutingDecision]:
        def matches(d: RoutingDecision) -> bool:
            if task and d.task != task:
                return False
            if selected_candidate and d.selected_candidate != selected_candidate:
                return False
            if execution_id and d.execution_id != execution_id:
                return False
            return True
        return [d for d in self.all() if matches(d)]


# ---------------------------------------------------------------------------
# 40. Report aggregation -- the exact shape the student frontend contract
#     expects (AIOverview / TrackResult / CandidateSummary). Pre-computed
#     here; the frontend never performs this math. See the frontend spec's
#     "REQUIRED DATA CONTRACT" section.
# ---------------------------------------------------------------------------

STUDENT_TRACK_LABELS: dict[str, str] = {
    "A_medical_qa": "Medical knowledge",
    "B_concept_extraction": "Concept understanding",
    "C_concept_resolution_f1": "Concept understanding",
    "C_concept_false_merge": "Concept understanding",
    "D_relationships": "Relationships",
    "E_generation": "Question generation",
    "F_validation": "Question validation",
    "G_cross_subject": "Knowledge-gap detection",
    "H_fake_mastery_duplicates": "Knowledge-gap detection",
    "H_fake_mastery_coverage": "Knowledge-gap detection",
    "I_robustness": "Robustness",
    "J_injection": "Robustness",
    "J_injection_tool_violations": "Robustness",
    "safety_override_cme": "Medical knowledge",
}


def ai_overview(result: CandidateBenchmarkResult) -> dict:
    """The `AIOverview` shape from the frontend data contract."""
    return {
        "overallScore": _ranking_score(result),
        "status": result.student_status,
        "benchmarkVersion": result.run.benchmark_version,
        "evaluatedAt": result.run.timestamp,
        "sampleSize": max((t.n for t in result.tracks), default=0),
        "confidenceInterval": _overall_ci(result),
        "currentCandidate": result.run.candidate_id,
    }


def _overall_ci(result: CandidateBenchmarkResult) -> list[float] | None:
    """Widest observed band across higher-is-better tracks only -- same
    direction filter and rationale as `_ranking_score`. This is a rough
    display rollup, not a real statistical interval; it exists because the
    frontend contract's AIOverview asks for one `confidenceInterval` field.

    Bounds are divided by each gate's `scale_max` for the same reason the
    estimates are: the rubric gate's raw band is on a 0-4 axis, and mixing it
    in unscaled produced a displayed interval whose upper bound was 360 on a
    0-100 chart."""
    lower_direction = [t for t in result.tracks if t.direction == "lower"]
    los = [t.ci_lower / (t.scale_max or 1.0)
          for t in lower_direction if t.ci_lower is not None]
    his = [t.ci_upper / (t.scale_max or 1.0)
          for t in lower_direction if t.ci_upper is not None]
    if not los or not his:
        # Nothing higher-is-better to roll up. If every track here is an
        # error rate, invert its band onto the same axis the score already
        # uses (bounds swap: [1-upper, 1-lower]) rather than reporting no
        # interval beside a perfectly good number -- the mirror of the
        # all-error-rate case handled in `student_track_results`.
        inv_lo, inv_hi = [], []
        for t in result.tracks:
            if t.direction == "lower" or t.ci_lower is None or t.ci_upper is None:
                continue
            scale = t.scale_max or 1.0
            inv_lo.append(1.0 - t.ci_upper / scale)
            inv_hi.append(1.0 - t.ci_lower / scale)
        if not inv_lo or not inv_hi:
            return None
        return [min(inv_lo), max(inv_hi)]
    return [min(los), max(his)]


def student_track_results(result: CandidateBenchmarkResult) -> list[dict]:
    """The `TrackResult[]` shape from the frontend data contract, grouped
    and relabeled into the student-facing track names. Multiple internal
    gates can map to one student-facing track (e.g. concept resolution's F1
    and false-merge gates both feed "Concept understanding"); when they do,
    the worse status wins and n is the largest measured, matching the
    principle that a student should never see a track look healthier than
    its weakest measured component."""
    STATUS_RANK = {"pass": 0, "review": 1, "unavailable": 1, "fail": 2}
    grouped: dict[str, list[TrackResult]] = defaultdict(list)
    for t in result.tracks:
        label = STUDENT_TRACK_LABELS.get(t.track, t.track)
        grouped[label].append(t)

    out = []
    for label, members in grouped.items():
        worst = max(members, key=lambda t: STATUS_RANK.get(t.student_status, 1))
        # Same direction filter as _ranking_score: e.g. "Concept
        # understanding" groups GATE-C-F1 (accuracy-like, higher better)
        # with GATE-C-MERGE (an error rate, lower better) -- averaging their
        # raw estimates together would be the identical bug fixed there.
        # The group's STATUS still reflects both (worst-status-wins above);
        # only the numeric score excludes the error-rate gates. Dividing by
        # `scale_max` keeps a 0-4 rubric mean off the same axis as a
        # proportion (see `_ranking_score`).
        scored = [m.score / (m.scale_max or 1.0) for m in members
                 if m.score is not None and m.direction == "lower"]
        # ...unless the group is made ENTIRELY of error-rate gates. "Question
        # validation" is only GATE-F-FALSEAPPROVE, so the filter above left it
        # with nothing and the track rendered blank on the one screen whose
        # purpose is disclosure. There is no mixing hazard when every member
        # shares a direction, so invert them onto the "higher is better" axis
        # and report the number rather than showing an empty cell.
        if not scored:
            scored = [s for s in (normalized_track_score(m) for m in members)
                     if s is not None]
        out.append({
            "track": label,
            "score": (sum(scored) / len(scored)) if scored else None,
            "status": worst.student_status,
            "sampleSize": max((m.n for m in members), default=0),
            "confidenceInterval": _overall_ci(
                CandidateBenchmarkResult(run=result.run, tracks=members)
            ),
        })
    return out


def candidate_summary(result: CandidateBenchmarkResult) -> dict:
    """The `CandidateSummary` shape from the frontend data contract."""
    manifest = result.run.candidate_manifest or {}
    return {
        "candidateId": result.run.candidate_id,
        "provider": manifest.get("provider"),
        "model": manifest.get("model_id"),
        "version": manifest.get("model_version"),
        "overallScore": _ranking_score(result),
        "status": result.student_status,
        "lastEvaluated": result.run.timestamp,
    }
