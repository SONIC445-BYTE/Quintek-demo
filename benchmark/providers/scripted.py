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

    # Shapes the app asks for, keyed by the JSON key its prompt names.
    SHAPES = ("questions", "concepts", "checks")

    def _shaped_reply(self, request: GenerationRequest):
        prompt = request.prompt or ""
        for key in self.SHAPES:
            if f'"{key}"' not in prompt:
                continue
            return getattr(self, f"_{key}")(prompt)
        return None

    @staticmethod
    def _passage_sentences(prompt: str) -> list[str]:
        """
        Sentences from the prompt, longest first.

        Grounding the reply in the prompt's own text is what makes a scripted
        run exercise the real validation path: a reply about something absent
        from the passages would be rejected as ungrounded, and the harness
        would be testing the rejection path every time by accident.
        """
        import re

        # Prompts carry their own JSON schema by example. A "sentence" holding
        # braces or quoted keys is that schema, not passage text, and building
        # a question out of it produces a stem about the instructions.
        body = prompt
        for marker in ("SOURCE PASSAGES", "PASSAGES:", "SOURCE:"):
            if marker in body:
                body = body.split(marker, 1)[1]
                break

        parts = [p.strip(" :\t") for p in re.split(r"(?<=[.!?])\s+", body)]
        usable = [p for p in parts
                  if 40 <= len(p) <= 300
                  and " " in p
                  and not any(ch in p for ch in "{}[]")
                  and '"' not in p]
        return sorted(usable, key=len, reverse=True)

    def _questions(self, prompt: str) -> dict:
        sentences = self._passage_sentences(prompt) or ["A scripted placeholder stem."]
        wanted = 1
        import re

        match = re.search(r"(\d+)\s+(?:multiple[- ]choice\s+)?questions?", prompt, re.I)
        if match:
            wanted = max(1, min(20, int(match.group(1))))

        questions = []
        for index in range(min(wanted, len(sentences))):
            fact = sentences[index]
            questions.append({
                "stem": f"According to the source, which statement is correct? ({fact})",
                "options": [fact, "None of the above applies.",
                            "The opposite of the statement above.",
                            "The source does not address this."],
                "correct_index": 0,
                "rationale": f"The passage states: {fact}",
                "concepts_tested": [],
                "passage": 1,
            })
        return {"questions": questions}

    def _checks(self, prompt: str) -> dict:
        """
        A validation reply.

        Every check passes, which is the RIGHT default for a plumbing double
        and the wrong thing to read anything into: this is not a judgement, and
        a scripted validation must never be reported as evidence that Quintek
        can recognise a bad question. The adversarial battery exists for that,
        and the real validator has not passed it.
        """
        import re

        # The prompt lists every check it wants judged, one per line, as
        # `- "check_name": question`. Answering only the ones shown `: true` in
        # the reply EXAMPLE leaves the rest defaulting to false, and the double
        # then flags every question -- which looks like a validator doing its
        # job and is really a double answering the wrong question.
        names = re.findall(r'-\s*"([a-z_]+)"\s*:', prompt)
        if not names:
            names = re.findall(r'"([a-z_]+)":\s*true', prompt)
        checks = {name: True for name in names} or {"factually_correct": True}
        return {"checks": checks, "issues": [], "verdict": "approved"}

    def _concepts(self, prompt: str) -> dict:
        sentences = self._passage_sentences(prompt)[:3]
        return {"concepts": [
            {"name": s.split(" is ")[0][:60].strip(" .") or f"Concept {i + 1}",
             "description": s}
            for i, s in enumerate(sentences)]}

    def _call(self, request: GenerationRequest, timeout_seconds: float):
        if request.item_id in self.error_items:
            raise ConnectionError("simulated provider failure")
        remaining = self.fail_attempts.get(request.item_id, 0)
        if remaining > 0:
            seen = self._attempt_counts.get(request.item_id, 0)
            self._attempt_counts[request.item_id] = seen + 1
            if seen < remaining:
                raise TimeoutError(f"simulated timeout after {timeout_seconds}s")
        # The app asks for several different JSON shapes, and each prompt
        # states the one it wants by example. Sniffing that is enough for a
        # test double, and it is what makes the whole app runnable end to end
        # with no key and no spend -- which is this class's entire reason to
        # exist. It is still not a model: the content below is assembled from
        # the prompt, never reasoned about, and `name` says so on every record
        # it appears in.
        shaped = self._shaped_reply(request)
        if shaped is not None:
            raw = json.dumps(shaped)
            return raw, shaped, len(request.prompt) // 4, len(raw) // 4

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
