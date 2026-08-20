"""
Tests for failure classification, the provider registry, and the two-layer router.

The property under test throughout is one sentence:

    ADAPTER WORKS != PROVIDER REACHABLE != PROVIDER HEALTHY

and the thing being prevented is a firewall being recorded as evidence about
a model.
"""

from __future__ import annotations

import pytest

from benchmark.evaluation import EvaluationScheduler
from benchmark.fitness import PerformanceScore
from benchmark.health import BreakerPolicy, CLOSED, HealthRegistry, OPEN, UNAVAILABLE
from benchmark.provider_registry import (ModelEntry, ProviderEntry, ProviderRegistry,
                                         default_registry)
from benchmark.provider_status import ProviderStatus, assess, classify, policy_for
from benchmark.quintek_router import Candidate, QuintekRouter

EGRESS = "curl: (56) CONNECT tunnel failed, response 403"


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("error, expected", [
    (EGRESS, ProviderStatus.EGRESS_BLOCKED),
    ("gateway answered 403 to CONNECT (policy denial)", ProviderStatus.EGRESS_BLOCKED),
    ("connect_rejected", ProviderStatus.EGRESS_BLOCKED),
    ("HTTP 429 rate limit exceeded", ProviderStatus.RATE_LIMITED),
    ("Too Many Requests", ProviderStatus.RATE_LIMITED),
    ("read timed out after 180s", ProviderStatus.TIMEOUT),
    ("504 Gateway Timeout", ProviderStatus.TIMEOUT),
    ("404 page not found", ProviderStatus.MODEL_UNAVAILABLE),
    ("unknown model: llama-9", ProviderStatus.MODEL_UNAVAILABLE),
    ("401 Unauthorized", ProviderStatus.AUTH_FAILED),
    ("invalid api key", ProviderStatus.AUTH_FAILED),
    ("could not parse JSON from the reply", ProviderStatus.INVALID_RESPONSE),
    ("kernel panic", ProviderStatus.UNKNOWN_ERROR),
])
def test_errors_are_classified(error, expected):
    assert classify(error) == expected


def test_a_proxy_403_is_not_confused_with_an_api_403():
    """
    Otherwise indistinguishable, and they demand opposite responses: one is
    permanent and environmental, the other is a credential problem.
    """
    assert classify(EGRESS) == ProviderStatus.EGRESS_BLOCKED
    assert classify("403 Forbidden: your key lacks access") == ProviderStatus.AUTH_FAILED


def test_a_timeout_exception_is_recognised_by_type():
    assert classify(TimeoutError("no message")) == ProviderStatus.TIMEOUT


def test_a_successful_but_very_slow_call_is_degraded_not_available():
    assert assess(None, latency_ms=103_000, slow_threshold_ms=15_000).status == \
        ProviderStatus.DEGRADED
    assert assess(None, latency_ms=900, slow_threshold_ms=15_000).status == \
        ProviderStatus.AVAILABLE


# ---------------------------------------------------------------------------
# Policies -- what each class implies
# ---------------------------------------------------------------------------

def test_an_egress_block_is_never_retried():
    policy = policy_for(ProviderStatus.EGRESS_BLOCKED)
    assert policy.retryable is False
    assert policy.open_circuit is True
    # No cooldown makes a firewall go away.
    assert policy.circuit_seconds is None


def test_a_rate_limit_does_not_open_the_circuit():
    """The provider is healthy; we are being greedy. Taking it out of
    rotation would punish it for our request rate."""
    policy = policy_for(ProviderStatus.RATE_LIMITED)
    assert policy.open_circuit is False
    assert policy.retryable is True
    assert policy.backoff_seconds > 0


def test_a_timeout_is_retried_once_then_opens_the_circuit():
    policy = policy_for(ProviderStatus.TIMEOUT)
    assert policy.retryable is True
    assert policy.max_retries == 1
    assert policy.open_circuit is True
    assert policy.circuit_seconds is not None


def test_only_an_unusable_reply_counts_against_the_model():
    """
    Everything else is environmental. A model is not worse because a firewall
    exists, a key is wrong, or a host is busy.
    """
    counting = [s for s in vars(ProviderStatus).values()
                if isinstance(s, str) and s.isupper()
                and policy_for(s).counts_against_quality]
    assert counting == [ProviderStatus.INVALID_RESPONSE]


def test_environmental_failures_are_flagged_as_such():
    assert assess(EGRESS).environmental is True
    assert assess("401 Unauthorized").environmental is True
    assert assess("429 rate limited").environmental is True
    assert assess("could not parse JSON").environmental is False
    assert assess(None).environmental is False


def test_every_status_has_a_policy_with_guidance():
    from benchmark.provider_status import ALL_STATUSES
    for status in ALL_STATUSES:
        assert policy_for(status).guidance, status


# ---------------------------------------------------------------------------
# The breaker responds differently per class
# ---------------------------------------------------------------------------

def test_an_egress_block_opens_the_circuit_and_keeps_it_open():
    health = HealthRegistry(policy=BreakerPolicy(failure_threshold=3, cooldown_seconds=1))
    for _ in range(3):
        health.observe("cerebras:x", success=False, error=EGRESS)
    breaker = health.breaker("cerebras:x")
    assert breaker.state == OPEN
    assert breaker.current_cooldown == float("inf")


def test_rate_limiting_never_opens_the_circuit():
    health = HealthRegistry(policy=BreakerPolicy(failure_threshold=2))
    for _ in range(6):
        health.observe("p:m", success=False, error="429 rate limit exceeded")
    assert health.breaker("p:m").state == CLOSED
    assert health.allows("p:m") is True


def test_a_timeout_opens_the_circuit_with_a_finite_cooldown():
    health = HealthRegistry(policy=BreakerPolicy(failure_threshold=3, cooldown_seconds=600))
    for _ in range(3):
        health.observe("p:m", success=False, timeout=True, error="read timed out")
    breaker = health.breaker("p:m")
    assert breaker.state == OPEN
    assert breaker.current_cooldown not in (None, float("inf"))


def test_an_unusable_reply_does_not_take_the_provider_out_of_rotation():
    """The host is fine; the model is not. Those need different responses."""
    health = HealthRegistry(policy=BreakerPolicy(failure_threshold=2))
    for _ in range(5):
        health.observe("p:m", success=False, error="could not parse JSON")
    assert health.breaker("p:m").state == CLOSED


def test_health_separates_environmental_failures_from_attributable_ones():
    health = HealthRegistry()
    for _ in range(3):
        health.observe("p:m", success=False, error=EGRESS)
    report = health.health("p:m")
    assert report["environmental_failures"] == 3
    # The rate a quality judgement may consult has no data at all -- which is
    # the correct answer, not zero.
    assert report["attributable_success_rate"] is None
    assert report["status_counts"] == {ProviderStatus.EGRESS_BLOCKED: 3}


def test_a_blocked_provider_is_marked_unavailable_not_merely_unhealthy():
    health = HealthRegistry()
    health.observe("p:m", success=False, error=EGRESS)
    assert health.declared_state("p:m") == UNAVAILABLE


# ---------------------------------------------------------------------------
# The provider registry
# ---------------------------------------------------------------------------

def test_adapter_mock_and_live_are_three_separate_facts():
    registry = default_registry()
    registry.record_probe("cerebras", error=EGRESS, host="api.cerebras.ai:443")
    entry = registry.provider("cerebras")

    assert entry.adapter_ok is True         # the code is written
    assert entry.mock_tested is True        # and tested
    assert entry.live_ok is None            # but never reached from here
    assert entry.status == ProviderStatus.EGRESS_BLOCKED
    assert entry.blocked_host == "api.cerebras.ai:443"


def test_a_blocked_provider_is_not_recorded_as_having_failed():
    """
    "We could not test this" is not a test result. live_ok must stay None
    rather than becoming False.
    """
    registry = default_registry()
    registry.record_probe("openrouter", error=EGRESS, host="openrouter.ai:443")
    assert registry.provider("openrouter").live_ok is None
    assert registry.provider("openrouter").live_symbol == "—"


def test_a_host_that_answered_badly_is_recorded_as_having_failed():
    registry = default_registry()
    registry.record_probe("nvidia", error="500 Internal Server Error")
    assert registry.provider("nvidia").live_ok is False


def test_a_slow_but_working_provider_is_live_and_degraded():
    registry = default_registry()
    registry.record_probe("nvidia", latency_ms=103_000, slow_threshold_ms=15_000)
    entry = registry.provider("nvidia")
    assert entry.live_ok is True
    assert entry.status == ProviderStatus.DEGRADED


def test_the_report_explains_that_blocked_adapters_have_not_failed():
    registry = default_registry()
    registry.record_probe("cerebras", error=EGRESS, host="api.cerebras.ai:443")
    report = registry.report()
    assert "cerebras" not in report["usable_providers"]
    assert "they have not failed" in report["note"]
    assert report["egress_blocked"][0]["host"] == "api.cerebras.ai:443"


def test_a_model_cannot_be_registered_against_an_unknown_provider():
    registry = ProviderRegistry()
    with pytest.raises(KeyError, match="not registered"):
        registry.add_model(ModelEntry("nonesuch", "some-model"))


def test_provider_and_model_are_separate_dimensions():
    """
    The interesting finding is usually "model M is best, and P is the faster
    host for it" -- invisible if the two collapse into one identifier.
    """
    registry = ProviderRegistry()
    for name in ("a", "b"):
        registry.add_provider(ProviderEntry(name))
    registry.add_model(ModelEntry("a", "shared-model", capabilities={"reasoning": True}))
    registry.add_model(ModelEntry("b", "shared-model", capabilities={"reasoning": True}))

    keys = [m.key for m in registry.models()]
    assert keys == ["a:shared-model", "b:shared-model"]
    assert [m.model_id for m in registry.models()] == ["shared-model", "shared-model"]


def test_layer_one_filters_on_capability():
    registry = ProviderRegistry()
    registry.add_provider(ProviderEntry("p"))
    registry.add_model(ModelEntry("p", "text", capabilities={"reasoning": True}))
    registry.add_model(ModelEntry("p", "multimodal",
                                  capabilities={"reasoning": True, "vision": True}))

    kept, dropped = registry.eligible(required_capabilities=("vision",))
    assert [m.key for m in kept] == ["p:multimodal"]
    assert dropped[0]["reason"] == "does not claim vision"


def test_layer_one_drops_blocked_providers_and_says_it_is_environmental():
    registry = default_registry()
    registry.add_model(ModelEntry("cerebras", "llama3.1-8b",
                                  capabilities={"reasoning": True}))
    registry.record_probe("cerebras", error=EGRESS, host="api.cerebras.ai:443")

    kept, dropped = registry.eligible()
    assert not [m for m in kept if m.provider == "cerebras"]
    entry = next(d for d in dropped if d["key"] == "cerebras:llama3.1-8b")
    assert entry["environmental"] is True
    assert "EGRESS_BLOCKED" in entry["reason"]


def test_the_rendered_table_shows_all_four_columns():
    registry = default_registry()
    registry.record_probe("cerebras", error=EGRESS, host="api.cerebras.ai:443")
    text = registry.render()
    assert "adapter: yes" in text
    assert "mock:    yes" in text
    assert "live:    —" in text
    assert "status:  EGRESS_BLOCKED" in text


# ---------------------------------------------------------------------------
# The two-layer router
# ---------------------------------------------------------------------------

def build(reg_status: dict[str, str | None]):
    registry = default_registry()
    candidates = []
    for provider, error in reg_status.items():
        registry.add_model(ModelEntry(provider, "m", capabilities={"medical_qa": True}))
        if error:
            registry.record_probe(provider, error=error, host=f"{provider}.test:443")
        candidates.append(Candidate(f"{provider}:m", provider, "m", {"medical_qa"}))
    perf = {c.key: PerformanceScore(c.key, n=40, success_rate=1.0, latency_p95_ms=1200,
                                    accepted_rate=0.9, mean_quality=0.9)
            for c in candidates}
    router = QuintekRouter(candidates, provider_registry=registry,
                           performance_for=lambda k, t: perf[k],
                           health_for=lambda k: {"usable_now": True},
                           required_capabilities={"EXPLANATION": ("medical_qa",)})
    return router


def test_layer_one_and_layer_two_are_reported_separately():
    decision = build({"nvidia": None, "cerebras": EGRESS}).route("EXPLANATION", roll=0.99)
    assert decision.layer1_eligible == ["nvidia:m"]
    assert decision.layer2_ranked == ["nvidia:m"]
    assert decision.selected == "nvidia:m"


def test_a_blocked_provider_is_excluded_at_layer_one_as_environmental():
    decision = build({"nvidia": None, "cerebras": EGRESS,
                      "openrouter": EGRESS}).route("EXPLANATION", roll=0.99)
    excluded = decision.as_dict()["environmental_exclusions"]
    assert set(excluded) == {"cerebras:m", "openrouter:m"}
    for entry in decision.environmental_exclusions:
        assert entry["layer"] == 1
        assert "EGRESS_BLOCKED" in entry["reason"]


def test_passing_layer_one_does_not_make_a_provider_preferred():
    """
    A provider that merely works must not become the preferred one by default;
    that decision belongs to layer 2.
    """
    registry = default_registry()
    for provider in ("nvidia", "cerebras"):
        registry.add_model(ModelEntry(provider, "m", capabilities={"medical_qa": True}))
    candidates = [Candidate(f"{p}:m", p, "m", {"medical_qa"}) for p in ("nvidia", "cerebras")]
    perf = {
        "nvidia:m": PerformanceScore("nvidia:m", n=40, success_rate=1.0,
                                     latency_p95_ms=1200, accepted_rate=0.3,
                                     mean_quality=0.3),
        "cerebras:m": PerformanceScore("cerebras:m", n=40, success_rate=1.0,
                                       latency_p95_ms=400, accepted_rate=0.95,
                                       mean_quality=0.95),
    }
    router = QuintekRouter(candidates, provider_registry=registry,
                           performance_for=lambda k, t: perf[k],
                           health_for=lambda k: {"usable_now": True},
                           required_capabilities={"EXPLANATION": ("medical_qa",)})
    decision = router.route("EXPLANATION", roll=0.99)
    assert set(decision.layer1_eligible) == {"nvidia:m", "cerebras:m"}
    assert decision.selected == "cerebras:m"     # layer 2 decided, not layer 1


# ---------------------------------------------------------------------------
# The evaluation scheduler
# ---------------------------------------------------------------------------

TASKS = [(f"t{i:03d}", ["MCQ", "VIGNETTE", "CONCEPT"][i % 3]) for i in range(30)]


def test_the_scheduler_fills_the_emptiest_cells_first():
    scheduler = EvaluationScheduler(quota=5)
    plan = scheduler.plan(candidates=["a"], tasks=TASKS,
                          coverage={"a": {"MCQ": 5, "VIGNETTE": 0, "CONCEPT": 0}}, limit=4)
    assert all(call.task_type in ("VIGNETTE", "CONCEPT") for call in plan)


def test_the_scheduler_never_queues_work_for_a_blocked_provider():
    """
    Scheduling work for a host behind a firewall produces a queue of certain
    failures and a coverage matrix of zeros that look like poor performance.
    """
    scheduler = EvaluationScheduler(quota=5,
                                    usable=lambda c: not c.startswith("cerebras"))
    plan = scheduler.plan(candidates=["nvidia:8b", "cerebras:8b"], tasks=TASKS,
                          coverage={}, limit=10)
    assert plan
    assert all(not call.candidate.startswith("cerebras") for call in plan)


def test_the_scheduler_does_not_hand_a_candidate_the_same_task_twice():
    scheduler = EvaluationScheduler(quota=10)
    plan = scheduler.plan(candidates=["a"], tasks=TASKS, coverage={}, limit=15)
    task_ids = [call.task_id for call in plan]
    assert len(task_ids) == len(set(task_ids))


def test_the_scheduler_returns_nothing_when_no_candidate_is_usable():
    scheduler = EvaluationScheduler(quota=5, usable=lambda c: False)
    assert scheduler.plan(candidates=["a", "b"], tasks=TASKS, coverage={}) == []


def test_progress_reports_an_incomplete_matrix_as_provisional():
    scheduler = EvaluationScheduler(quota=5)
    progress = scheduler.progress(candidates=["a"], task_types=["MCQ", "VIGNETTE"],
                                  coverage={"a": {"MCQ": 5, "VIGNETTE": 1}})
    assert progress["cells_filled"] == 1
    assert progress["complete"] is False
    assert "provisional" in progress["note"]


def test_a_complete_matrix_carries_no_warning():
    scheduler = EvaluationScheduler(quota=5)
    progress = scheduler.progress(candidates=["a"], task_types=["MCQ"],
                                  coverage={"a": {"MCQ": 5}})
    assert progress["complete"] is True
    assert progress["note"] == ""


# ---------------------------------------------------------------------------
# Regression: a failure must never be silently lost
# ---------------------------------------------------------------------------

def test_a_bare_timeout_with_no_message_still_trips_the_breaker():
    """
    Found by running the suite after wiring classification in: a caller
    reporting `success=False, timeout=True` with no error text classified as
    AVAILABLE, whose policy does not open circuits -- so the failure was
    recorded nowhere and the breaker never tripped.
    """
    health = HealthRegistry(policy=BreakerPolicy(failure_threshold=3, timeout_weight=2))
    health.observe("k", success=False, timeout=True)
    health.observe("k", success=False, timeout=True)
    assert health.breaker("k").state == OPEN


def test_a_bare_failure_with_no_detail_is_recorded_as_unknown_not_available():
    health = HealthRegistry(policy=BreakerPolicy(failure_threshold=2))
    health.observe("j", success=False)
    health.observe("j", success=False)
    assert health.breaker("j").state == OPEN
    assert health.health("j")["status_counts"] == {ProviderStatus.UNKNOWN_ERROR: 2}


def test_a_failure_is_never_classified_as_available():
    """The invariant behind both regressions above."""
    health = HealthRegistry()
    for kwargs in ({}, {"timeout": True}, {"error": ""}, {"latency_ms": 10.0}):
        verdict = health.observe(f"k{id(kwargs)}", success=False, **kwargs)
        assert verdict["status"] != ProviderStatus.AVAILABLE, kwargs
        assert verdict["ok"] is False
