"""
Tests for batch execution.

Written against a specific failure: 500 sequential requests into an endpoint
with 100-second stalls, nothing watching, an hour spent measuring the host
rather than the model. Each test here is one of the things that would have
prevented it.
"""

from __future__ import annotations

import time

import pytest

from benchmark.batch import (ABORTED, DONE, FAILED, SKIPPED, BatchRunner, Job, StopPolicy,
                             escalating_sizes)
from benchmark.health import BreakerPolicy, HealthRegistry, OPEN


def ok(job):
    return {"raw_output": "generated", "accepted": True}


def make_jobs(n, candidate="fast", task_type="MCQ"):
    return [Job(task_id=f"t{i}", task_type=task_type, candidate=candidate)
            for i in range(n)]


def test_all_jobs_run_and_are_recorded():
    result = BatchRunner(ok, workers=4).run(make_jobs(12))
    assert result["completed"] == 12
    assert result["acceptance_rate"] == 1.0
    assert len(result["jobs"]) == 12
    assert all(j["started_at"] and j["finished_at"] for j in result["jobs"])


def test_a_slow_provider_does_not_freeze_the_experiment():
    """The whole point: other workers keep going."""
    def execute(job):
        if job.candidate == "slow":
            raise TimeoutError("timeout after 180s")
        return ok(job)

    health = HealthRegistry(policy=BreakerPolicy(failure_threshold=2, timeout_weight=2))
    jobs = [Job(task_id=f"t{i}", candidate="slow" if i % 5 == 0 else "fast")
            for i in range(20)]
    result = BatchRunner(execute, workers=4, health=health,
                         candidate_for=lambda j: j.candidate,
                         stop_policy=StopPolicy(max_consecutive_failures=99)).run(jobs)

    assert result["completed"] == 16
    assert health.breaker("slow").state == OPEN
    assert health.breaker("fast").state != OPEN


def test_the_breaker_stops_a_dead_endpoint_absorbing_the_queue():
    def always_times_out(job):
        raise TimeoutError("timeout after 180s")

    health = HealthRegistry(policy=BreakerPolicy(failure_threshold=2, timeout_weight=2,
                                                 cooldown_seconds=600))
    result = BatchRunner(always_times_out, workers=1, health=health,
                         candidate_for=lambda j: "dead",
                         stop_policy=StopPolicy(max_consecutive_failures=99),
                         max_attempts=1).run(make_jobs(20))

    # Most jobs are skipped by the open breaker rather than each paying a timeout.
    assert result["by_status"].get(SKIPPED, 0) >= 15
    assert result["by_status"].get(FAILED, 0) <= 5


def test_a_skipped_job_says_the_circuit_was_open():
    health = HealthRegistry(policy=BreakerPolicy(failure_threshold=1, cooldown_seconds=600))
    health.observe("dead", success=False, error="timeout after 180s")
    result = BatchRunner(ok, workers=1, health=health,
                         candidate_for=lambda j: "dead").run(make_jobs(3))
    skipped = [j for j in result["jobs"] if j["status"] == SKIPPED]
    assert skipped and "circuit open" in skipped[0]["error"]


def test_consecutive_failures_stop_the_run():
    """Ten failures in a row is measuring the endpoint, not the model."""
    def always_fails(job):
        raise RuntimeError("500 from provider")

    result = BatchRunner(always_fails, workers=1, max_attempts=1,
                         stop_policy=StopPolicy(max_consecutive_failures=5)
                         ).run(make_jobs(50))
    assert result["stopped_early"] is True
    assert "consecutive failures" in result["stop_reason"]
    assert result["by_status"].get(ABORTED, 0) > 0


def test_a_collapsing_acceptance_rate_stops_the_run():
    def rejected(job):
        return {"raw_output": "x", "accepted": False}

    result = BatchRunner(rejected, workers=1,
                         stop_policy=StopPolicy(min_acceptance_rate=0.5,
                                                min_before_judging=10)).run(make_jobs(100))
    assert result["stopped_early"] is True
    assert "below the 50% floor" in result["stop_reason"]


def test_acceptance_is_not_judged_before_there_is_enough_of_it():
    """A rate over three items is noise; stopping on noise wastes the run."""
    policy = StopPolicy(min_acceptance_rate=0.9, min_before_judging=20)
    assert policy.should_stop(completed=3, accepted=0, consecutive_failures=0) is None
    assert policy.should_stop(completed=20, accepted=0, consecutive_failures=0) is not None


def test_a_run_resumes_instead_of_restarting():
    jobs = make_jobs(10)
    result = BatchRunner(ok, workers=2).run(jobs, skip_done={"t0", "t1", "t2"})
    assert result["by_status"].get(SKIPPED) == 3
    assert result["completed"] == 7
    skipped = [j for j in result["jobs"] if j["status"] == SKIPPED]
    assert "already completed" in skipped[0]["error"]


def test_a_failing_job_is_retried_up_to_the_limit():
    attempts = {"n": 0}

    def flaky(job):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise RuntimeError("transient")
        return ok(job)

    result = BatchRunner(flaky, workers=1, max_attempts=3).run(make_jobs(1))
    assert result["completed"] == 1
    assert result["jobs"][0]["attempt"] == 2


def test_every_job_carries_its_own_latency():
    def slow_ish(job):
        time.sleep(0.02)
        return ok(job)

    result = BatchRunner(slow_ish, workers=2).run(make_jobs(4))
    assert all(j["latency_ms"] > 0 for j in result["jobs"])
    assert result["latency_p50_ms"] is not None


def test_an_observer_that_raises_does_not_take_down_the_run():
    """Observability must never be the thing that kills the batch."""
    def bad_observer(job):
        raise RuntimeError("logging exploded")

    result = BatchRunner(ok, workers=2, on_job=bad_observer).run(make_jobs(5))
    assert result["completed"] == 5


def test_progress_is_visible_while_the_run_is_still_going():
    seen = []
    BatchRunner(ok, workers=1, on_job=lambda j: seen.append(j.task_id)).run(make_jobs(5))
    assert len(seen) == 5


def test_the_escalation_ladder_scales_in_steps():
    assert escalating_sizes(500) == [20, 50, 100, 250, 500]
    assert escalating_sizes(100) == [20, 50, 100]
    assert escalating_sizes(10) == [10]
