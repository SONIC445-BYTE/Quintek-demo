"""
The V1 critical path, end to end, with the failure routes that matter.

This is the section-11 audit as a test rather than a document: model selection
-> qualified-model enforcement -> generation -> provenance -> ledger -> admin
visibility, plus the three ways it is supposed to refuse.

WHAT THIS IS NOT
----------------
It is not evidence that any model is medically suitable, and it does not run
one. Every provider here is scripted. What it establishes is that the
ENFORCEMENT holds: that an unqualified, retired, unhealthy or over-budget
model cannot reach a learner, and that what did serve is attributable
afterwards.

The distinction matters because the enforcement is the part a passing
benchmark cannot demonstrate. A green qualification run tells you a model
scored well; only these paths tell you the system would have stopped it if it
had not.
"""

from __future__ import annotations

import pytest

from benchmark import analytics as an
from benchmark.analytics_api import AnalyticsAPI
from benchmark.discovery import DynamicModelRegistry, Observation
from benchmark.health import BreakerPolicy, HealthRegistry
from benchmark.orchestration import CallLimiter, ExecutionLog, Orchestrator
from benchmark.promotion_api import PromotionAPI, PromotionError
from benchmark.provider_status import ProviderStatus
from benchmark.providers.base import GenerationResponse
from benchmark.providers.scripted import ScriptedProvider
from benchmark.registry import Registry, Status
from benchmark.tasks import TaskType

from test_router import _gate, _promote, _register, _write_run

RETIRED_410 = ("HTTP 410: The model has reached its end of life on "
               "2026-08-26T09:00:00Z and is no longer available.")


class FailingProvider:
    def __init__(self, error: str):
        self.error, self.calls = error, 0

    def generate(self, request):
        self.calls += 1
        return GenerationResponse(
            item_id=request.item_id, raw_output="", parsed=None, provider="scripted",
            model="scripted", model_version="1.0", latency_ms=10.0,
            input_tokens=0, output_tokens=0, error=self.error)


@pytest.fixture
def rig(tmp_path):
    """Registry + archive + logs + a discovery registry, all on tmp."""
    registry = Registry(tmp_path / "registry.json")
    runs_root = tmp_path / "runs"
    models = DynamicModelRegistry(tmp_path / "models.json")
    return {
        "registry": registry, "runs_root": runs_root,
        "archive": an.RunArchive(runs_root),
        "exec_log": ExecutionLog(tmp_path / "executions.jsonl"),
        "routing_log": an.RoutingLog(tmp_path / "routing.jsonl"),
        "models": models, "tmp": tmp_path,
    }


def seed_candidate(rig, model_id, *, score=0.95, outcome="PASS", promote=True):
    candidate = _register(rig["registry"], model_id, ["question_generation"])
    if promote:
        _promote(rig["registry"], candidate.candidate_id)
    _write_run(rig["runs_root"], f"run-{model_id}", candidate.candidate_id, outcome,
               {"E_generation": _gate("E_generation", "GATE-E-RUBRIC",
                                      "mean_rubric_score", outcome, estimate=score)})
    return candidate


def observe(rig, candidate, *, http_status=200, error=None):
    key = f"{candidate.provider}:{candidate.model_id}"
    if rig["models"].get(key) is None:
        rig["models"].reconcile(candidate.provider,
                                [Observation(provider=candidate.provider,
                                             model_id=candidate.model_id)])
    rig["models"].record_probe(key, http_status=http_status, error=error,
                               credential_ref="NVIDIA_API_KEY")
    return key


# ---------------------------------------------------------------------------
# The happy path, and what it leaves behind
# ---------------------------------------------------------------------------

def test_the_critical_path_serves_and_stays_attributable(rig):
    """
    Selection -> generation -> execution ledger -> admin visibility. The point
    of the assertions at the end is that "which model answered this" survives
    the call, because a transparency screen that cannot answer it is the one
    thing this product must not ship.
    """
    good = seed_candidate(rig, "model-good")
    observe(rig, good)

    orch = Orchestrator(rig["registry"], rig["archive"],
                        lambda c: ScriptedProvider(accuracy=1.0),
                        rig["exec_log"], rig["routing_log"],
                        health=HealthRegistry(), model_registry=rig["models"])
    response, record = orch.generate(TaskType.QUESTION_GENERATION, "make a question")

    assert response is not None and response.ok
    assert record.status == "ok"

    # Provenance, on the row that was written, not recomputed for the test.
    logged = rig["exec_log"].all()[-1]
    assert logged.candidate_id == good.candidate_id
    assert logged.provider == good.provider
    assert logged.model == good.model_id
    assert logged.model_version == good.model_version
    assert logged.routing_policy
    assert logged.timestamp

    # And the routing decision that produced it is separately recoverable, so
    # "why this model" and "what happened when it ran" are both answerable
    # from disk rather than one being inferred from the other.
    decisions = rig["routing_log"].all()
    assert len(decisions) == 1
    assert decisions[0].execution_id == record.execution_id
    assert decisions[0].selected_candidate == good.candidate_id
    assert decisions[0].benchmark_evidence


def test_admin_can_see_the_model_and_its_capability_provenance(rig):
    good = seed_candidate(rig, "model-good")
    observe(rig, good)
    rig["models"].save()

    api = AnalyticsAPI(rig["runs_root"], model_registry_path=rig["tmp"] / "models.json")
    status, body = api.handle("/ai/discovery", {})
    assert status == 200
    row = {r["key"]: r for r in body["models"]}[f"{good.provider}:{good.model_id}"]
    assert row["availability"] == "AVAILABLE"
    assert row["credential_ref"] == "NVIDIA_API_KEY"
    assert row["retired"] is False


# ---------------------------------------------------------------------------
# The three refusals
# ---------------------------------------------------------------------------

def test_an_unqualified_model_cannot_be_promoted(rig):
    """
    A run that did not pass is not evidence. This is the gate that stands
    between a benchmark number and a learner.
    """
    failed = seed_candidate(rig, "model-failed", outcome="FAIL", promote=False)
    observe(rig, failed)
    rig["models"].save()

    api = PromotionAPI(_FakeAI(rig["registry"]), archive=rig["archive"],
                       registry=rig["registry"], model_registry=rig["models"])
    reason = api._blocking_reason({
        "candidate_id": failed.candidate_id, "integrity_satisfied": True,
        "scores_withheld": False, "outcome": "FAIL"})
    assert reason is not None
    assert "only PASS" in reason or "outcome is FAIL" in reason


def test_a_retired_model_cannot_be_promoted_or_routed(rig):
    """
    Both halves. Promotion refuses so the deployment record cannot name a dead
    model; routing refuses so nothing calls it even if a record existed.
    """
    dead = seed_candidate(rig, "model-dead", score=0.99)
    alive = seed_candidate(rig, "model-alive", score=0.10)
    observe(rig, dead, http_status=410, error=RETIRED_410)
    observe(rig, alive)
    rig["models"].save()

    api = PromotionAPI(_FakeAI(rig["registry"]), archive=rig["archive"],
                       registry=rig["registry"], model_registry=rig["models"])
    reason = api._blocking_reason({
        "candidate_id": dead.candidate_id, "integrity_satisfied": True,
        "scores_withheld": False, "outcome": "PASS"})
    assert "withdrawn by the provider" in reason

    called = []
    orch = Orchestrator(rig["registry"], rig["archive"],
                        lambda c: (called.append(c.candidate_id)
                                   or ScriptedProvider(accuracy=1.0)),
                        rig["exec_log"], rig["routing_log"],
                        health=HealthRegistry(), model_registry=rig["models"])
    _response, record = orch.generate(TaskType.QUESTION_GENERATION, "q")
    assert record.candidate_id == alive.candidate_id
    assert dead.candidate_id not in called


def test_a_provider_outage_falls_over_and_is_recorded_as_an_outage(rig):
    """
    Not silently, and not as a quality signal. The failure is classified, the
    breaker learns it, and the record carries the class.
    """
    sick = seed_candidate(rig, "model-sick", score=0.99)
    well = seed_candidate(rig, "model-well", score=0.10)
    observe(rig, sick)
    observe(rig, well)
    providers = {sick.candidate_id: FailingProvider("HTTP 500: internal server error")}
    health = HealthRegistry(policy=BreakerPolicy(failure_threshold=3))

    orch = Orchestrator(
        rig["registry"], rig["archive"],
        lambda c: providers.get(c.candidate_id) or ScriptedProvider(accuracy=1.0),
        rig["exec_log"], rig["routing_log"], health=health,
        model_registry=rig["models"])
    response, record = orch.generate(TaskType.QUESTION_GENERATION, "q")

    assert response is not None and response.ok          # the learner still got one
    assert record.candidate_id == well.candidate_id
    assert record.fallback is True
    failed_row = [r for r in rig["exec_log"].all()
                  if r.candidate_id == sick.candidate_id][0]
    assert failed_row.status == "error"
    assert failed_row.failure_status == ProviderStatus.UNKNOWN_ERROR


def test_the_budget_stops_the_run_rather_than_being_retried_around(rig):
    """
    A budget stop is a stop. The orchestrator returns without an answer and
    records why, instead of trying a cheaper model to get under the ceiling --
    which would make the ceiling a suggestion.
    """
    good = seed_candidate(rig, "model-good")
    observe(rig, good)
    limiter = CallLimiter(max_calls=0)

    orch = Orchestrator(rig["registry"], rig["archive"],
                        lambda c: ScriptedProvider(accuracy=1.0),
                        rig["exec_log"], rig["routing_log"],
                        call_limiter=limiter, health=HealthRegistry(),
                        model_registry=rig["models"])
    response, record = orch.generate(TaskType.QUESTION_GENERATION, "q")

    assert response is None
    assert record.status == "error"
    assert "budget" in (record.error or "").lower()
    assert limiter.calls_made == 0


def test_an_unhealthy_model_is_refused_before_it_is_called_again(rig):
    """
    The breaker is in the path, not beside it. Once open, the next request
    does not reach the model at all.
    """
    sick = seed_candidate(rig, "model-sick", score=0.99)
    seed_candidate(rig, "model-well", score=0.10)
    observe(rig, sick)
    failing = FailingProvider(RETIRED_410)
    health = HealthRegistry()

    orch = Orchestrator(
        rig["registry"], rig["archive"],
        lambda c: failing if c.candidate_id == sick.candidate_id
        else ScriptedProvider(accuracy=1.0),
        rig["exec_log"], rig["routing_log"], health=health,
        model_registry=rig["models"])
    orch.generate(TaskType.QUESTION_GENERATION, "q")
    assert failing.calls == 1
    assert health.allows(f"{sick.provider}:{sick.model_id}") is False

    orch.generate(TaskType.QUESTION_GENERATION, "q")
    assert failing.calls == 1        # not called a second time


# ---------------------------------------------------------------------------
# Promotion cannot skip the lifecycle
# ---------------------------------------------------------------------------

def test_registered_cannot_jump_to_production(rig):
    """
    The state machine, not a convention. REGISTERED reaches PRODUCTION only
    through BENCHMARK_REQUIRED -> EVALUATING -> ELIGIBLE.
    """
    candidate = _register(rig["registry"], "model-new", ["question_generation"])
    with pytest.raises(ValueError, match="illegal transition"):
        rig["registry"].transition(candidate.candidate_id, Status.PRODUCTION)
    with pytest.raises(ValueError, match="illegal transition"):
        rig["registry"].transition(candidate.candidate_id, Status.ELIGIBLE)
    assert rig["registry"].eligible_candidates() == []


class _FakeAI:
    """Only what PromotionAPI touches."""

    def __init__(self, registry):
        self.registry = registry
