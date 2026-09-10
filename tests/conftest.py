"""
Test-wide isolation for anything the suite would otherwise write to a shared,
real file.

WHY THIS EXISTS
---------------
`student/ai.py` appends an execution record per model call, defaulting to
`executions.jsonl` at the repository root. That is the same file
`EvalAPI._latency_for` takes a median latency from, and that median is served
to a learner through `/ai/eval`.

Nothing separated the two. Running the suite appended records naming a real
provider -- `openrouter`, which is genuinely registered -- from a scripted test
double, every one with a latency of exactly 12.0 ms. 229 of them accumulated,
42 during the very session that found them. Anything reading that file for
provider health was reading test fixtures as measurements.

The redirect below is the actual fix: the suite cannot reach the real log
because it is never told where it is. `test_execution_log_isolation.py` then
proves the redirect is in force rather than assuming it.
"""

from __future__ import annotations

import os

import pytest

# The path the suite must never touch, resolved once at import.
REAL_EXECUTION_LOG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "executions.jsonl")


@pytest.fixture(scope="session", autouse=True)
def _isolate_execution_log(tmp_path_factory):
    """
    Point every execution-log write at a throwaway file, and label the records.

    Session-scoped and autouse: a per-test opt-in is a per-test opportunity to
    forget, and forgetting is what produced 229 fabricated latency figures in
    the shared log.

    Both halves matter. The redirect stops the suite reaching the real file.
    The origin label means that if some future path writes to a real log
    anyway, the record still cannot be mistaken for a measurement.
    """
    real = os.path.exists(REAL_EXECUTION_LOG)
    before = os.path.getsize(REAL_EXECUTION_LOG) if real else None

    sandbox = tmp_path_factory.mktemp("execution-log") / "executions.jsonl"
    previous = {
        "QUINTEK_EXECUTION_LOG": os.environ.get("QUINTEK_EXECUTION_LOG"),
        "QUINTEK_EXECUTION_ORIGIN": os.environ.get("QUINTEK_EXECUTION_ORIGIN"),
    }
    os.environ["QUINTEK_EXECUTION_LOG"] = str(sandbox)
    os.environ["QUINTEK_EXECUTION_ORIGIN"] = "test-harness"
    try:
        yield sandbox
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        # Checked at session teardown rather than in a test, so it covers the
        # WHOLE run regardless of ordering. A test asserting this could only
        # ever see writes that happened before it ran.
        if real:
            after = os.path.getsize(REAL_EXECUTION_LOG)
            if after != before:
                raise AssertionError(
                    f"the real execution log at {REAL_EXECUTION_LOG} grew from {before} "
                    f"to {after} bytes during this test run. Something wrote to the "
                    "shared log despite the redirect -- find it before trusting any "
                    "provider-health figure computed from that file.")
