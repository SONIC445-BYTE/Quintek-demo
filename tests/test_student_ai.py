"""
Phase 4: AI orchestration and the benchmark -> production gate.

The gate is the point. A model serves a learner only because a benchmark run
said it could, and this file asserts that the code enforces that rather than
documenting it.
"""

from __future__ import annotations

import pytest

from student.ai import AICallFailed, AIEngine, NoEligibleModel, extract_json
from student.db import Database


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "q.db")


class _Provider:
    """Scripted provider. Nothing here reaches a network."""
    name, model, model_version = "scripted", "test-model", "1.0"

    def __init__(self, reply="{\"ok\": true}", ok=True):
        self.reply, self.ok, self.calls = reply, ok, 0

    def generate(self, request):
        from benchmark.providers.base import GenerationResponse
        self.calls += 1
        return GenerationResponse(
            item_id=request.item_id, raw_output=self.reply if self.ok else "",
            parsed=extract_json(self.reply) if self.ok else None,
            provider=self.name, model=self.model, model_version=self.model_version,
            latency_ms=12.0, input_tokens=10, output_tokens=5,
            error=None if self.ok else "simulated failure", attempts=1 if self.ok else 3,
        )


# ---------------------------------------------------------------------------
# Resolution order
# ---------------------------------------------------------------------------

def test_with_nothing_configured_the_engine_refuses_rather_than_guessing(db):
    engine = AIEngine(db)
    with pytest.raises(NoEligibleModel, match="nothing is promoted"):
        engine.resolve("QUESTION_GENERATION")


def test_a_development_candidate_is_used_only_when_explicitly_configured(db):
    assert AIEngine(db, development_candidate="cand-dev").resolve("QUESTION_GENERATION") == (
        "cand-dev", "development_override")
    # Absent config, still a refusal -- never "just pick a registered one".
    with pytest.raises(NoEligibleModel):
        AIEngine(db).resolve("QUESTION_GENERATION")


def test_a_promotion_outranks_the_development_override(db):
    engine = AIEngine(db, development_candidate="cand-dev")
    engine.promote("QUESTION_GENERATION", "cand-good", "run-1", outcome="PASS")
    assert engine.resolve("QUESTION_GENERATION") == ("cand-good", "promoted")


def test_promotion_is_per_task(db):
    engine = AIEngine(db, development_candidate="cand-dev")
    engine.promote("QUESTION_GENERATION", "cand-a", "run-1", outcome="PASS")
    assert engine.resolve("QUESTION_GENERATION")[0] == "cand-a"
    assert engine.resolve("CONCEPT_EXTRACTION") == ("cand-dev", "development_override")


# ---------------------------------------------------------------------------
# The promotion gate
# ---------------------------------------------------------------------------

def test_a_failing_run_cannot_be_promoted(db):
    engine = AIEngine(db)
    for outcome in ["FAIL", "INVALID_RUN", "UNEVALUABLE", "INCOMPLETE",
                    "NOT_VALID_FOR_PRODUCTION_PASS"]:
        with pytest.raises(ValueError, match="cannot promote"):
            engine.promote("QUESTION_GENERATION", "cand-x", "run-1", outcome=outcome)
    assert engine.deployment_history() == []


def test_a_conditional_run_needs_a_named_human_and_a_reason(db):
    engine = AIEngine(db)
    with pytest.raises(ValueError, match="named sign-off"):
        engine.promote("QUESTION_GENERATION", "cand-x", "run-1", outcome="CONDITIONAL")
    with pytest.raises(ValueError, match="named sign-off"):
        engine.promote("QUESTION_GENERATION", "cand-x", "run-1", outcome="CONDITIONAL",
                       signoff_name="Dr Who")          # no rationale
    engine.promote("QUESTION_GENERATION", "cand-x", "run-1", outcome="CONDITIONAL",
                   signoff_name="Dr Who", signoff_rationale="D-F1 short by 0.8%, retest booked")
    assert engine.resolve("QUESTION_GENERATION")[0] == "cand-x"


def test_a_candidate_cannot_be_promoted_on_another_candidates_run(db):
    engine = AIEngine(db)
    with pytest.raises(ValueError, match="another's evidence"):
        engine.promote("QUESTION_GENERATION", "cand-a", "run-1", outcome="PASS",
                       run_candidate_id="cand-b")


def test_promoting_deactivates_the_previous_deployment_without_deleting_it(db):
    """'What was serving this task in March' has to stay answerable."""
    engine = AIEngine(db)
    engine.promote("QUESTION_GENERATION", "cand-old", "run-1", outcome="PASS")
    engine.promote("QUESTION_GENERATION", "cand-new", "run-2", outcome="PASS")

    history = engine.deployment_history("QUESTION_GENERATION")
    assert len(history) == 2
    active = [h for h in history if h["deactivated_at"] is None]
    assert len(active) == 1 and active[0]["candidate_id"] == "cand-new"
    assert any(h["candidate_id"] == "cand-old" and h["deactivated_at"] for h in history)


# ---------------------------------------------------------------------------
# Calling
# ---------------------------------------------------------------------------

def test_a_successful_call_reports_which_path_selected_the_model(db):
    provider = _Provider('{"answer": "B"}')
    engine = AIEngine(db, provider_factory=lambda c: provider,
                      development_candidate="cand-dev")
    result = engine.call("QUESTION_GENERATION", "prompt")
    assert result.parsed == {"answer": "B"}
    assert result.source == "development_override"
    assert provider.calls == 1

    engine.promote("QUESTION_GENERATION", "cand-good", "run-1", outcome="PASS")
    assert engine.call("QUESTION_GENERATION", "prompt").source == "promoted"


def test_a_failed_call_raises_rather_than_returning_empty_text(db):
    engine = AIEngine(db, provider_factory=lambda c: _Provider(ok=False),
                      development_candidate="cand-dev")
    with pytest.raises(AICallFailed, match="failed after 3 attempt"):
        engine.call("QUESTION_GENERATION", "prompt")


def test_calling_without_a_provider_factory_refuses(db):
    engine = AIEngine(db, development_candidate="cand-dev")
    with pytest.raises(NoEligibleModel, match="provider factory"):
        engine.call("QUESTION_GENERATION", "prompt")


# ---------------------------------------------------------------------------
# JSON extraction -- models wrap objects in prose no matter what the prompt says
# ---------------------------------------------------------------------------

def test_json_is_found_inside_prose_and_fences():
    assert extract_json('Sure! ```json\n{"a": 1}\n``` hope that helps') == {"a": 1}
    assert extract_json('{"nested": {"b": 2}} trailing') == {"nested": {"b": 2}}
    assert extract_json("no object here") is None
    assert extract_json("") is None


def test_malformed_json_is_unparseable_not_wrong():
    """An unparseable reply is a different failure from a wrong answer, and
    must not be silently coerced into one."""
    assert extract_json('{"a": }') is None
