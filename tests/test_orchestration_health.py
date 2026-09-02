"""
Orchestration failover, and the state it must not forget between calls.

THE DEFECT THESE PIN
--------------------
`Orchestrator.generate` kept its `tried` set in a local variable and nowhere
else. Within one call a failed candidate was excluded correctly; the next
independent call started clean and selected it again. A model answering 410
was therefore re-selected on every single request, forever, while
`benchmark/batch.py` -- which does consult `health.allows()` -- stopped after
three.

Underneath it, a second one: `HealthRegistry.observe` sent every
circuit-opening status through the three-strike threshold. A 410, a 401 and a
402 are conclusive on the first observation, and the status policy already
said so (`open_circuit and not retryable`) -- nothing read it. Two wasted
calls per failure, every time the window reset.

Every provider here is scripted. No network, no credits.
"""

from __future__ import annotations

import pytest

from benchmark import analytics as an
from benchmark.discovery import Availability, DynamicModelRegistry, Observation
from benchmark.health import CLOSED, OPEN, BreakerPolicy, HealthRegistry
from benchmark.orchestration import ExecutionLog, Orchestrator
from benchmark.provider_status import ProviderStatus, policy_for
from benchmark.providers.base import GenerationResponse
from benchmark.providers.scripted import ScriptedProvider
from benchmark.registry import Registry
from benchmark.tasks import TaskType

from test_router import _gate, _promote, _register, _write_run

NVIDIA_410 = ("HTTP 410: The model has reached its end of life on "
              "2026-08-26T09:00:00Z and is no longer available.")


class FailingProvider:
    """
    A provider that always fails with one scripted error. Mirrors
    `ScriptedProvider`'s surface only as far as `Orchestrator` uses it.
    """

    def __init__(self, error: str, *, latency_ms: float = 12.0):
        self.error = error
        self.latency_ms = latency_ms
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        return GenerationResponse(
            item_id=request.item_id, raw_output="", parsed=None,
            provider="scripted", model="scripted", model_version="1.0",
            latency_ms=self.latency_ms, input_tokens=0, output_tokens=0,
            error=self.error)


@pytest.fixture
def env(tmp_path):
    registry = Registry(tmp_path / "registry.json")
    runs_root = tmp_path / "runs"
    return (registry, runs_root, an.RunArchive(runs_root),
            ExecutionLog(tmp_path / "executions.jsonl"),
            an.RoutingLog(tmp_path / "routing.jsonl"))


def seed(registry, runs_root, model_id, *, score=0.95):
    candidate = _register(registry, model_id, ["question_generation"])
    _promote(registry, candidate.candidate_id)
    _write_run(runs_root, f"run-{model_id}", candidate.candidate_id, "PASS",
               {"E_generation": _gate("E_generation", "GATE-E-RUBRIC",
                                      "mean_rubric_score", "PASS", estimate=score)})
    return candidate


def key_of(candidate) -> str:
    return f"{candidate.provider}:{candidate.model_id}"


# ---------------------------------------------------------------------------
# The breaker: conclusive vs. threshold
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", [
    ProviderStatus.MODEL_RETIRED, ProviderStatus.AUTH_FAILED,
    ProviderStatus.BILLING_BLOCKED, ProviderStatus.EGRESS_BLOCKED,
    ProviderStatus.MODEL_UNAVAILABLE,
])
def test_a_conclusive_failure_opens_the_circuit_on_the_first_observation(status):
    health = HealthRegistry(policy=BreakerPolicy(failure_threshold=3))
    health.observe("nvidia:m", success=False, error="x", status=status)
    assert health.allows("nvidia:m") is False, status
    assert health.breaker("nvidia:m").state == OPEN
    # And the policy is what decided it, not a list of statuses in the breaker.
    assert policy_for(status).retryable is False
    assert policy_for(status).open_circuit is True


@pytest.mark.parametrize("status", [ProviderStatus.TIMEOUT, ProviderStatus.UNKNOWN_ERROR])
def test_a_retryable_failure_still_needs_the_threshold(status):
    """
    One timeout is not evidence of anything. Opening on the first would take a
    working endpoint out of rotation for a blip.
    """
    health = HealthRegistry(policy=BreakerPolicy(failure_threshold=3))
    health.observe("nvidia:m", success=False, error="x", status=status)
    assert health.allows("nvidia:m") is True
    assert health.breaker("nvidia:m").state == CLOSED


def test_a_rate_limit_never_opens_the_circuit():
    """The provider is healthy; we are being greedy. Opening punishes it for us."""
    health = HealthRegistry(policy=BreakerPolicy(failure_threshold=1))
    for _ in range(5):
        health.observe("nvidia:m", success=False, error="429",
                       status=ProviderStatus.RATE_LIMITED)
    assert health.allows("nvidia:m") is True


def test_a_conclusive_failure_has_no_cooldown_that_reopens_it():
    health = HealthRegistry(policy=BreakerPolicy(cooldown_seconds=1))
    health.observe("nvidia:m", success=False, error=NVIDIA_410,
                   status=ProviderStatus.MODEL_RETIRED)
    assert health.breaker("nvidia:m").current_cooldown == float("inf")


# ---------------------------------------------------------------------------
# Orchestration reads the shared state
# ---------------------------------------------------------------------------

def test_health_state_persists_across_separate_orchestration_calls(env):
    """
    The headline regression. Two independent `generate()` calls, one shared
    HealthRegistry: the second must not re-select what the first proved dead.
    """
    registry, runs_root, archive, exec_log, routing_log = env
    dead = seed(registry, runs_root, "model-dead", score=0.99)   # ranks first
    alive = seed(registry, runs_root, "model-alive", score=0.10)

    providers = {dead.candidate_id: FailingProvider(NVIDIA_410),
                 alive.candidate_id: ScriptedProvider(accuracy=1.0)}
    health = HealthRegistry()
    orch = Orchestrator(registry, archive, lambda c: providers[c.candidate_id],
                        exec_log, routing_log, health=health)

    first, first_record = orch.generate(TaskType.QUESTION_GENERATION, "q")
    assert first is not None and first.ok
    assert first_record.candidate_id == alive.candidate_id
    assert first_record.fallback is True
    assert providers[dead.candidate_id].calls == 1
    assert health.allows(key_of(dead)) is False

    second, second_record = orch.generate(TaskType.QUESTION_GENERATION, "q")
    assert second is not None and second.ok
    assert second_record.candidate_id == alive.candidate_id
    # The whole point: the dead model was NOT called a second time.
    assert providers[dead.candidate_id].calls == 1
    assert second_record.fallback is False        # it was never a candidate


def test_without_a_health_registry_the_old_forgetful_behaviour_is_unchanged(env):
    """
    The wiring is opt-in, so every existing caller behaves exactly as before.
    Stated as a test because "additive" is a claim, not an assumption.
    """
    registry, runs_root, archive, exec_log, routing_log = env
    dead = seed(registry, runs_root, "model-dead", score=0.99)
    seed(registry, runs_root, "model-alive", score=0.10)
    failing = FailingProvider(NVIDIA_410)
    providers = {dead.candidate_id: failing}

    orch = Orchestrator(registry, archive,
                        lambda c: providers.get(c.candidate_id) or ScriptedProvider(accuracy=1.0),
                        exec_log, routing_log)
    orch.generate(TaskType.QUESTION_GENERATION, "q")
    orch.generate(TaskType.QUESTION_GENERATION, "q")
    assert failing.calls == 2


@pytest.mark.parametrize("error, expected_status, reselected", [
    (NVIDIA_410, ProviderStatus.MODEL_RETIRED, False),
    ("HTTP 402: payment required", ProviderStatus.BILLING_BLOCKED, False),
    ("HTTP 401: invalid api key", ProviderStatus.AUTH_FAILED, False),
    ("HTTP 404: unknown model", ProviderStatus.MODEL_UNAVAILABLE, False),
    ("curl: (56) CONNECT tunnel failed, response 403",
     ProviderStatus.EGRESS_BLOCKED, False),
    ("read timed out after 180s", ProviderStatus.TIMEOUT, True),
    ("HTTP 429 rate limit exceeded", ProviderStatus.RATE_LIMITED, True),
    ("could not parse JSON from the reply", ProviderStatus.INVALID_RESPONSE, True),
])
def test_each_failure_class_is_classified_and_acted_on_differently(
        env, error, expected_status, reselected):
    """
    Not all errors are the same error. A 410 must never be tried again; a
    timeout and a rate limit must be, because they recover.
    """
    registry, runs_root, archive, exec_log, routing_log = env
    first = seed(registry, runs_root, "model-first", score=0.99)
    seed(registry, runs_root, "model-second", score=0.10)
    failing = FailingProvider(error)
    providers = {first.candidate_id: failing}
    health = HealthRegistry()
    orch = Orchestrator(registry, archive,
                        lambda c: providers.get(c.candidate_id) or ScriptedProvider(accuracy=1.0),
                        exec_log, routing_log, health=health)

    orch.generate(TaskType.QUESTION_GENERATION, "q")
    recorded = [r for r in exec_log.all() if r.candidate_id == first.candidate_id]
    assert recorded[0].failure_status == expected_status

    orch.generate(TaskType.QUESTION_GENERATION, "q")
    assert (failing.calls > 1) is reselected, (error, failing.calls)


def test_a_five_hundred_is_retried_and_then_broken(env):
    """
    A provider fault is neither conclusive nor free. Three of them, and the
    breaker stops paying to find out again.
    """
    registry, runs_root, archive, exec_log, routing_log = env
    first = seed(registry, runs_root, "model-first", score=0.99)
    seed(registry, runs_root, "model-second", score=0.10)
    failing = FailingProvider("HTTP 500: internal server error")
    providers = {first.candidate_id: failing}
    health = HealthRegistry(policy=BreakerPolicy(failure_threshold=3))
    orch = Orchestrator(registry, archive,
                        lambda c: providers.get(c.candidate_id) or ScriptedProvider(accuracy=1.0),
                        exec_log, routing_log, health=health)
    for _ in range(4):
        orch.generate(TaskType.QUESTION_GENERATION, "q")
    assert failing.calls == 3
    assert health.allows(key_of(first)) is False


def test_when_every_candidate_is_broken_the_record_says_so(env):
    """
    Otherwise the log reads "no candidate is capability-matched", which sends
    the reader looking for a capability problem that does not exist.
    """
    registry, runs_root, archive, exec_log, routing_log = env
    only = seed(registry, runs_root, "model-only")
    health = HealthRegistry()
    health.observe(key_of(only), success=False, error=NVIDIA_410,
                   status=ProviderStatus.MODEL_RETIRED)
    orch = Orchestrator(registry, archive, lambda c: FailingProvider("unused"),
                        exec_log, routing_log, health=health)
    response, record = orch.generate(TaskType.QUESTION_GENERATION, "q")
    assert response is None
    assert record.status == "no_eligible_candidate"
    assert "health excluded" in record.error


def test_a_recovered_model_is_usable_again(env):
    registry, runs_root, archive, exec_log, routing_log = env
    flaky = seed(registry, runs_root, "model-flaky", score=0.99)
    seed(registry, runs_root, "model-steady", score=0.10)
    health = HealthRegistry(policy=BreakerPolicy(failure_threshold=3,
                                                 cooldown_seconds=0.0))
    for _ in range(3):
        health.observe(key_of(flaky), success=False, error="504 gateway timeout",
                       status=ProviderStatus.TIMEOUT)
    assert health.allows(key_of(flaky)) in (True, False)   # HALF_OPEN after 0s
    health.observe(key_of(flaky), success=True, latency_ms=20.0)
    assert health.allows(key_of(flaky)) is True

    orch = Orchestrator(registry, archive, lambda c: ScriptedProvider(accuracy=1.0),
                        exec_log, routing_log, health=health)
    _, record = orch.generate(TaskType.QUESTION_GENERATION, "q")
    assert record.candidate_id == flaky.candidate_id


# ---------------------------------------------------------------------------
# Orchestration writes to the discovery registry too
# ---------------------------------------------------------------------------

def test_a_retirement_seen_in_production_outlives_the_process(env, tmp_path):
    """
    A 410 in production is the same fact a discovery probe would learn, and
    paying for it twice is silly. Persisting it means a restart routes around
    the model without spending a call to rediscover it.
    """
    registry, runs_root, archive, exec_log, routing_log = env
    dead = seed(registry, runs_root, "model-dead", score=0.99)
    seed(registry, runs_root, "model-alive", score=0.10)

    models = DynamicModelRegistry(tmp_path / "models.json")
    models.reconcile(dead.provider, [Observation(provider=dead.provider,
                                                 model_id=dead.model_id)])
    models.record_probe(key_of(dead), http_status=200)

    providers = {dead.candidate_id: FailingProvider(NVIDIA_410)}
    orch = Orchestrator(registry, archive,
                        lambda c: providers.get(c.candidate_id) or ScriptedProvider(accuracy=1.0),
                        exec_log, routing_log, health=HealthRegistry(),
                        model_registry=models)
    orch.generate(TaskType.QUESTION_GENERATION, "q")

    reopened = DynamicModelRegistry(tmp_path / "models.json")
    record = reopened.get(key_of(dead))
    assert record.availability == Availability.RETIRED
    assert "410" in record.retirement_reason


def test_a_retired_model_is_barred_even_with_a_fresh_health_registry(env, tmp_path):
    """
    Health is in-process and resets on restart; a retirement is durable. The
    registry is what carries the fact across the gap.
    """
    registry, runs_root, archive, exec_log, routing_log = env
    dead = seed(registry, runs_root, "model-dead", score=0.99)
    alive = seed(registry, runs_root, "model-alive", score=0.10)

    models = DynamicModelRegistry(tmp_path / "models.json")
    models.reconcile(dead.provider, [Observation(provider=dead.provider,
                                                 model_id=dead.model_id)])
    models.record_probe(key_of(dead), error=NVIDIA_410, http_status=410)

    called = []

    def factory(candidate):
        called.append(candidate.candidate_id)
        return ScriptedProvider(accuracy=1.0)

    orch = Orchestrator(registry, archive, factory, exec_log, routing_log,
                        health=HealthRegistry(), model_registry=models)
    _, record = orch.generate(TaskType.QUESTION_GENERATION, "q")
    assert record.candidate_id == alive.candidate_id
    assert dead.candidate_id not in called


def test_a_successful_call_records_health_so_the_next_one_can_read_it(env):
    registry, runs_root, archive, exec_log, routing_log = env
    good = seed(registry, runs_root, "model-good")
    health = HealthRegistry()
    orch = Orchestrator(registry, archive, lambda c: ScriptedProvider(accuracy=1.0),
                        exec_log, routing_log, health=health)
    orch.generate(TaskType.QUESTION_GENERATION, "q")
    reading = health.health(key_of(good))
    assert reading["n"] == 1
    assert reading["success_rate"] == 1.0


# ---------------------------------------------------------------------------
# Phase 6: a promotion may not name a model that no longer exists
# ---------------------------------------------------------------------------

def test_a_passing_run_cannot_promote_a_withdrawn_model(tmp_path):
    """
    The router and the orchestrator already refuse to CALL a retired model, so
    production degrades safely without this. What this prevents is different:
    a deployment record, and an admin console, naming a dead model as the
    production model for a task. That is not an outage, it is a false
    statement about what is serving learners.

    Eleven models on this account were retired inside one week, so a passing
    run outliving its model is not hypothetical.
    """
    from benchmark.promotion_api import PromotionAPI, PromotionError

    models = DynamicModelRegistry(tmp_path / "models.json")
    models.reconcile("nvidia", [Observation(provider="nvidia", model_id="gone"),
                                Observation(provider="nvidia", model_id="alive")])
    models.record_probe("nvidia:gone", error=NVIDIA_410, http_status=410)
    models.record_probe("nvidia:alive", http_status=200)

    class FakeCandidate:
        def __init__(self, provider, model_id):
            self.provider, self.model_id = provider, model_id

    class FakeRegistry:
        def __init__(self, mapping):
            self._m = mapping

        def get(self, cid):
            return self._m.get(cid)

    class FakeAI:
        registry = None

    api = PromotionAPI(FakeAI(), archive=object(),
                       registry=FakeRegistry({
                           "cand-gone": FakeCandidate("nvidia", "gone"),
                           "cand-alive": FakeCandidate("nvidia", "alive")}),
                       model_registry=models)

    passing = {"candidate_id": "cand-gone", "integrity_satisfied": True,
               "scores_withheld": False, "outcome": "PASS"}
    reason = api._blocking_reason(passing)
    assert reason is not None
    assert "withdrawn by the provider" in reason
    assert "410" in reason

    # The live model with the same passing run is promotable.
    passing["candidate_id"] = "cand-alive"
    assert api._blocking_reason(passing) is None


def test_withdrawal_is_reported_ahead_of_integrity(tmp_path):
    """
    Ordered most-fundamental-first. Telling the reader "its integrity checks
    failed" about a model that no longer exists sends them to fix the wrong
    thing.
    """
    from benchmark.promotion_api import PromotionAPI

    models = DynamicModelRegistry(tmp_path / "models.json")
    models.reconcile("nvidia", [Observation(provider="nvidia", model_id="gone")])
    models.record_probe("nvidia:gone", error=NVIDIA_410, http_status=410)

    class FakeCandidate:
        provider, model_id = "nvidia", "gone"

    class FakeRegistry:
        def get(self, cid):
            return FakeCandidate()

    class FakeAI:
        registry = None

    api = PromotionAPI(FakeAI(), archive=object(), registry=FakeRegistry(),
                       model_registry=models)
    both_wrong = {"candidate_id": "cand-gone", "integrity_satisfied": False,
                  "scores_withheld": True, "outcome": "FAIL"}
    assert "withdrawn by the provider" in api._blocking_reason(both_wrong)


def test_no_model_registry_means_no_opinion_not_a_pass(tmp_path):
    """
    A missing discovery registry means discovery has not run here, which is
    not evidence that a model is fine. The check says nothing rather than
    approving.
    """
    from benchmark.promotion_api import PromotionAPI

    class FakeAI:
        registry = None

    api = PromotionAPI(FakeAI(), archive=object(), registry=None, model_registry=None)
    assert api._withdrawn_reason("anything") is None
