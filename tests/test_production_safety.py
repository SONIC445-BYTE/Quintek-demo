"""
The protections that must hold while no model is qualified.

Quintek's authoritative state is NO MODEL QUALIFIED / INSUFFICIENT EVIDENCE.
That is a safe state only if the system actually refuses, so each refusal is
asserted here rather than assumed. These are the paths a well-meaning change
would most easily weaken: they all look like "make it work".
"""

from __future__ import annotations

import json

import pytest

from student.ai import AIEngine, NoEligibleModel
from student.api import StudentAPI
from student.db import Database


@pytest.fixture
def api(tmp_path):
    return StudentAPI(Database(tmp_path / "p.db"))


# ---------------------------------------------------------------------------
# No qualified model => no production generation
# ---------------------------------------------------------------------------

def test_resolve_refuses_when_nothing_is_promoted_routed_or_overridden(tmp_path):
    engine = AIEngine(Database(tmp_path / "a.db"))
    with pytest.raises(NoEligibleModel) as caught:
        engine.resolve("QUESTION_GENERATION")
    message = str(caught.value)
    assert "nothing is promoted" in message
    assert "no candidate is benchmark-eligible" in message


def test_the_development_override_is_labelled_as_such_not_as_qualification(tmp_path):
    """
    A dev override must be distinguishable from a real qualification in the
    provenance, or an operator reading an execution record cannot tell whether
    a learner was served by an evaluated model or a scripted stand-in.
    """
    engine = AIEngine(Database(tmp_path / "a.db"), development_candidate="dev-x")
    candidate, source = engine.resolve("QUESTION_GENERATION")
    assert candidate == "dev-x"
    assert source == "development_override"
    assert source not in ("promoted", "routed"), (
        "a development override must never be recorded as a qualification")


def test_the_override_is_off_unless_explicitly_configured(tmp_path, monkeypatch):
    """It must take a deliberate act to enable, never a default."""
    monkeypatch.delenv("QUINTEK_DEV_CANDIDATE", raising=False)
    engine = AIEngine(Database(tmp_path / "a.db"))
    assert engine.development_candidate is None
    with pytest.raises(NoEligibleModel):
        engine.resolve("QUESTION_GENERATION")


# ---------------------------------------------------------------------------
# The gate cannot be reached sideways
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("outcome", [
    "INCOMPLETE", "INSUFFICIENT_EVIDENCE", "UNEVALUABLE",
    "NOT_VALID_FOR_PRODUCTION_PASS", "INVALID_RUN", "FAIL",
])
def test_an_unfinished_or_failed_run_cannot_promote(tmp_path, outcome):
    """
    Quintek's Phase 0 ended INCOMPLETE. The promotion gate is what stops that
    from becoming a deployed model.

    CONDITIONAL is deliberately NOT in this list. `benchmark/gates.py` defines
    it as "misses by no more than the registered tolerance (non-safety only)"
    and `ELIGIBLE_OUTCOMES = {"PASS", "CONDITIONAL"}` -- a documented,
    tolerance-bounded pass, not an unfinished run. Asserting it blocked would
    encode a preference over the specification.
    """
    from benchmark.analytics import RunArchive
    from benchmark.promotion_api import PromotionAPI
    from benchmark.registry import Registry

    class _AI:
        def __init__(self, registry): self.registry = registry

    registry = Registry(tmp_path / "r.json")
    api = PromotionAPI(_AI(registry), archive=RunArchive(tmp_path / "runs"),
                       registry=registry)
    reason = api._blocking_reason({
        "candidate_id": "c-1", "integrity_satisfied": True,
        "scores_withheld": False, "outcome": outcome})
    assert reason is not None, f"{outcome} was allowed to promote"


def test_unknown_capability_is_not_treated_as_supported(tmp_path):
    """
    UNKNOWN must never widen into PASS. The registry refuses a role
    requirement it has not observed rather than assuming it.
    """
    from benchmark.discovery import DynamicModelRegistry, Observation
    reg = DynamicModelRegistry(tmp_path / "m.json")
    reg.reconcile("nvidia", [Observation(provider="nvidia", model_id="m-1")])
    eligible = reg.eligible(required_capabilities=("structured_output",),
                            require_observed=True)
    assert [e for e in eligible if getattr(e, "key", "") == "nvidia:m-1"] == [], (
        "a model with no observed capability was treated as eligible")


def test_the_holdout_is_untouched():
    """0 scoring runs of MAX_USES 5, and this test is how that stays true."""
    from validator.holdout import MAX_USES, read_ledger
    scores = [r for r in read_ledger() if r.kind == "score"]
    assert len(scores) == 0, f"holdout has been scored {len(scores)} time(s)"
    assert MAX_USES == 5


# ---------------------------------------------------------------------------
# Failures are failures, not results
# ---------------------------------------------------------------------------

def test_a_provider_failure_is_not_counted_against_model_quality():
    from benchmark.provider_status import ProviderStatus, classify, policy_for
    for error, expected in [
        ("HTTP 410: end of life", ProviderStatus.MODEL_RETIRED),
        ("[Errno 111] Connection refused", ProviderStatus.UNREACHED),
        ("HTTP 402 payment required", ProviderStatus.BILLING_BLOCKED),
    ]:
        status = classify(error=error)
        assert status == expected
        assert policy_for(status).counts_against_quality is False, (
            f"{status} was allowed to count against the model")


# ---------------------------------------------------------------------------
# Nothing secret reaches the client
# ---------------------------------------------------------------------------

def test_no_endpoint_returns_a_credential(api, monkeypatch):
    """
    The whole unauthenticated surface, scanned for the live key's shape. A
    credential in a response body is a credential on a learner's phone.
    """
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-must-never-be-returned-0123456789")
    for path in ("/health", "/capabilities", "/demos"):
        _status, body = api.handle("GET", path, {}, None, None)
        assert "nvapi-" not in json.dumps(body, default=str), f"{path} leaked a credential"


def test_a_freeze_manifest_refuses_to_carry_a_credential():
    from validator import freeze as freeze_mod
    for key in ("api_key", "Authorization", "SECRET"):
        with pytest.raises(freeze_mod.FreezeViolation, match="refusing to freeze"):
            freeze_mod.build(corpus="c", corpus_hash="h",
                             models=[{"role": "judge", key: "value"}],
                             experiments=[{"name": "1", "layers": "ABD"}])
