"""
Tests for the candidate funnel.

The funnel's job is to reduce ~518 catalogue entries to a benchmarkable
shortlist without deciding the benchmark's outcome in advance. So the tests
are mostly about what must NOT get through: unverified entries, models whose
capabilities are merely unstated, and anything selected for a reason that is
really a proxy for reputation.
"""

from __future__ import annotations

import json

import pytest

from benchmark.candidates import (BILLING_BLOCKED, MODEL_UNAVAILABLE, ROLE_FILTERS, SERVING,
                                  TIMEOUT, UNVERIFIED, CatalogueEntry, Filter, apply_filter,
                                  apply_probe_results, diversify, from_openrouter,
                                  load_catalogue, rank_for_shortlist)


def entry(**kwargs):
    base = dict(provider="p", model_id="m", context_length=128_000,
                supports_structured=True, supports_reasoning=True, supports_vision=True,
                supports_tools=True, output_modalities=["text"],
                price_in_per_m=0.05, price_out_per_m=0.2,
                inference_status=SERVING)
    base.update(kwargs)
    return CatalogueEntry(**base)


# ---------------------------------------------------------------------------
# Catalogue presence is not availability
# ---------------------------------------------------------------------------

def test_an_unverified_entry_never_reaches_a_shortlist():
    """A shortlist admitting these hands the benchmark a queue of 404s."""
    result = apply_filter([entry(inference_status=UNVERIFIED)], ROLE_FILTERS["generation"])
    assert result.size == 0
    assert "inference not confirmed" in result.rejected[0]["reasons"][0]


@pytest.mark.parametrize("status", [MODEL_UNAVAILABLE, BILLING_BLOCKED, TIMEOUT, UNVERIFIED])
def test_only_a_confirmed_serving_entry_passes(status):
    assert apply_filter([entry(inference_status=status)],
                        ROLE_FILTERS["generation"]).size == 0
    assert apply_filter([entry(inference_status=SERVING)],
                        ROLE_FILTERS["generation"]).size == 1


def test_billing_blocked_is_recheckable_not_permanent():
    """
    402 is an account state, not a model property. Encoding it as "this model
    is bad" would survive the bill being paid.
    """
    blocked = entry(inference_status=BILLING_BLOCKED)
    assert blocked.confirmed is False
    assert blocked.recheckable is True
    # A hard 404 is not recheckable in the same sense.
    assert entry(inference_status=MODEL_UNAVAILABLE).recheckable is False


def test_a_probe_records_when_it_was_observed():
    candidate = entry(inference_status=UNVERIFIED)
    candidate.record_probe(BILLING_BLOCKED, reason="payment required")
    assert candidate.observed_at
    assert candidate.inference_reason == "payment required"


# ---------------------------------------------------------------------------
# Unknown is not yes
# ---------------------------------------------------------------------------

def test_unstated_capabilities_are_not_treated_as_supported():
    """NVIDIA publishes only an id. That is not evidence of anything."""
    bare = entry(supports_structured=None, context_length=None)
    failures = ROLE_FILTERS["generation"].check(bare)
    assert any("not exposed" in f for f in failures)


def test_unknown_capabilities_can_be_admitted_deliberately():
    bare = entry(supports_structured=None, context_length=None)
    permissive = Filter("x", require_structured=True, min_context=32_000,
                        allow_unknown_capabilities=True)
    assert permissive.check(bare) == []


def test_an_unstated_price_cannot_be_shown_to_be_within_budget():
    unpriced = entry(price_in_per_m=None)
    spec = Filter("x", max_price_in_per_m=1.0)
    assert any("not stated" in f for f in spec.check(unpriced))


# ---------------------------------------------------------------------------
# The two bugs found by running this against the live catalogue
# ---------------------------------------------------------------------------

def test_a_negative_sentinel_price_is_not_read_as_the_cheapest_model():
    """
    OpenRouter uses -1 for request-time pricing. Read literally it is the
    cheapest number in the catalogue, which sorted the router aliases to the
    top of every shortlist.
    """
    parsed = from_openrouter({"id": "openrouter/auto", "context_length": 2_000_000,
                              "pricing": {"prompt": "-1", "completion": "-1"},
                              "architecture": {"input_modalities": ["text"],
                                               "output_modalities": ["text"]},
                              "supported_parameters": ["response_format"]})
    assert parsed.price_in_per_m is None
    assert parsed.price_out_per_m is None


def test_unpriced_entries_sort_last_not_first():
    ranked = rank_for_shortlist([entry(model_id="unpriced", price_in_per_m=None),
                                 entry(model_id="cheap", price_in_per_m=0.01)])
    assert [e.model_id for e in ranked] == ["cheap", "unpriced"]


def test_a_model_that_cannot_emit_text_is_rejected():
    """
    Several catalogue entries are image or audio generators that pass every
    other filter. Quintek's pipeline reads text.
    """
    audio_only = entry(output_modalities=["audio"])
    assert any("not text" in f for f in ROLE_FILTERS["generation"].check(audio_only))
    assert ROLE_FILTERS["generation"].check(entry(output_modalities=["text", "audio"])) == []


# ---------------------------------------------------------------------------
# Role filters
# ---------------------------------------------------------------------------

def test_validation_requires_reasoning_and_generation_does_not():
    no_reasoning = entry(supports_reasoning=False)
    assert ROLE_FILTERS["generation"].check(no_reasoning) == []
    assert any("reasoning" in f for f in ROLE_FILTERS["validation"].check(no_reasoning))


def test_vision_role_requires_vision():
    assert any("vision" in f
               for f in ROLE_FILTERS["vision"].check(entry(supports_vision=False)))


def test_every_role_filter_requires_structured_output_and_confirmed_inference():
    for name, spec in ROLE_FILTERS.items():
        assert spec.require_structured is True, name
        assert spec.require_confirmed_inference is True, name


def test_a_short_context_model_is_rejected_for_text_roles():
    assert any("context" in f
               for f in ROLE_FILTERS["generation"].check(entry(context_length=8_192)))


# ---------------------------------------------------------------------------
# Ranking must not smuggle in a quality judgement
# ---------------------------------------------------------------------------

def test_ranking_uses_only_objective_fields():
    """
    Ordering by anything that proxies for reputation would decide the
    benchmark before running it.
    """
    entries = [entry(model_id="famous-flagship", price_in_per_m=5.0),
               entry(model_id="obscure", price_in_per_m=0.01)]
    assert [e.model_id for e in rank_for_shortlist(entries)][0] == "obscure"


def test_diversify_caps_one_family_dominating_the_shortlist():
    entries = [entry(model_id=f"acme/model-{i}", price_in_per_m=0.01 * i)
               for i in range(8)]
    assert len(diversify(entries, per_family=3)) == 3


def test_diversify_caps_one_provider_dominating():
    entries = [entry(provider="p", model_id=f"fam{i}/m") for i in range(20)]
    assert len(diversify(entries, per_provider=5, per_family=99)) == 5


# ---------------------------------------------------------------------------
# The shipped discovery data
# ---------------------------------------------------------------------------

def test_the_live_catalogue_loads_and_starts_unverified():
    entries = load_catalogue("discovery/catalogue_raw.json")
    assert len(entries) > 500
    assert all(e.inference_status == UNVERIFIED for e in entries)
    assert {e.provider for e in entries} == {"nvidia", "cerebras", "openrouter"}


def test_probe_results_merge_onto_the_catalogue():
    entries = load_catalogue("discovery/catalogue_raw.json")
    updated = apply_probe_results(entries, "discovery/nvidia_inference.json")
    assert updated == 102
    serving = [e for e in entries if e.provider == "nvidia" and e.confirmed]
    assert serving, "the probe recorded no serving NVIDIA models"
    assert all(e.observed_at for e in serving)


def test_cerebras_is_recorded_as_billing_blocked_not_as_a_bad_model():
    entries = load_catalogue("discovery/catalogue_raw.json")
    apply_probe_results(entries, "discovery/cerebras_inference.json")
    cerebras = [e for e in entries if e.provider == "cerebras"]
    assert len(cerebras) == 2
    assert all(e.inference_status == BILLING_BLOCKED for e in cerebras)
    assert all(e.recheckable for e in cerebras)
