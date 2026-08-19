"""
The AI engine the student pipeline calls.

This is where the benchmark stops being a report and starts deciding what the
product does. Which model serves a task is resolved in a fixed order:

  1. **A promoted production deployment.** An admin has explicitly activated a
     candidate for this task, against a benchmark run that passed. This is the
     only path intended for real use, and `promote()` refuses to create one
     from a run that did not pass.

  2. **The deterministic router**, if nothing is promoted. Capability filter,
     then safety filter, then benchmark eligibility, then score. No model is
     asked which model to use.

  3. **A development candidate**, only if one was explicitly configured. Every
     call made this way is stamped `development_override` in the execution log,
     because a figure produced by an unevaluated model must never be
     indistinguishable from one produced by a promoted one.

  4. **Nothing.** `NoEligibleModel` is raised. An app that cannot honestly
     answer says so; it does not quietly fall back to a model nobody checked.

Today, on this repository, step 1 and 2 both come up empty -- no candidate has
a passing benchmark run, because the corpus does not exist yet. So a deployment
that wants working question generation must configure step 3 and live with the
stamp. That is the honest state of the system, and it is visible in every
execution record rather than buried in a config file.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .db import Database, new_id, now_iso


class NoEligibleModel(RuntimeError):
    """No candidate is promoted, eligible, or configured for this task."""


class AICallFailed(RuntimeError):
    """The model was reached but did not return a usable answer."""


@dataclass
class AIResult:
    text: str
    parsed: dict | None
    candidate_id: str
    model: str
    provider: str
    source: str          # promoted | routed | development_override
    latency_ms: float
    attempts: int
    execution_id: str


class AIEngine:
    """
    Resolves a task to a model and calls it, recording every execution.

    `provider_factory(candidate_row) -> provider` is injected so this module
    has no opinion about NVIDIA specifically, matching
    `benchmark/orchestration.py`'s reasoning. Tests supply a scripted factory.
    """

    def __init__(self, db: Database, *, registry=None, archive=None,
                 provider_factory=None, development_candidate: str | None = None):
        self.db = db
        self.registry = registry
        self.archive = archive
        self.provider_factory = provider_factory
        # Explicit opt-in only. Never inferred, never defaulted to "the first
        # registered candidate" -- that is how an unevaluated model ends up
        # serving production without anyone deciding to let it.
        self.development_candidate = development_candidate or os.environ.get(
            "QUINTEK_DEV_CANDIDATE") or None

    # ---------- resolution ----------

    def active_deployment(self, task_type: str) -> dict | None:
        row = self.db.query_one(
            "SELECT * FROM production_deployments WHERE task_type = ? AND deactivated_at IS NULL"
            " ORDER BY activated_at DESC LIMIT 1", (task_type,))
        return dict(row) if row else None

    def resolve(self, task_type: str) -> tuple[str, str]:
        """Return `(candidate_id, source)` or raise NoEligibleModel."""
        deployment = self.active_deployment(task_type)
        if deployment:
            return deployment["candidate_id"], "promoted"

        if self.registry is not None and self.archive is not None:
            from benchmark.router import Router
            from benchmark.tasks import TaskType
            try:
                task = TaskType(task_type)
            except ValueError:
                task = None
            if task is not None:
                result = Router(self.registry, self.archive).select(task)
                if result.selected_candidate:
                    return result.selected_candidate, "routed"

        if self.development_candidate:
            return self.development_candidate, "development_override"

        raise NoEligibleModel(
            f"no model is available for {task_type}: nothing is promoted, no candidate is "
            "benchmark-eligible, and no development candidate is configured"
        )

    # ---------- calling ----------

    def call(self, task_type: str, prompt: str, *, system: str = "",
             max_tokens: int = 1024, temperature: float = 0.0,
             prompt_version: str = "v1") -> AIResult:
        candidate_id, source = self.resolve(task_type)

        if self.provider_factory is None:
            raise NoEligibleModel("no provider factory is configured on this AI engine")

        candidate = None
        if self.registry is not None:
            candidate = self.registry.get(candidate_id)
        provider = self.provider_factory(candidate or candidate_id)

        from benchmark.providers.base import GenerationRequest
        execution_id = new_id("exec")
        request = GenerationRequest(item_id=execution_id, prompt=prompt, system=system,
                                    max_tokens=max_tokens, temperature=temperature)
        response = provider.generate(request)

        self._record(execution_id, task_type, candidate_id, source, provider,
                     response, prompt_version)

        if not response.ok:
            raise AICallFailed(
                f"{task_type} call to {getattr(provider, 'model', candidate_id)} failed after "
                f"{response.attempts} attempt(s): {response.error}")

        return AIResult(
            text=response.raw_output, parsed=response.parsed, candidate_id=candidate_id,
            model=getattr(provider, "model", ""), provider=getattr(provider, "name", ""),
            source=source, latency_ms=response.latency_ms, attempts=response.attempts,
            execution_id=execution_id,
        )

    def _record(self, execution_id, task_type, candidate_id, source, provider,
                response, prompt_version) -> None:
        """
        Append to the orchestrator's execution log if one is configured.

        `source` is carried through so an auditor can tell a promoted call from
        a development-override call without cross-referencing anything.
        """
        from benchmark.orchestration import ExecutionLog, ExecutionRecord
        log_path = os.environ.get("QUINTEK_EXECUTION_LOG", "executions.jsonl")
        try:
            ExecutionLog(Path(log_path)).record(ExecutionRecord(
                execution_id=execution_id, task_type=task_type, candidate_id=candidate_id,
                provider=getattr(provider, "name", ""), model=getattr(provider, "model", ""),
                model_version=getattr(provider, "model_version", ""),
                prompt_version=f"{prompt_version}:{source}", timestamp=now_iso(),
                latency_ms=response.latency_ms, input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                status="ok" if response.ok else "error", error=response.error,
                routing_policy=source.upper(), attempt_number=response.attempts,
            ))
        except Exception:
            # Telemetry must never take down the call it was measuring.
            pass

    # ---------- promotion (the benchmark -> production gate) ----------

    def promote(self, task_type: str, candidate_id: str, benchmark_run_id: str, *,
                outcome: str, activated_by: str = "", signoff_name: str = "",
                signoff_rationale: str = "", run_candidate_id: str | None = None) -> str:
        """
        Activate a candidate for a task. Enforces the gate from
        `docs/QUINTEK_LOGIC.md` section 7, in code rather than in prose.

        A CONDITIONAL run may be promoted only with a named human and a
        rationale, because "conditional" means someone accepted a specific
        shortfall and that acceptance needs an owner.
        """
        if outcome == "PASS":
            pass
        elif outcome == "CONDITIONAL":
            if not signoff_name.strip() or not signoff_rationale.strip():
                raise ValueError(
                    "a CONDITIONAL run needs a named sign-off and a rationale: someone has to "
                    "own the accepted shortfall")
        else:
            raise ValueError(
                f"cannot promote a run whose outcome is {outcome!r}; only PASS, or CONDITIONAL "
                "with a recorded sign-off, may serve production")

        if run_candidate_id is not None and run_candidate_id != candidate_id:
            raise ValueError(
                f"run {benchmark_run_id} evaluated candidate {run_candidate_id}, not "
                f"{candidate_id} -- a candidate cannot be promoted on another's evidence")

        # Never delete: the previous deployment is history, and "what was
        # serving this task in March" has to remain answerable.
        self.db.execute(
            "UPDATE production_deployments SET deactivated_at = ?"
            " WHERE task_type = ? AND deactivated_at IS NULL", (now_iso(), task_type))
        did = new_id("dep")
        self.db.execute(
            "INSERT INTO production_deployments (id, task_type, candidate_id, benchmark_run_id,"
            " outcome, activated_at, activated_by, signoff_name, signoff_rationale)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (did, task_type, candidate_id, benchmark_run_id, outcome, now_iso(),
             activated_by, signoff_name, signoff_rationale))
        return did

    def deployment_history(self, task_type: str | None = None) -> list[dict]:
        if task_type:
            rows = self.db.query(
                "SELECT * FROM production_deployments WHERE task_type = ?"
                " ORDER BY activated_at DESC", (task_type,))
        else:
            rows = self.db.query(
                "SELECT * FROM production_deployments ORDER BY activated_at DESC")
        return [dict(r) for r in rows]


def extract_json(text: str) -> dict | None:
    """
    First JSON object in a model reply.

    Models wrap JSON in prose and fences no matter how firmly the prompt asks
    them not to. A reply that contains no object is unparseable, which is a
    different failure from a wrong answer and is recorded as such.
    """
    if not text:
        return None
    depth, start = 0, None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    start = None
    return None
