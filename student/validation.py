"""
Independent question validation.

A separate job, on a **different model configuration** from the one that wrote
the question. That mirrors the benchmark's judge-independence rule for the same
reason it exists there: a model asked to check its own work grades its own
reasoning, and the failure it is least able to see is the one it just made.

Two rules this module will not bend:

  * **The validator never sees the generator's reasoning.** It gets the stem,
    the options, the keyed answer and the source passage -- the same material a
    human reviewer would get. Handing over the generator's rationale invites
    the validator to check the argument instead of the claim.

  * **Flags, never silently discards.** A failed check marks the question
    `flagged` with the reasons attached. Deleting it would destroy the evidence
    that the generator produces this kind of error, which is exactly what the
    admin failure-analysis screens exist to show.
"""

from __future__ import annotations

import json

from .ai import AICallFailed, AIEngine, extract_json
from .db import Database, now_iso

VALIDATION_PROMPT_VERSION = "val-v1"

# The checks from docs/QUINTEK_LOGIC.md section 4.3.
CHECKS = [
    ("factually_correct", "Is the keyed answer factually correct?"),
    ("grounded_in_source", "Is the question answerable from the supplied passage alone?"),
    ("key_is_right", "Is the keyed option genuinely the single best answer?"),
    ("distractors_plausible", "Are the wrong options plausible but clearly wrong?"),
    ("unambiguous", "Is exactly one option defensible?"),
    ("concept_aligned", "Does it test the concepts it claims to test?"),
    ("no_unsupported_claims", "Does it avoid asserting anything the passage does not support?"),
    ("pg_level", "Is it at postgraduate level rather than recall-trivial?"),
]


class ValidationSkipped(RuntimeError):
    """Validation could not run independently, so it did not run at all."""


class QuestionValidator:
    def __init__(self, db: Database, ai: AIEngine):
        self.db = db
        self.ai = ai

    def _independent_candidate(self, generator_candidate: str) -> str | None:
        """
        Pick a validating configuration that is not the generating one.

        Returns None when the only available model is the generator. That is a
        refusal, not a fallback: validating with the same configuration would
        produce an approval that means nothing, and an approval that means
        nothing is worse than no approval, because it is indistinguishable from
        a real one downstream.
        """
        try:
            candidate, _ = self.ai.resolve("QUESTION_VALIDATION")
        except Exception:
            return None
        return None if candidate == generator_candidate else candidate

    def build_prompt(self, question: dict, passage: str) -> str:
        options = json.loads(question["options_json"])
        lettered = "\n".join(f"{chr(65 + i)}. {o}" for i, o in enumerate(options))
        checks = "\n".join(f'- "{key}": {text}' for key, text in CHECKS)
        return (
            "Review this examination question. You did not write it, and you are not "
            "being asked to improve it -- only to judge it.\n\n"
            f"SOURCE PASSAGE:\n{passage}\n\n"
            f"STEM:\n{question['stem']}\n\nOPTIONS:\n{lettered}\n\n"
            f"KEYED ANSWER: {chr(65 + question['correct_index'])}\n\n"
            f"Judge each check as true or false:\n{checks}\n\n"
            "Reply with ONLY a JSON object:\n"
            '{"checks": {"factually_correct": true, ...}, '
            '"issues": ["short description of each problem"], '
            '"verdict": "approved" | "flagged"}'
        )

    def validate(self, question_id: str) -> dict:
        row = self.db.query_one("SELECT * FROM questions WHERE id = ?", (question_id,))
        if row is None:
            raise ValueError(f"no such question: {question_id}")
        question = dict(row)

        validator = self._independent_candidate(question["generated_by_candidate_id"])
        if validator is None:
            raise ValidationSkipped(
                "no configuration is available that differs from the one that generated this "
                "question; validating with the same model would produce an approval that "
                "means nothing")

        passage = ""
        if question["chunk_id"]:
            chunk = self.db.query_one("SELECT text FROM source_chunks WHERE id = ?",
                                      (question["chunk_id"],))
            passage = chunk["text"] if chunk else ""

        try:
            result = self.ai.call("QUESTION_VALIDATION", self.build_prompt(question, passage),
                                  prompt_version=VALIDATION_PROMPT_VERSION,
                                  max_tokens=700, temperature=0.0)
        except AICallFailed as exc:
            # An unreachable validator leaves the question pending. It must not
            # be approved by default -- that is how unchecked questions reach a
            # learner.
            self._record(question_id, "pending", {"error": str(exc)}, "")
            raise

        payload = result.parsed or extract_json(result.text) or {}
        checks = {k: bool(payload.get("checks", {}).get(k, False)) for k, _ in CHECKS}
        issues = [str(i) for i in (payload.get("issues") or [])]

        # The verdict is derived from the checks, not taken from the model's own
        # summary line: a model that answers "false" to `key_is_right` and then
        # says "approved" must not be believed about the second part.
        failed = [k for k, ok in checks.items() if not ok]
        status = "approved" if not failed else "flagged"
        detail = {"checks": checks, "issues": issues, "failed_checks": failed,
                  "validator_candidate": validator, "validated_at": now_iso()}
        self._record(question_id, status, detail, validator)
        return {"question_id": question_id, "status": status, **detail}

    def _record(self, question_id: str, status: str, detail: dict, validator: str) -> None:
        self.db.execute(
            "UPDATE questions SET validation_status = ?, validation_json = ?,"
            " validated_by_candidate_id = ? WHERE id = ?",
            (status, json.dumps(detail), validator, question_id))

    def validate_pending(self, notebook_id: str | None = None, limit: int = 50) -> dict:
        sql = "SELECT id FROM questions WHERE validation_status = 'pending'"
        params: tuple = ()
        if notebook_id:
            sql += " AND primary_notebook_id = ?"
            params = (notebook_id,)
        sql += " LIMIT ?"
        rows = self.db.query(sql, (*params, limit))

        summary = {"approved": 0, "flagged": 0, "skipped": 0, "failed": 0}
        for row in rows:
            try:
                summary[self.validate(row["id"])["status"]] += 1
            except ValidationSkipped:
                summary["skipped"] += 1
            except Exception:
                summary["failed"] += 1
        return summary
