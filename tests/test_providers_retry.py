"""
Provider retry/timeout handling.

Phase 0 of docs/MASTER_BUILD_PROMPT_V0_4.md requires "retry/timeout handling"
in the provider abstraction. These tests exercise the actual retry loop in
benchmark/providers/base.py rather than trusting that it exists.
"""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from benchmark.providers.base import GenerationRequest, RetryPolicy
from benchmark.providers.scripted import ScriptedProvider


def test_succeeds_first_try_records_one_attempt():
    p = ScriptedProvider(answers={"Q1": "A"})
    resp = p.generate(GenerationRequest(item_id="Q1", prompt="x"))
    assert resp.ok
    assert resp.attempts == 1


def test_retries_then_succeeds_within_budget():
    p = ScriptedProvider(answers={"Q1": "A"}, fail_attempts={"Q1": 2})
    p.retry_policy = RetryPolicy(max_retries=2, timeout_seconds=1.0)
    resp = p.generate(GenerationRequest(item_id="Q1", prompt="x"))
    assert resp.ok
    assert resp.attempts == 3  # 2 simulated failures + the successful attempt


def test_exhausting_retries_reports_error_not_a_silent_answer():
    """
    A provider error must never render as an empty/wrong answer that scores
    as a knowledge failure -- see providers/base.py docstring and
    scorers/deterministic.score_medical_qa, which excludes non-ok responses
    from the denominator entirely.
    """
    p = ScriptedProvider(answers={"Q1": "A"}, fail_attempts={"Q1": 5})
    p.retry_policy = RetryPolicy(max_retries=2, timeout_seconds=1.0)
    resp = p.generate(GenerationRequest(item_id="Q1", prompt="x"))
    assert not resp.ok
    assert resp.attempts == 3  # initial attempt + 2 retries, then give up
    assert resp.parsed is None
    assert "Timeout" in resp.error


def test_connection_error_is_retried_same_as_timeout():
    p = ScriptedProvider(error_items={"Q1"})
    p.retry_policy = RetryPolicy(max_retries=1, timeout_seconds=1.0)
    resp = p.generate(GenerationRequest(item_id="Q1", prompt="x"))
    assert not resp.ok
    assert resp.attempts == 2


def test_zero_retries_means_exactly_one_attempt():
    p = ScriptedProvider(error_items={"Q1"})
    p.retry_policy = RetryPolicy(max_retries=0, timeout_seconds=1.0)
    resp = p.generate(GenerationRequest(item_id="Q1", prompt="x"))
    assert not resp.ok
    assert resp.attempts == 1


def test_manifest_records_retry_policy():
    p = ScriptedProvider()
    p.retry_policy = RetryPolicy(max_retries=2, timeout_seconds=15.0)
    m = p.manifest()
    assert m["retry_policy"]["max_retries"] == 2
    assert m["retry_policy"]["timeout_seconds"] == 15.0
