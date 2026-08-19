"""
The Quintek AI Benchmark, as a learner sees it.

This is a disclosure surface, not a scoreboard. A learner revising for an exam
has one real question -- "should I trust the thing that just marked me wrong?"
-- and everything here exists to answer it. That framing rules out a few
things that a general model leaderboard would do:

  * **It is not a ranking of AI models.** Every payload carries `scope_note`
    saying so. These figures come from Quintek's own tasks on Quintek's own
    corpus; a model that scores poorly here may be excellent at things Quintek
    never asked it to do. Publishing a number without that sentence invites the
    reading it cannot support.

  * **A number without its uncertainty is not shown.** Sample size and interval
    travel with every score, because "82%" over eleven questions and "82%" over
    nine hundred are different claims and a learner cannot tell them apart
    unless we say.

  * **Absence is stated, never filled in.** With no benchmark runs, this module
    returns empty rankings and an explicit `empty_reason`. It never falls back
    to sample data. A transparency screen showing figures that were not
    measured is worse than one showing nothing, because it spends the trust it
    was built to earn.

  * **What is actually serving is separated from what scored best.** The
    highest-ranked candidate is frequently not the one answering the learner's
    questions -- because nothing has been promoted, or because a safety gate
    excluded it. `powering()` reports the truth including the uncomfortable
    case where an unevaluated development model is serving, which is the state
    of this repository today.

Composition, not reimplementation: ranking, normalization and interval
arithmetic all live in `benchmark/analytics.py`. This module reshapes them and
adds the learner-facing language.
"""

from __future__ import annotations

from dataclasses import dataclass

SCOPE_NOTE = (
    "These results measure how each AI performs on Quintek's own tasks, using Quintek's own "
    "medical revision material. They are not a general ranking of AI models — a model that "
    "scores lower here may be stronger at things Quintek never asks it to do."
)

_EMPTY_REASON = (
    "No AI has completed a Quintek benchmark run yet, so there is nothing measured to show. "
    "Until one has, this screen stays empty rather than displaying example figures."
)


@dataclass(frozen=True)
class Category:
    """
    One tab. `gate_ids` empty means the overall view, which uses the
    cross-track ranking rather than a per-task average.
    """
    key: str
    label: str
    blurb: str
    gate_ids: tuple[str, ...] = ()
    task_type: str | None = None


# Ordered as a learner would read them: the summary first, then the tasks that
# most visibly shape their revision, then the ones that protect them quietly.
CATEGORIES: tuple[Category, ...] = (
    Category("overall", "Overall",
             "Across every Quintek task, combined.", ()),
    Category("medical_qa", "Medical accuracy",
             "Whether the AI's own medical answers are correct.",
             ("GATE-A-ACC",), "EXPLANATION"),
    Category("question_generation", "Question generation",
             "Whether the questions it writes are answerable, grounded in your source, and "
             "actually about the concept they claim to test.",
             ("GATE-E-RUBRIC",), "QUESTION_GENERATION"),
    Category("question_validation", "Question checking",
             "Whether it catches a bad question before you ever see it.",
             ("GATE-F-FALSEAPPROVE",), "QUESTION_VALIDATION"),
    Category("concepts", "Concept understanding",
             "Whether it pulls the right concepts out of your notes, and whether it keeps two "
             "different concepts apart.",
             ("GATE-B-F1", "GATE-C-F1", "GATE-C-MERGE"), "CONCEPT_RESOLUTION"),
    Category("relationships", "Relationships",
             "Whether it correctly links concepts to one another.",
             ("GATE-D-F1",), "RELATIONSHIP_EXTRACTION"),
    Category("knowledge_gaps", "Knowledge-gap detection",
             "Whether it spots what you do not know — including the gap you have hidden by "
             "memorising one phrasing of a question.",
             ("GATE-G-LINK", "GATE-H-FAMILY", "GATE-H-DUP"), "KNOWLEDGE_GAP_EXTRACTION"),
    Category("robustness", "Robustness",
             "Whether it holds up when a question is worded strangely, or when your source "
             "contains text trying to manipulate it.",
             # Spans two tasks rather than one, so task_type stays None: this
             # tab is a property of the whole system, not of one step in it.
             ("GATE-I-RETENTION", "GATE-J-ATTACK", "GATE-J-TOOL"), None),
)

CATEGORIES_BY_KEY = {c.key: c for c in CATEGORIES}

# A learner reading "measured" should not have to learn our vocabulary.
_STATUS_PROSE = {
    "pass": "Met Quintek's requirement for this task.",
    "fail": "Did not meet Quintek's requirement for this task.",
    "review": "Measured, but the result needs human review before it counts.",
    "unavailable": "Not measured — this task was not scored in that run.",
}


class TransparencyService:
    """
    Reads the benchmark archive and the deployment record; writes nothing.

    `archive` and `registry` may be None -- a Quintek install that has never
    run a benchmark is a legitimate state and must render, saying what is
    missing rather than raising.
    """

    def __init__(self, *, archive=None, registry=None, ai_engine=None):
        self.archive = archive
        self.registry = registry
        self.ai = ai_engine

    # ---------- helpers ----------

    def _results(self) -> list:
        if self.archive is None:
            return []
        out = []
        for cid in self.archive.candidates():
            result = self.archive.latest_run_for_candidate(cid)
            if result is not None:
                out.append(result)
        return out

    def _display_name(self, candidate_id: str, result=None) -> str:
        """
        A learner-legible name. Falls back through registry, then the run's
        own manifest, then the candidate id -- an id is ugly but it is true,
        which beats inventing a friendly name for a model we cannot identify.
        """
        entry = self.registry.get(candidate_id) if self.registry else None
        model = getattr(entry, "model_id", None)
        if not model and result is not None:
            model = (result.run.candidate_manifest or {}).get("model_id")
        return model or candidate_id

    def _provider(self, candidate_id: str, result=None) -> str | None:
        entry = self.registry.get(candidate_id) if self.registry else None
        provider = getattr(entry, "provider", None)
        if not provider and result is not None:
            provider = (result.run.candidate_manifest or {}).get("provider")
        return provider

    # ---------- rankings ----------

    def ranking(self, category_key: str = "overall") -> dict:
        """
        One tab's table.

        Unmeasured candidates sort last and keep a null score rather than a
        zero. Zero is a measurement; "we did not measure this" is not, and a
        table that renders them identically is lying by layout.
        """
        from benchmark import analytics as an

        category = CATEGORIES_BY_KEY.get(category_key)
        if category is None:
            raise KeyError(category_key)

        results = self._results()
        if not results:
            return {"category": category.key, "label": category.label,
                    "blurb": category.blurb, "entries": [], "measured_count": 0,
                    "empty_reason": _EMPTY_REASON, "scope_note": SCOPE_NOTE}

        entries = []
        for result in results:
            cid = result.run.candidate_id
            if category.gate_ids:
                members = [t for t in result.tracks if t.gate_id in category.gate_ids]
                normalized = [n for n in (an.normalized_track_score(m) for m in members)
                              if n is not None]
                score = (sum(normalized) / len(normalized)) if normalized else None
                sample = max((m.n for m in members), default=0)
                interval = an._overall_ci(
                    an.CandidateBenchmarkResult(run=result.run, tracks=members)) if members else None
                status = self._group_status(members)
            else:
                score = an._ranking_score(result)
                sample = max((t.n for t in result.tracks), default=0)
                interval = an._overall_ci(result)
                status = result.student_status

            entries.append({
                "candidate_id": cid,
                "name": self._display_name(cid, result),
                "provider": self._provider(cid, result),
                "score": score,
                "sample_size": sample,
                "confidence_interval": interval,
                "status": status,
                "status_prose": _STATUS_PROSE.get(status, "Not measured."),
                "production_eligible": result.production_eligible,
                "last_evaluated": result.run.timestamp,
                "run_id": result.run.run_id,
            })

        # Unmeasured last; among measured, higher first; ties broken on name so
        # the table does not reshuffle between two identical loads.
        entries.sort(key=lambda e: (e["score"] is None, -(e["score"] or 0.0), e["name"]))
        rank = 0
        for entry in entries:
            if entry["score"] is None:
                entry["rank"] = None
                continue
            rank += 1
            entry["rank"] = rank

        measured = sum(1 for e in entries if e["score"] is not None)
        return {
            "category": category.key, "label": category.label, "blurb": category.blurb,
            "entries": entries, "measured_count": measured,
            "empty_reason": "" if measured else
                ("Runs exist, but none of them scored this task, so there is nothing to rank "
                 "here yet."),
            "scope_note": SCOPE_NOTE,
            # Stated once per payload so a single-tab render still carries it.
            "ranking_caveat": ("Ranks compare candidates on this task only. A gap smaller than "
                               "the confidence intervals overlap is not a real difference."),
        }

    @staticmethod
    def _group_status(members) -> str:
        if not members:
            return "unavailable"
        order = {"pass": 0, "review": 1, "unavailable": 1, "fail": 2}
        return max(members, key=lambda m: order.get(m.student_status, 1)).student_status

    def categories(self) -> dict:
        """Tab list, each annotated with whether it has anything to show."""
        tabs = []
        for category in CATEGORIES:
            try:
                measured = self.ranking(category.key)["measured_count"]
            except Exception:
                measured = 0
            tabs.append({"key": category.key, "label": category.label,
                         "blurb": category.blurb, "measured_count": measured,
                         "task_type": category.task_type})
        return {"categories": tabs, "scope_note": SCOPE_NOTE}

    # ---------- what is actually serving ----------

    def powering(self) -> dict:
        """
        "Currently powering Quintek".

        Reports the honest state including the one nobody wants to publish: a
        model serving without a passing benchmark run behind it. If that is
        true it says so here, because a learner who later discovers it from
        somewhere else has learnt something worse than the fact itself.
        """
        if self.ai is None:
            return {"tasks": [], "scope_note": SCOPE_NOTE,
                    "empty_reason": "This install has no AI engine configured.",
                    "all_evidence_backed": False}

        from benchmark.tasks import TaskType

        tasks = []
        for task in TaskType:
            deployment = self.ai.active_deployment(task.value)
            candidate, source = None, "unresolved"
            if deployment:
                candidate, source = deployment["candidate_id"], "promoted"
            else:
                try:
                    candidate, source = self.ai.resolve(task.value)
                except Exception:
                    pass

            result = (self.archive.latest_run_for_candidate(candidate)
                      if (self.archive and candidate) else None)
            tasks.append({
                "task_type": task.value,
                "task_label": self._task_label(task.value),
                "candidate_id": candidate,
                "name": self._display_name(candidate, result) if candidate else None,
                "provider": self._provider(candidate, result) if candidate else None,
                "source": source,
                "basis": _SOURCE_PROSE.get(source, "Unknown."),
                "evidence_backed": source in {"promoted", "routed"},
                "run_id": result.run.run_id if result else None,
                "last_evaluated": result.run.timestamp if result else None,
            })

        unevaluated = [t for t in tasks if t["source"] == "development_override"]
        unresolved = [t for t in tasks if t["source"] == "unresolved"]
        return {
            "tasks": tasks,
            "all_evidence_backed": not unevaluated and not unresolved,
            "warning": (
                f"{len(unevaluated)} of Quintek's {len(tasks)} AI tasks are being served by a "
                "model that has not passed a Quintek benchmark run. Answers from those tasks "
                "carry the same caveat as any unverified source."
                if unevaluated else ""),
            "unresolved_note": (
                f"{len(unresolved)} task(s) have no model at all; features that need them will "
                "report that they are unavailable rather than guessing."
                if unresolved else ""),
            "scope_note": SCOPE_NOTE,
        }

    @staticmethod
    def _task_label(task_type: str) -> str:
        return _TASK_LABELS.get(task_type, task_type.replace("_", " ").capitalize())

    # ---------- one model ----------

    def profile(self, candidate_id: str) -> dict:
        """
        A model's page: identity, per-task fingerprint, history, and what it
        is trusted with today.
        """
        from benchmark import analytics as an

        if self.archive is None:
            raise KeyError(candidate_id)
        result = self.archive.latest_run_for_candidate(candidate_id)
        if result is None:
            raise KeyError(candidate_id)

        entry = self.registry.get(candidate_id) if self.registry else None
        fingerprint = []
        for category in CATEGORIES:
            if not category.gate_ids:
                continue
            members = [t for t in result.tracks if t.gate_id in category.gate_ids]
            normalized = [n for n in (an.normalized_track_score(m) for m in members)
                          if n is not None]
            fingerprint.append({
                "category": category.key, "label": category.label,
                "score": (sum(normalized) / len(normalized)) if normalized else None,
                "sample_size": max((m.n for m in members), default=0),
                "status": self._group_status(members),
                # The bar must be absent, not zero-length, when unmeasured.
                "measured": bool(normalized),
            })

        serving = []
        if self.ai is not None:
            from benchmark.tasks import TaskType
            for task in TaskType:
                try:
                    resolved, source = self.ai.resolve(task.value)
                except Exception:
                    continue
                if resolved == candidate_id:
                    serving.append({"task_type": task.value,
                                    "task_label": self._task_label(task.value),
                                    "source": source})

        return {
            "candidate_id": candidate_id,
            "name": self._display_name(candidate_id, result),
            "provider": self._provider(candidate_id, result),
            "version": (result.run.candidate_manifest or {}).get("model_version")
                       or getattr(entry, "model_version", None),
            "registry_status": getattr(entry, "status", None),
            "overall_score": an._ranking_score(result),
            "overall_interval": an._overall_ci(result),
            "status": result.student_status,
            "status_prose": _STATUS_PROSE.get(result.student_status, "Not measured."),
            "production_eligible": result.production_eligible,
            "safety_status": result.safety_status,
            "last_evaluated": result.run.timestamp,
            "run_id": result.run.run_id,
            "fingerprint": fingerprint,
            "tracks": an.student_track_results(result),
            "currently_serving": serving,
            "history": self.history(candidate_id)["points"],
            "scope_note": SCOPE_NOTE,
        }

    def history(self, candidate_id: str) -> dict:
        """
        Every run for one candidate, oldest first, for the trend chart.

        A single point is returned as a single point with a note, rather than
        as a chart. Two points joined by a line reads as a trend; one point
        drawn as a line reads as a flat one, and neither is true of a model
        evaluated once.
        """
        from benchmark import analytics as an

        if self.archive is None:
            return {"candidate_id": candidate_id, "points": [], "chartable": False,
                    "note": _EMPTY_REASON}
        runs = sorted(self.archive.runs_for_candidate(candidate_id),
                      key=lambda r: (r.run.timestamp, r.run.run_id))
        points = [{
            "run_id": r.run.run_id,
            "timestamp": r.run.timestamp,
            "score": an._ranking_score(r),
            "interval": an._overall_ci(r),
            "outcome": r.run.outcome,
            "benchmark_version": r.run.benchmark_version,
        } for r in runs]

        versions = {p["benchmark_version"] for p in points}
        return {
            "candidate_id": candidate_id,
            "points": points,
            "chartable": len(points) > 1,
            "note": ("" if len(points) > 1 else
                     "Evaluated once so far — there is no trend to draw yet."),
            # Comparing scores across benchmark versions compares two different
            # exams. Say so rather than drawing one continuous line over it.
            "version_warning": (
                "These runs span more than one benchmark version, so points are not directly "
                "comparable across the change." if len(versions) > 1 else ""),
        }

    # ---------- the whole screen ----------

    def overview(self) -> dict:
        """One call for the whole surface, so the screen renders atomically."""
        return {
            "title": "Quintek AI Benchmark",
            "scope_note": SCOPE_NOTE,
            "disclaimer": (
                "This is Quintek's own evaluation of the AI models it uses. It is not a "
                "general ranking of AI models, and it is not comparable to public leaderboards."),
            "how_it_works": _HOW_IT_WORKS,
            "categories": self.categories()["categories"],
            "powering": self.powering(),
            "ranking": self.ranking("overall"),
        }


_SOURCE_PROSE = {
    "promoted": "A person reviewed this model's benchmark run and activated it for this task.",
    "routed": "Selected automatically as the strongest model that passed Quintek's benchmark "
              "and safety gates for this task.",
    "development_override": "Configured manually for development. It has NOT passed a Quintek "
                            "benchmark run, and its answers are not evidence-backed.",
    "unresolved": "No model is available for this task. Features that need it report that they "
                  "are unavailable rather than guessing.",
}

_TASK_LABELS = {
    "SOURCE_PROCESSING": "Reading your uploads",
    "CONCEPT_EXTRACTION": "Finding concepts in your notes",
    "CONCEPT_RESOLUTION": "Keeping concepts distinct",
    "RELATIONSHIP_EXTRACTION": "Linking concepts together",
    "QUESTION_GENERATION": "Writing your questions",
    "QUESTION_VALIDATION": "Checking questions before you see them",
    "EXPLANATION": "Explaining answers",
    "KNOWLEDGE_GAP_EXTRACTION": "Spotting what you don't know",
    "REVISION_SELECTION": "Choosing what you revise next",
}

_HOW_IT_WORKS = [
    {"step": 1, "title": "Several AIs are registered",
     "text": "Quintek keeps a pool of candidate AI configurations rather than committing to "
             "one model."},
    {"step": 2, "title": "Each is tested on Quintek's own tasks",
     "text": "Every candidate answers the same fixed set of medical revision tasks it has "
             "never seen, on material chosen for this benchmark."},
    {"step": 3, "title": "Results are reported with their uncertainty",
     "text": "Every score arrives with how many items produced it and how wide its confidence "
             "interval is. A number on its own is not a result."},
    {"step": 4, "title": "Safety gates can exclude a model outright",
     "text": "A candidate that fails a mandatory safety gate cannot serve you, no matter how "
             "well it scored elsewhere."},
    {"step": 5, "title": "Each task goes to the best eligible model for that task",
     "text": "The model that writes your questions need not be the one that checks them. "
             "No single model is 'Quintek's AI'."},
]
