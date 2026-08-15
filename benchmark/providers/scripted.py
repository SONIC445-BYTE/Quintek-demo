"""
Scripted provider for harness testing.

This exists so the harness can be exercised end-to-end without API keys, cost,
or a corpus. It returns pre-programmed answers, including deliberately wrong
ones, so acceptance tests can drive the system into FAIL, INVALID_RUN and every
other outcome state on demand.

It is NOT a model. It must never appear in a qualification run; the runner
records provider name in the manifest and the scorecard prints it.
"""

from __future__ import annotations

import json
import random

from .base import BaseProvider, GenerationRequest


class ScriptedProvider(BaseProvider):
    name = "scripted-test-harness"
    model = "scripted"
    model_version = "v0.4-test"
    model_family = "none"

    def __init__(self, answers: dict[str, str] | None = None,
                 accuracy: float = 1.0, seed: int = 7,
                 error_items: set[str] | None = None,
                 fail_attempts: dict[str, int] | None = None):
        self.answers = answers or {}
        self.accuracy = accuracy
        self.rng = random.Random(seed)
        self.error_items = error_items or set()
        # item_id -> number of leading attempts to fail before succeeding,
        # so tests can exercise the retry loop deterministically.
        self.fail_attempts = dict(fail_attempts or {})
        self._attempt_counts: dict[str, int] = {}

    def _call(self, request: GenerationRequest, timeout_seconds: float):
        if request.item_id in self.error_items:
            raise ConnectionError("simulated provider failure")
        remaining = self.fail_attempts.get(request.item_id, 0)
        if remaining > 0:
            seen = self._attempt_counts.get(request.item_id, 0)
            self._attempt_counts[request.item_id] = seen + 1
            if seen < remaining:
                raise TimeoutError(f"simulated timeout after {timeout_seconds}s")
        answer = self.answers.get(request.item_id)
        if answer is None:
            gold = request.metadata.get("gold_answer", "A")
            options = request.metadata.get("options", ["A", "B", "C", "D"])
            if self.rng.random() < self.accuracy:
                answer = gold
            else:
                wrong = [o for o in options if o != gold] or ["Z"]
                answer = self.rng.choice(wrong)
        payload = {"answer": answer, "confidence": 0.8, "brief_reason": "scripted"}
        raw = json.dumps(payload)
        return raw, payload, len(request.prompt) // 4, len(raw) // 4
