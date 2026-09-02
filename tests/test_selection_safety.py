"""
What may and may not decide which model serves a request.

THE DEFECT THIS FILE OPENED WITH
--------------------------------
`blend` renormalises over the components that were actually measured, which is
right: treating "no cost data" as "infinitely expensive" would bury an unpriced
model for a reason that has nothing to do with the model. But it makes scores
from different coverage incomparable, and nothing read the `weight_covered` it
recorded. Measured on this tree:

    nvidia:cheap    fitness 1.000   from 20% of the weighting   (a price, nothing else)
    nvidia:proven   fitness 0.936   from 100%                   (200 observations)

and production selected `nvidia:cheap`. That is "selected solely because it is
cheap", which is the thing Quintek's business constraints most need not to
happen. `ModelFitness.rank_key` now sorts a thinly-evidenced score below every
fully-measured one, while leaving it ELIGIBLE so exploration can still reach
it -- a candidate that can never be picked can never be measured.

Every scenario here runs against scripted performance figures and fake
transports. Nothing in this file spends a provider credit.
"""

from __future__ import annotations

import pytest

from benchmark.discovery import (Availability, DynamicModelRegistry, Observation,
                                 Pricing, price_state)
from benchmark.fitness import (MIN_OBSERVATIONS, MIN_WEIGHT_COVERAGE,
                               PerformanceScore, UTILITY_PROFILES, _normalise_cost,
                               blend, score_fitness)
from benchmark.quintek_router import Candidate, QuintekRouter

T0 = "2026-08-01T00:00:00Z"
NVIDIA_410 = ("HTTP 410: The model has reached its end of life on "
              "2026-08-26T09:00:00Z and is no longer available.")

CHEAP, PROVEN = "nvidia:cheap", "nvidia:proven"
PAIR = [Candidate(CHEAP, "nvidia", "cheap"), Candidate(PROVEN, "nvidia", "proven")]


def measured(key, **kwargs):
    base = dict(n=MIN_OBSERVATIONS * 5, mean_quality=0.90, accepted_rate=0.90,
                latency_p95_ms=2000.0, success_rate=0.99, structured_ok_rate=0.95,
                cost_per_1k=0.5)
    base.update(kwargs)
    return PerformanceScore(key, **base)


def route(perf, **kwargs):
    return QuintekRouter(PAIR, performance_for=lambda k, t: perf[k]).route(
        "QUESTION_GENERATION", **kwargs)


# ---------------------------------------------------------------------------
# Cost may inform a decision. It may not make one.
# ---------------------------------------------------------------------------

def test_a_model_is_not_selected_solely_because_it_is_cheap():
    """The regression. See this file's docstring for the measured numbers."""
    perf = {CHEAP: measured(CHEAP, mean_quality=None, accepted_rate=None,
                            latency_p95_ms=None, success_rate=None,
                            structured_ok_rate=None, cost_per_1k=0.0),
            PROVEN: measured(PROVEN)}
    cheap = score_fitness(CHEAP, task_type="QUESTION_GENERATION",
                          performance=perf[CHEAP])
    proven = score_fitness(PROVEN, task_type="QUESTION_GENERATION",
                           performance=perf[PROVEN])
    # The cheap one still SCORES higher -- the arithmetic is not being fudged.
    assert cheap.fitness > proven.fitness
    assert cheap.thinly_evidenced is True
    assert proven.thinly_evidenced is False
    # It just cannot lead the ranking on that.
    decision = route(perf, roll=0.99)
    assert decision.layer2_ranked == [PROVEN, CHEAP]
    assert decision.selected == PROVEN


def test_a_thinly_evidenced_candidate_stays_eligible_and_reachable():
    """
    Excluding it would be the opposite error: a model that can never be picked
    can never accumulate the evidence that would justify picking it.
    """
    perf = {CHEAP: PerformanceScore(CHEAP, cost_per_1k=0.0),
            PROVEN: measured(PROVEN)}
    thin = score_fitness(CHEAP, task_type="QUESTION_GENERATION",
                         performance=perf[CHEAP])
    assert thin.eligible is True
    assert any("intended weighting" in reason for reason in thin.reasons)
    # Forced exploration reaches it precisely because it is under-observed.
    decision = route(perf, roll=0.99)
    assert decision.selected == CHEAP
    assert "forced exploration" in decision.reason
    # And evaluation mode routes to whoever knows least, always.
    assert route(perf, mode="evaluation").selected == CHEAP


def test_the_coverage_floor_is_one_named_constant():
    """
    It was 0.5 written into a warning string and nothing else. Two thresholds
    that must agree and are spelled separately eventually disagree.
    """
    assert 0.0 < MIN_WEIGHT_COVERAGE <= 1.0
    _score, detail = blend({"cost": 1.0}, UTILITY_PROFILES["batch"])
    assert detail["weight_covered"] < MIN_WEIGHT_COVERAGE


def test_unknown_pricing_is_never_read_as_zero():
    assert _normalise_cost(None) is None            # not 0.0, not 1.0
    assert _normalise_cost(0.0) == 1.0              # a REAL zero is the best price
    _score, detail = blend({"quality": 0.5, "cost": None}, UTILITY_PROFILES["batch"])
    assert "cost" in detail["dropped"]
    assert "cost" not in detail["used"]


def test_unknown_pricing_cannot_be_shown_to_be_within_a_budget(tmp_path):
    reg = DynamicModelRegistry(tmp_path / "r.json")
    reg.reconcile("openrouter", [
        Observation(provider="openrouter", model_id="priced", input_price=0.4),
        Observation(provider="openrouter", model_id="silent", input_price=None),
        Observation(provider="openrouter", model_id="sentinel", input_price=-1.0,
                    price_stated=False),
        Observation(provider="openrouter", model_id="free", input_price=0.0),
    ], at=T0)
    for model in ("priced", "silent", "sentinel", "free"):
        reg.record_probe(f"openrouter:{model}", http_status=200, at=T0)

    kept, _ = reg.eligible(max_input_price=1.0)
    assert sorted(r.model_id for r in kept) == ["free", "priced"]
    assert reg.get("openrouter:silent").pricing_status == Pricing.UNKNOWN
    assert reg.get("openrouter:sentinel").pricing_status == Pricing.UNPRICED
    assert reg.get("openrouter:free").pricing_status == Pricing.FREE


def test_a_real_zero_and_an_absent_price_are_different_facts():
    assert price_state(0.0) == Pricing.FREE
    assert price_state(None) == Pricing.UNKNOWN
    assert price_state(None, stated=False) == Pricing.UNPRICED


def test_selection_weighs_more_than_price():
    """
    Quality is 30-55% of every profile; cost is 3-20%. Stated as a test so a
    future weight edit that makes cost dominant has to argue with something.
    """
    for name, weights in UTILITY_PROFILES.items():
        assert weights["cost"] < weights["quality"], name
        assert weights["cost"] <= 0.20, name


def test_a_hard_latency_ceiling_beats_any_price(tmp_path):
    """
    Cheap and unusably slow is not a trade-off to be weighed. The measured
    case: a 103-second p95 on an interactive task.
    """
    slow_and_free = measured(CHEAP, latency_p95_ms=103_000.0, cost_per_1k=0.0)
    verdict = score_fitness(CHEAP, task_type="EXPLANATION", performance=slow_and_free)
    assert verdict.eligible is False
    assert any("ceiling" in reason for reason in verdict.reasons)


# ---------------------------------------------------------------------------
# Quota: unknown is not unlimited
# ---------------------------------------------------------------------------

def test_a_rate_limited_model_is_not_eligible_while_it_is_rate_limited(tmp_path):
    """
    No provider adapter here exposes a remaining-quota figure, so quota cannot
    be observed -- which means it must never be ASSUMED either. The observable
    proxy is a 429, and a model currently carrying one is not AVAILABLE.
    """
    reg = DynamicModelRegistry(tmp_path / "r.json")
    reg.reconcile("nvidia", [Observation(provider="nvidia", model_id="m")], at=T0)
    reg.record_probe("nvidia:m", error="HTTP 429 rate limit exceeded",
                     http_status=429, at=T0)
    assert reg.get("nvidia:m").availability == Availability.RATE_LIMITED
    assert reg.eligible()[0] == []
    # But recoverable, and re-probed on the backoff schedule rather than never.
    assert reg.get("nvidia:m").recheckable is True


def test_a_rate_limit_is_recorded_against_the_model_not_charged_to_its_quality():
    """
    Being throttled is our request rate, not the model's fault. If a 429
    counted against quality, hammering a good model would demote it.
    """
    from benchmark.provider_status import ProviderStatus, policy_for

    assert policy_for(ProviderStatus.RATE_LIMITED).counts_against_quality is False
    assert policy_for(ProviderStatus.BILLING_BLOCKED).counts_against_quality is False
    assert policy_for(ProviderStatus.MODEL_RETIRED).counts_against_quality is False
    # The one class that IS about the model: it answered, unusably.
    assert policy_for(ProviderStatus.INVALID_RESPONSE).counts_against_quality is True


# ---------------------------------------------------------------------------
# Stage 7 scenario L: the best model retires mid-flight
# ---------------------------------------------------------------------------

def test_the_leading_model_retiring_hands_traffic_over_without_a_deployment(tmp_path):
    """
    Scenario L. The candidate set, the code and the weights are identical
    across these two routes. Only the registry file moved -- which is what a
    cron discovery run, or a 410 seen in production, writes.
    """
    reg = DynamicModelRegistry(tmp_path / "r.json")
    reg.reconcile("nvidia", [Observation(provider="nvidia", model_id="proven"),
                             Observation(provider="nvidia", model_id="cheap")], at=T0)
    for model in ("proven", "cheap"):
        reg.record_probe(f"nvidia:{model}", http_status=200, at=T0)

    perf = {CHEAP: measured(CHEAP, mean_quality=0.50),
            PROVEN: measured(PROVEN, mean_quality=0.95)}

    def router():
        return QuintekRouter(PAIR, performance_for=lambda k, t: perf[k],
                             model_registry=reg)

    before = router().route("QUESTION_GENERATION", roll=0.99)
    assert before.selected == PROVEN

    reg.record_probe(PROVEN, error=NVIDIA_410, http_status=410)
    after = router().route("QUESTION_GENERATION", roll=0.99)
    assert after.selected == CHEAP
    dropped = [c for c in after.considered if c["key"] == PROVEN][0]
    assert dropped["dropped_at"] == "layer0_retired"
    # A withdrawal is not a quality signal, and the historical figures stand.
    assert dropped["environmental"] is True
    assert perf[PROVEN].mean_quality == 0.95


def test_every_candidate_retiring_is_reported_not_silently_empty(tmp_path):
    """
    The failure mode worth naming: routing raises with the reason per
    candidate rather than returning something arbitrary.
    """
    from benchmark.quintek_router import NoRoutableCandidate

    reg = DynamicModelRegistry(tmp_path / "r.json")
    reg.reconcile("nvidia", [Observation(provider="nvidia", model_id="proven"),
                             Observation(provider="nvidia", model_id="cheap")], at=T0)
    for model in ("proven", "cheap"):
        reg.record_probe(f"nvidia:{model}", error=NVIDIA_410, http_status=410, at=T0)

    perf = {CHEAP: measured(CHEAP), PROVEN: measured(PROVEN)}
    router = QuintekRouter(PAIR, performance_for=lambda k, t: perf[k],
                           model_registry=reg)
    with pytest.raises(NoRoutableCandidate) as excinfo:
        router.route("QUESTION_GENERATION")
    assert "retired" in str(excinfo.value)


# ---------------------------------------------------------------------------
# The scenario map, so coverage is auditable rather than asserted
# ---------------------------------------------------------------------------

SCENARIOS = {
    "A model disappears from catalogue": "test_discovery.py::test_2*",
    "B model returns 410": "test_discovery.py::test_3b, test_orchestration_health.py",
    "C catalogue lists it, inference 404": "test_discovery.py::test_3c, test_8",
    "D model times out": "test_discovery.py::test_5",
    "E provider goes down": "test_discovery.py::test_16",
    "F provider returns 429": "test_discovery.py::test_3_to_7, this file",
    "G provider returns 402": "test_discovery.py::test_3_to_7",
    "H unknown capabilities": "test_capability_probe.py::test_9, test_an_unreachable_*",
    "I unknown pricing": "this file, test_discovery.py::test_10",
    "J newly discovered model becomes available": "test_capability_probe.py::test_22",
    "K previously unavailable model recovers": "test_discovery.py::test_7",
    "L best model retires during production": "this file",
    "M best model retires before experiment freeze": "test_discovery.py::test_13",
    "N health persists across orchestration calls": "test_orchestration_health.py",
    "O discovery runs without redeployment": "test_discovery.py::test_15",
}


def test_every_adversarial_scenario_has_a_named_home():
    """
    Not a behavioural test. A checklist that lives next to the code, so
    "which of these did we actually cover" is answerable without re-reading
    four files.
    """
    assert len(SCENARIOS) == 15
    assert all(value for value in SCENARIOS.values())


# ---------------------------------------------------------------------------
# External registries: what a catalogue may and may not establish
# ---------------------------------------------------------------------------

def test_a_declared_capability_never_reaches_qualified(tmp_path):
    """
    OpenRouter publishes capability metadata for 421 models and charges for
    inference. Without OPENROUTER_API_KEY the catalogue is readable and the
    models are not callable, so 267 of them DECLARE both validation
    capabilities and none can be QUALIFIED. That gap is the whole point of
    keeping DECLARED and OBSERVED apart: a registry that let a vendor's own
    metadata qualify a model would have produced a Phase 0 pairing out of
    nothing.
    """
    reg = DynamicModelRegistry(tmp_path / "r.json")
    reg.reconcile("openrouter", [Observation(
        provider="openrouter", model_id="vendor/m", context_window=128_000,
        capabilities={"structured_output": True, "reasoning": True})], at=T0)
    record = reg.get("openrouter:vendor/m")
    assert record.capability("structured_output").source == "DECLARED"
    assert record.availability == Availability.UNVERIFIED

    # Declared is enough for a permissive view...
    kept, _ = reg.eligible(required_capabilities=("structured_output", "reasoning"))
    assert kept == []           # still not AVAILABLE, so still not eligible

    reg.record_probe("openrouter:vendor/m", http_status=200, at=T0)
    kept, _ = reg.eligible(required_capabilities=("structured_output", "reasoning"))
    assert [r.key for r in kept] == ["openrouter:vendor/m"]

    # ...and never enough for qualification, which requires observation.
    kept, dropped = reg.eligible(required_capabilities=("structured_output", "reasoning"),
                                 require_observed=True)
    assert kept == []
    assert all("declared, not observed" in r for r in dropped[0]["reasons"])


def test_the_probe_ceiling_is_configuration_not_a_constant(tmp_path):
    """
    External spend must be bounded by a number somebody wrote down in advance,
    the same reason tools_validator_eval.py refuses to start without
    --max-calls.
    """
    from benchmark.discovery import DiscoveryPolicy

    assert DiscoveryPolicy().probe_call_ceiling > 0
    path = tmp_path / "discovery.json"
    path.write_text('{"probe_call_ceiling": 9}')
    assert DiscoveryPolicy.load(path).probe_call_ceiling == 9


def test_a_forecast_over_the_ceiling_refuses_rather_than_truncating(tmp_path,
                                                                    monkeypatch,
                                                                    capsys):
    """
    Truncating silently would leave a half-probed provider looking like a
    complete picture, and "no model qualifies" would then be a budget rather
    than a finding.
    """
    import tools_discovery

    reg_path = tmp_path / "models.json"
    reg = DynamicModelRegistry(reg_path)
    reg.reconcile("nvidia", [Observation(provider="nvidia", model_id=f"m{i}")
                             for i in range(10)], at=T0)
    for i in range(10):
        reg.record_probe(f"nvidia:m{i}", http_status=200, at=T0)
    reg.save()
    (tmp_path / "policy.json").write_text('{"probe_call_ceiling": 5}')

    class Args:
        registry, policy = str(reg_path), str(tmp_path / "policy.json")
        role, providers = "validation", "nvidia"
        limit, max_calls = 0, None
        include_opt_in = dry_run = False

    assert tools_discovery.run_capability_probe(Args()) == 2
    out = capsys.readouterr()
    assert "EXCEEDED" in out.out
    assert "exceeds the configured ceiling" in out.err


def test_a_base_endpoint_that_is_not_a_completions_url_is_refused(capsys):
    """
    `NVIDIAProvider` POSTs to base_url verbatim. This repository's own
    documented example passed `.../v1`, which would have spent the whole
    2295-attempt budget collecting 404s and reported them as a validator that
    could not reach a model. Refused rather than rewritten: --endpoint is
    recorded in the freeze manifest as where the requests went, and a manifest
    naming one URL while the run used another is a provenance record that lies.
    """
    import tools_validator_eval as tool

    class Args:
        endpoint = "https://integrate.api.nvidia.com/v1"

    assert tool._reject_bad_endpoint(Args()) is True
    assert "not a completions URL" in capsys.readouterr().err

    class Good:
        endpoint = "https://integrate.api.nvidia.com/v1/chat/completions"

    assert tool._reject_bad_endpoint(Good()) is False

    class Unset:
        endpoint = ""

    assert tool._reject_bad_endpoint(Unset()) is False


def test_the_documented_run_command_uses_a_completions_url():
    """The doc was the source of the bad example; pin it so it cannot regress."""
    from pathlib import Path

    doc = Path(__file__).resolve().parent.parent / "docs" / "VALIDATOR.md"
    for line in doc.read_text().splitlines():
        if "--endpoint" in line and "http" in line:
            assert "/chat/completions" in line, line
