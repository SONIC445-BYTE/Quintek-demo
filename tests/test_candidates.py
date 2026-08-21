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
from pathlib import Path

import pytest

from benchmark import candidates
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


# ---------------------------------------------------------------------------
# A router is not a model
# ---------------------------------------------------------------------------
# `openrouter/free` -- "Free Models Router" -- reached ALL THREE shortlists as
# though it were a model. It passed every capability filter, and it prices
# itself at 0/0 rather than the `-1` sentinel, so the guard that catches
# `openrouter/auto` missed it entirely. A leaderboard containing it compares
# model against model against a model-selection algorithm.

ROUTER_ENTRY = {
    "id": "openrouter/free", "name": "Free Models Router",
    "context_length": 200000,
    "architecture": {"modality": "text+image->text",
                     "input_modalities": ["text", "image"],
                     "output_modalities": ["text"], "tokenizer": "Router"},
    "pricing": {"prompt": "0", "completion": "0"},
    "supported_parameters": ["response_format", "tools", "reasoning"],
}

MODEL_ENTRY = {
    "id": "inclusionai/ling-2.6-flash", "context_length": 131072,
    "architecture": {"modality": "text->text", "input_modalities": ["text"],
                     "output_modalities": ["text"], "tokenizer": "Other"},
    "pricing": {"prompt": "0.00000001", "completion": "0.00000003"},
    "supported_parameters": ["response_format", "tools", "reasoning"],
}

ALIAS_ENTRY = {
    "id": "~z-ai/glm-latest", "context_length": 1048576,
    "alias_target": {"name": "Z.ai: GLM 5.3", "slug": "z-ai/glm-5.3"},
    "architecture": {"modality": "text->text", "input_modalities": ["text"],
                     "output_modalities": ["text"], "tokenizer": "Router"},
    "pricing": {"prompt": "0.0000014", "completion": "0.0000044"},
    "supported_parameters": ["response_format"],
}


def _serving(payload):
    entry = candidates.from_openrouter(payload)
    entry.inference_status = candidates.SERVING
    return entry


def test_a_router_is_recognised_from_what_the_catalogue_said() -> None:
    """Not from its name: a router published under another vendor's namespace
    would slip a name-based rule."""
    assert _serving(ROUTER_ENTRY).entry_kind == candidates.ENTRY_ROUTER
    assert _serving(MODEL_ENTRY).entry_kind == candidates.ENTRY_MODEL
    assert _serving(ALIAS_ENTRY).entry_kind == candidates.ENTRY_ALIAS


def test_the_zero_priced_router_is_the_one_the_price_guard_missed() -> None:
    entry = _serving(ROUTER_ENTRY)
    assert entry.price_in_per_m == 0.0, "0/0, not the -1 sentinel"
    assert entry.entry_kind == candidates.ENTRY_ROUTER


@pytest.mark.parametrize("role", sorted(candidates.ROLE_FILTERS))
def test_a_router_is_refused_by_every_role(role) -> None:
    failures = candidates.ROLE_FILTERS[role].check(_serving(ROUTER_ENTRY))
    assert failures, f"{role} admitted a router"
    assert any("not a single model" in f for f in failures)


@pytest.mark.parametrize("role", ["generation", "validation"])
def test_a_real_model_still_gets_through(role) -> None:
    """A guard that excluded everything would be worse than the bug."""
    assert candidates.ROLE_FILTERS[role].check(_serving(MODEL_ENTRY)) == []


def test_the_exclusion_can_be_switched_off_deliberately() -> None:
    """
    Routers may be worth evaluating one day -- against each OTHER, on their
    own board. Named requirement rather than a hardcoded skip, so that is a
    decision someone makes rather than an edit to this class.
    """
    router_board = candidates.Filter(name="routers", require_single_model=False,
                                     require_structured=False)
    assert router_board.check(_serving(ROUTER_ENTRY)) == []


def test_no_shipped_shortlist_contains_a_router() -> None:
    """The artifact that carried the bug."""
    path = Path("discovery/shortlists.json")
    if not path.exists():
        pytest.skip("no shortlist artifact present")
    data = json.loads(path.read_text())
    for role, items in data.items():
        offenders = [i["model_id"] for i in items
                     if i.get("entry_kind") == candidates.ENTRY_ROUTER]
        assert not offenders, f"{role} shortlist still contains {offenders}"


def test_a_provider_with_no_kind_metadata_defaults_to_model() -> None:
    """
    NVIDIA and Cerebras expose only an id. Defaulting those to ROUTER would
    empty their shortlists; defaulting to MODEL matches what those catalogues
    actually contain.
    """
    entry = candidates.from_bare("nvidia", {"id": "meta/llama-3.1-8b-instruct"})
    assert entry.entry_kind == candidates.ENTRY_MODEL


# ---------------------------------------------------------------------------
# Consuming a discovery snapshot
# ---------------------------------------------------------------------------
# The external agent records what a catalogue contained and judges nothing.
# Quintek reads that and applies ROLE_FILTERS. These tests pin the seam: the
# loader must not acquire an opinion, and the filters must still have one.

SNAPSHOT = {
    "schema_version": "catalogue_entry/0.1.0",
    "normalizer_version": "0.1.0",
    "observed_at": "2026-08-21T17:28:43Z",
    "entries": [
        {"model_id": "a/model", "entry_kind": "MODEL", "context_length": 131072,
         "input_modalities": ["text"], "output_modalities": ["text"],
         "supported_parameters": ["response_format", "reasoning", "tools"],
         "price_in_per_m_usd": 0.01, "price_out_per_m_usd": 0.03,
         "source": "openrouter", "observed_at": "2026-08-21T17:28:43Z"},
        {"model_id": "openrouter/free", "entry_kind": "ROUTER",
         "context_length": 200000, "input_modalities": ["text", "image"],
         "output_modalities": ["text"],
         "supported_parameters": ["response_format", "reasoning"],
         "price_in_per_m_usd": 0.0, "price_out_per_m_usd": 0.0,
         "source": "openrouter"},
        {"model_id": "~v/latest", "entry_kind": "ALIAS", "alias_target": "v/1.0",
         "context_length": 8192, "input_modalities": ["text"],
         "output_modalities": ["text"], "supported_parameters": [],
         "source": "openrouter"},
    ],
}


def _snapshot_file(tmp_path, payload=None):
    path = tmp_path / "openrouter.normalized.json"
    path.write_text(json.dumps(payload or SNAPSHOT))
    return path


def test_a_snapshot_loads_every_entry_including_the_ones_we_will_reject(tmp_path):
    """
    Dropping routers at load time would recreate the problem the split exists
    to solve: a catalogue already filtered cannot be re-read under different
    rules.
    """
    entries = candidates.load_discovery_snapshot(_snapshot_file(tmp_path))
    assert len(entries) == 3
    assert {e.entry_kind for e in entries} == {"MODEL", "ROUTER", "ALIAS"}


def test_the_loader_applies_no_rule_of_its_own(tmp_path):
    entries = candidates.load_discovery_snapshot(_snapshot_file(tmp_path))
    # The router is present after loading...
    assert any(e.entry_kind == "ROUTER" for e in entries)
    # ...and refused only when a FILTER is applied.
    for entry in entries:
        entry.inference_status = SERVING
    passing = [e for e in entries if not ROLE_FILTERS["generation"].check(e)]
    assert [e.model_id for e in passing] == ["a/model"]


def test_capability_is_derived_here_not_in_the_snapshot(tmp_path):
    """
    The snapshot records `supported_parameters` verbatim. What counts as
    "supports structured output" is Quintek's reading of that list, so it is
    computed on this side of the boundary.
    """
    entries = candidates.load_discovery_snapshot(_snapshot_file(tmp_path))
    by_id = {e.model_id: e for e in entries}
    assert by_id["a/model"].supports_structured is True
    assert by_id["a/model"].supports_reasoning is True


def test_an_empty_parameter_list_is_unknown_not_false(tmp_path):
    """"We do not know" must not read as "no"."""
    entries = candidates.load_discovery_snapshot(_snapshot_file(tmp_path))
    alias = next(e for e in entries if e.entry_kind == "ALIAS")
    assert alias.supports_structured is None


def test_provenance_survives_the_load(tmp_path):
    entries = candidates.load_discovery_snapshot(_snapshot_file(tmp_path))
    assert all(e.observed_at for e in entries)


def test_a_snapshot_of_the_wrong_shape_says_so(tmp_path):
    """Rather than yielding an empty list that reads as 'no models exist'."""
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"models": []}))
    with pytest.raises(ValueError) as exc:
        candidates.load_discovery_snapshot(path)
    assert "refusing to guess" in str(exc.value)


def test_the_real_snapshot_loads_if_one_is_checked_out(tmp_path):
    """
    Exercised against the actual artifact when the discovery repository is
    present beside this one; skipped otherwise, since Quintek must never
    REQUIRE it to be.
    """
    root = Path("/home/user/registry-repo/snapshots/openrouter")
    if not root.exists():
        pytest.skip("discovery repository not checked out here")
    newest = sorted(root.iterdir())[-1] / "openrouter.normalized.json"
    entries = candidates.load_discovery_snapshot(newest)
    assert len(entries) > 100
    assert any(e.entry_kind == "ROUTER" for e in entries), (
        "the snapshot should still contain routers -- filtering is our job")
