"""
Batch execution as a job queue, not a for-loop of HTTP calls.

The 500-item experiment on this project failed as an *infrastructure*
experiment: sequential requests into an endpoint with intermittent 100-second
stalls, with nothing watching. It measured the endpoint and cost an hour.

    500 items -> 500 sequential requests -> one slow host -> nothing learned

What is needed instead is a queue with workers, per-job records, a breaker in
the path, and the ability to stop early:

    500 items -> queue -> N workers -> provider(s) -> validator -> accept/reject

Four properties this gives that a loop cannot:

  * **A slow provider does not freeze the experiment.** Other workers keep
    going; the stalled one trips its breaker and stops taking jobs.
  * **Every job has a record.** started_at, finished_at, latency, status, raw
    output, error, attempt. "Which items did we actually get" is answerable
    while the run is still going.
  * **Early stop on degradation.** If acceptance falls off a cliff at item 60,
    there is no reason to pay for 440 more.
  * **Resumability.** Jobs already completed are skipped on a rerun, so an
    interrupted 500-item run does not restart from zero.

Threads rather than asyncio: every provider in this repository is a blocking
HTTP client, the work is entirely IO-bound, and threads keep the provider
interface unchanged. This is a reference implementation of the shape, not a
scheduler.
"""

from __future__ import annotations

import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

PENDING, RUNNING, DONE, FAILED, SKIPPED, ABORTED = (
    "pending", "running", "done", "failed", "skipped", "aborted")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Job:
    task_id: str
    task_type: str = ""
    payload: dict = field(default_factory=dict)
    job_id: str = field(default_factory=lambda: f"job_{uuid.uuid4().hex[:12]}")
    candidate: str = ""
    status: str = PENDING
    attempt: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    latency_ms: float | None = None
    raw_output: str | None = None
    result: dict | None = None
    error: str | None = None
    accepted: bool | None = None

    def as_dict(self) -> dict:
        return {"job_id": self.job_id, "task_id": self.task_id, "task_type": self.task_type,
                "candidate": self.candidate, "status": self.status, "attempt": self.attempt,
                "started_at": self.started_at, "finished_at": self.finished_at,
                "latency_ms": self.latency_ms, "error": self.error,
                "accepted": self.accepted,
                "raw_output_chars": len(self.raw_output or "")}


@dataclass
class StopPolicy:
    """
    When to abandon a run rather than pay for the rest of it.

    `min_before_judging` exists because an acceptance rate over the first
    three items is noise, and stopping on noise wastes the run in the other
    direction.
    """

    min_acceptance_rate: float | None = None
    min_before_judging: int = 20
    max_consecutive_failures: int = 10

    def should_stop(self, *, completed: int, accepted: int,
                    consecutive_failures: int) -> str | None:
        if consecutive_failures >= self.max_consecutive_failures:
            return (f"{consecutive_failures} consecutive failures: this is measuring the "
                    "endpoint, not the model")
        if (self.min_acceptance_rate is not None and completed >= self.min_before_judging):
            rate = accepted / completed if completed else 0.0
            if rate < self.min_acceptance_rate:
                return (f"acceptance rate {rate:.0%} over {completed} items is below the "
                        f"{self.min_acceptance_rate:.0%} floor; stopping rather than paying "
                        "for the rest")
        return None


class BatchRunner:
    """
    `execute(job) -> dict` does the work for one job and returns a result
    dict, optionally carrying `raw_output` and `accepted`. Everything about
    providers, prompts and validation is the caller's; this owns concurrency,
    recording, breakers and stopping.
    """

    def __init__(self, execute, *, workers: int = 4, health=None,
                 candidate_for=None, stop_policy: StopPolicy | None = None,
                 max_attempts: int = 2, on_job=None):
        self.execute = execute
        self.workers = max(1, workers)
        self.health = health
        self.candidate_for = candidate_for or (lambda job: job.candidate or "default")
        self.stop_policy = stop_policy or StopPolicy()
        self.max_attempts = max_attempts
        self.on_job = on_job

        self._lock = threading.Lock()
        self._stop_reason: str | None = None
        self._completed = 0
        self._accepted = 0
        self._consecutive_failures = 0

    # ---------- worker ----------

    def _run_one(self, job: Job) -> None:
        candidate = self.candidate_for(job)
        job.candidate = candidate

        # The breaker sits in the path, so a dead endpoint stops taking work
        # instead of absorbing the rest of the queue into timeouts.
        if self.health is not None and not self.health.allows(candidate):
            job.status = SKIPPED
            job.error = self.health.refusal_reason(candidate)
            job.finished_at = _now()
            return

        job.attempt += 1
        job.status = RUNNING
        job.started_at = _now()
        started = time.monotonic()
        try:
            result = self.execute(job) or {}
            job.latency_ms = (time.monotonic() - started) * 1000
            job.result = result
            job.raw_output = result.get("raw_output")
            job.accepted = result.get("accepted")
            job.status = DONE
            if self.health is not None:
                self.health.observe(candidate, success=True, latency_ms=job.latency_ms)
        except Exception as exc:
            job.latency_ms = (time.monotonic() - started) * 1000
            job.error = f"{type(exc).__name__}: {exc}"
            job.status = FAILED
            timeout = "timeout" in job.error.lower()
            if self.health is not None:
                self.health.observe(candidate, success=False, timeout=timeout,
                                    error=job.error)
        finally:
            job.finished_at = _now()

    def _worker(self, work: "queue.Queue[Job | None]", jobs: list[Job]) -> None:
        while True:
            job = work.get()
            if job is None:
                work.task_done()
                return
            with self._lock:
                stopping = self._stop_reason is not None
            if stopping:
                job.status = ABORTED
                job.error = self._stop_reason
                work.task_done()
                continue

            for _ in range(self.max_attempts):
                self._run_one(job)
                if job.status in (DONE, SKIPPED):
                    break

            with self._lock:
                if job.status == DONE:
                    self._completed += 1
                    self._consecutive_failures = 0
                    if job.accepted:
                        self._accepted += 1
                elif job.status == FAILED:
                    self._consecutive_failures += 1
                if self._stop_reason is None:
                    self._stop_reason = self.stop_policy.should_stop(
                        completed=self._completed, accepted=self._accepted,
                        consecutive_failures=self._consecutive_failures)
            if self.on_job:
                try:
                    self.on_job(job)
                except Exception:
                    pass    # observability must never take down the run
            work.task_done()

    # ---------- entry point ----------

    def run(self, jobs: list[Job], *, skip_done: set[str] | None = None) -> dict:
        """
        Execute `jobs`. `skip_done` holds task_ids already completed by an
        earlier run, so an interrupted batch resumes instead of restarting.
        """
        skip_done = skip_done or set()
        pending = []
        for job in jobs:
            if job.task_id in skip_done:
                job.status = SKIPPED
                job.error = "already completed by an earlier run"
            else:
                pending.append(job)

        work: "queue.Queue[Job | None]" = queue.Queue()
        for job in pending:
            work.put(job)
        threads = []
        for _ in range(min(self.workers, max(1, len(pending)))):
            work.put(None)
            thread = threading.Thread(target=self._worker, args=(work, jobs), daemon=True)
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join()

        by_status: dict[str, int] = {}
        for job in jobs:
            by_status[job.status] = by_status.get(job.status, 0) + 1
        done = [j for j in jobs if j.status == DONE]
        latencies = sorted(j.latency_ms for j in done if j.latency_ms is not None)

        return {
            "total": len(jobs),
            "by_status": by_status,
            "completed": len(done),
            "accepted": sum(1 for j in done if j.accepted),
            "acceptance_rate": (sum(1 for j in done if j.accepted) / len(done)) if done
                               else None,
            "stopped_early": self._stop_reason is not None,
            "stop_reason": self._stop_reason or "",
            "latency_p50_ms": latencies[len(latencies) // 2] if latencies else None,
            "latency_max_ms": latencies[-1] if latencies else None,
            "jobs": [j.as_dict() for j in jobs],
        }


def escalating_sizes(target: int = 500) -> list[int]:
    """
    20 -> 50 -> 100 -> 250 -> 500.

    Scale in steps and stop when acceptance degrades, rather than committing
    the whole budget to a run whose first twenty items would have told you
    not to.
    """
    ladder = [20, 50, 100, 250, 500]
    return [n for n in ladder if n <= target] or [target]
