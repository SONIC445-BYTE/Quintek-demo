"""
The benchmark -> production promotion surface.

Everything else in this repository measures models. This is the one place a
measurement turns into a decision: an admin picks a candidate, names the run
that justifies it, and from that moment the learner-facing engine calls that
model for that task.

Three properties are enforced here rather than documented and hoped for:

  * **The gate is code.** `student.ai.AIEngine.promote` refuses anything that
    is not a PASS, or a CONDITIONAL carrying a named human and a written
    rationale. This module does not re-implement that check -- it calls it, so
    there is exactly one gate and no chance of the API and the engine
    disagreeing about what is promotable.

  * **Refusal is explained, not just returned.** `eligible()` reports every
    candidate the archive knows about, promotable or not, each with the reason.
    An admin who cannot promote the model they wanted needs to know whether the
    run failed, was withheld, or simply does not exist -- those are three
    different problems with three different fixes.

  * **Deactivation is not deletion.** Standing down a deployment returns the
    task to the deterministic router and leaves the record in place. "What was
    serving question generation in March, and who decided that" stays
    answerable forever.

The read routes are safe to expose to the admin console. The write routes are
not safe to expose to anyone else, and this module does not authenticate --
`student/api.py` owns admin authentication and calls in here once the caller
has been checked.
"""

from __future__ import annotations

from pathlib import Path


class PromotionError(ValueError):
    """The requested promotion is not permitted, with a reason."""


def _task_values() -> list[str]:
    from .tasks import TaskType
    return [t.value for t in TaskType]


class PromotionAPI:
    """
    Composes the benchmark archive (evidence) with the student database
    (decisions). Both are required: a promotion without a run is unfounded,
    and a run without a deployment record changes nothing.
    """

    def __init__(self, ai_engine, archive=None, *, registry=None, runs_root: str | Path = "runs"):
        self.ai = ai_engine
        if archive is None:
            from .analytics import RunArchive
            archive = RunArchive(runs_root)
        self.archive = archive
        self.registry = registry if registry is not None else getattr(ai_engine, "registry", None)

    # ---------- evidence ----------

    def _runs_index(self) -> dict[str, dict]:
        """run_id -> the facts promotion cares about."""
        index: dict[str, dict] = {}
        for result in self.archive.all_runs():
            run = result.run
            index[run.run_id] = {
                "run_id": run.run_id,
                "candidate_id": run.candidate_id,
                "outcome": run.outcome,
                "rankable": run.rankable,
                "timestamp": run.timestamp,
                "integrity_satisfied": run.integrity_satisfied,
                "scores_withheld": run.scores_withheld,
                "benchmark_version": run.benchmark_version,
            }
        return index

    def _blocking_reason(self, run: dict) -> str | None:
        """
        Why this run may not be promoted, or None if it may.

        Ordered from most fundamental to least, so the message names the thing
        that must be fixed first rather than the first thing checked.
        """
        if not run["integrity_satisfied"]:
            return ("the run's integrity checks did not pass, so its scores are not evidence "
                    "of anything")
        if run["scores_withheld"]:
            return "scores were withheld for this run, so there is nothing to promote on"
        if run["outcome"] == "PASS":
            return None
        if run["outcome"] == "CONDITIONAL":
            return None  # promotable, but only with a named sign-off -- checked at promote time
        return (f"the run's outcome is {run['outcome']}; only PASS, or CONDITIONAL with a "
                "recorded sign-off, may serve production")

    def eligible(self, task_type: str | None = None) -> dict:
        """
        Every run in the archive, annotated with whether it could serve
        `task_type` and why not if it could not.

        Deliberately returns the ineligible ones too. A list that silently
        omits the candidate an admin is looking for reads as "the system lost
        my run", and they will go looking in the filesystem.
        """
        if task_type is not None and task_type not in _task_values():
            raise PromotionError(
                f"unknown task type {task_type!r}; expected one of {', '.join(_task_values())}")

        rows = []
        for run in sorted(self._runs_index().values(),
                          key=lambda r: (r["timestamp"], r["run_id"]), reverse=True):
            blocking = self._blocking_reason(run)
            entry = self.registry.get(run["candidate_id"]) if self.registry else None
            rows.append({
                **run,
                "promotable": blocking is None,
                "requires_signoff": blocking is None and run["outcome"] == "CONDITIONAL",
                "blocking_reason": blocking,
                "registry_status": getattr(entry, "status", None),
                "model_id": getattr(entry, "model_id", None),
                "provider": getattr(entry, "provider", None),
            })

        return {
            "task_type": task_type,
            "runs": rows,
            "promotable_count": sum(1 for r in rows if r["promotable"]),
            # Said plainly rather than left to be inferred from an empty list.
            "note": ("No run in this archive is promotable. Until one is, the engine falls "
                     "back to the deterministic router, and then to a development candidate "
                     "if one is configured -- every such call is stamped "
                     "'development_override'.") if not any(r["promotable"] for r in rows) else "",
        }

    # ---------- current state ----------

    def current(self) -> dict:
        """What is serving each task right now, and on what authority."""
        from .tasks import TaskType

        tasks = []
        for task in TaskType:
            deployment = self.ai.active_deployment(task.value)
            if deployment:
                source, candidate = "promoted", deployment["candidate_id"]
            else:
                candidate, source = None, "unresolved"
                try:
                    candidate, source = self.ai.resolve(task.value)
                except Exception:
                    pass
            entry = self.registry.get(candidate) if (self.registry and candidate) else None
            tasks.append({
                "task_type": task.value,
                "candidate_id": candidate,
                "source": source,
                "model_id": getattr(entry, "model_id", None),
                "provider": getattr(entry, "provider", None),
                "deployment": deployment,
                # The single most important field on this screen: is this
                # model one somebody signed off on, or one nobody checked?
                "evidence_backed": source in {"promoted", "routed"},
            })
        return {"tasks": tasks,
                "promoted_count": sum(1 for t in tasks if t["source"] == "promoted"),
                "unresolved_count": sum(1 for t in tasks if t["source"] == "unresolved")}

    def history(self, task_type: str | None = None) -> dict:
        return {"task_type": task_type, "deployments": self.ai.deployment_history(task_type)}

    # ---------- the decision ----------

    def promote(self, *, task_type: str, run_id: str, activated_by: str = "",
                signoff_name: str = "", signoff_rationale: str = "") -> dict:
        """
        Activate the candidate that `run_id` evaluated, for `task_type`.

        The candidate is read from the run rather than accepted from the
        caller. Letting a request name both is how a passing run for model A
        ends up promoting model B -- the two fields agree by construction here.
        """
        if task_type not in _task_values():
            raise PromotionError(
                f"unknown task type {task_type!r}; expected one of {', '.join(_task_values())}")

        run = self._runs_index().get(run_id)
        if run is None:
            raise PromotionError(
                f"no run {run_id!r} in the archive; promotion must name a run that exists")

        blocking = self._blocking_reason(run)
        if blocking:
            raise PromotionError(f"run {run_id} cannot serve production: {blocking}")

        try:
            deployment_id = self.ai.promote(
                task_type, run["candidate_id"], run_id, outcome=run["outcome"],
                activated_by=activated_by, signoff_name=signoff_name,
                signoff_rationale=signoff_rationale, run_candidate_id=run["candidate_id"])
        except ValueError as exc:
            raise PromotionError(str(exc)) from exc

        return {"deployment_id": deployment_id, "task_type": task_type,
                "candidate_id": run["candidate_id"], "run_id": run_id,
                "outcome": run["outcome"], "activated_by": activated_by,
                "signoff_name": signoff_name}

    def deactivate(self, task_type: str, *, deactivated_by: str = "") -> dict:
        """
        Stand down the current deployment; the task returns to the router.

        Not a delete. The row keeps its activation record and gains a
        deactivation timestamp, because an audit of a bad answer produced last
        month needs to find the model that produced it.
        """
        from student.db import now_iso

        deployment = self.ai.active_deployment(task_type)
        if deployment is None:
            raise PromotionError(f"nothing is promoted for {task_type}; there is nothing to stand down")
        self.ai.db.execute(
            "UPDATE production_deployments SET deactivated_at = ?, deactivated_by = ?"
            " WHERE id = ?", (now_iso(), deactivated_by, deployment["id"]))
        fallback = "unresolved"
        try:
            _, fallback = self.ai.resolve(task_type)
        except Exception:
            pass
        return {"task_type": task_type, "deactivated": deployment["id"],
                "candidate_id": deployment["candidate_id"], "now_serving": fallback}

    # ---------- HTTP adapter ----------

    def handle_get(self, path: str, params: dict) -> tuple[int, dict] | None:
        def one(key, default=None):
            value = params.get(key, default)
            return value[0] if isinstance(value, list) and value else (
                default if isinstance(value, list) else value)

        try:
            if path == "/api/promotions":
                return 200, self.current()
            if path == "/api/promotions/eligible":
                return 200, self.eligible(one("task"))
            if path == "/api/promotions/history":
                return 200, self.history(one("task"))
        except PromotionError as exc:
            return 400, {"error": str(exc)}
        return None

    def handle_post(self, path: str, body: dict) -> tuple[int, dict] | None:
        try:
            if path == "/api/promotions":
                return 201, self.promote(
                    task_type=body.get("task_type", ""), run_id=body.get("run_id", ""),
                    activated_by=body.get("activated_by", ""),
                    signoff_name=body.get("signoff_name", ""),
                    signoff_rationale=body.get("signoff_rationale", ""))
            if path == "/api/promotions/deactivate":
                return 200, self.deactivate(body.get("task_type", ""),
                                            deactivated_by=body.get("deactivated_by", ""))
        except PromotionError as exc:
            return 400, {"error": str(exc)}
        return None
