"""
Dynamic provider/model discovery and reconciliation.

Every provider interaction here goes through a fake transport. Discovery logic
must be testable without spending a credit, and the cases worth testing -- a
410, a 402, a model reappearing after two absences -- are exactly the ones a
real endpoint will not produce on request.

The scenarios are numbered to match the requirement list they were written
against, so a reader can check coverage without reading every assertion.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from benchmark.discovery import (Availability, CredentialInRegistry, DiscoveryPolicy,
                                 DynamicModelRegistry, Observation, Pricing,
                                 EVENT_RETIRED, price_state)
from benchmark.provider_catalogue import (CatalogueSource, HttpResult,
                                          MissingCredential, SOURCES, Transport,
                                          fetch_catalogue, probe)
from benchmark.provider_status import ProviderStatus, classify
from benchmark.quintek_router import Candidate, QuintekRouter

T0 = "2026-08-01T00:00:00Z"
T1 = "2026-08-02T00:00:00Z"
T2 = "2026-08-03T00:00:00Z"
T3 = "2026-08-04T00:00:00Z"

NVIDIA_410 = ('{"type":"about:blank","title":"Gone","status":410,"detail":"The model '
              "'meta/llama-3.1-8b-instruct' has reached its end of life on "
              '2026-08-26T09:00:00Z and is no longer available."}')
NVIDIA_404 = ('{"status":404,"title":"Not Found","detail":"Function \'9b96341b\': '
              'Not found for account \'lq1Z\'"}')


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeTransport(Transport):
    """
    Scripted HTTP. `catalogue` is returned for GETs; `completions` maps a model
    id to what POSTing to it produces.
    """

    def __init__(self, *, catalogue=None, completions=None, catalogue_status=200):
        self.catalogue = catalogue if catalogue is not None else {"data": []}
        self.completions = completions or {}
        self.catalogue_status = catalogue_status
        self.gets, self.posts = [], []

    def get(self, url, *, headers, timeout):
        self.gets.append((url, headers))
        if self.catalogue_status != 200:
            return HttpResult(status=self.catalogue_status, body="upstream is unwell",
                              error=f"HTTP {self.catalogue_status}")
        return HttpResult(status=200, body=json.dumps(self.catalogue), latency_ms=12.0)

    def post_json(self, url, *, headers, payload, timeout):
        self.posts.append((url, headers, payload))
        outcome = self.completions.get(payload["model"])
        if outcome is None:
            return HttpResult(status=200, body='{"choices":[]}', latency_ms=40.0)
        if isinstance(outcome, BaseException):
            return HttpResult(status=None, body="", latency_ms=60_000.0, error=outcome)
        status, body = outcome
        return HttpResult(status=status, body=body, latency_ms=30.0,
                          error=None if status < 300 else f"HTTP {status}: {body}")


def nvidia_source():
    return SOURCES["nvidia"]


def catalogue_of(*ids):
    return {"data": [{"id": model_id, "object": "model"} for model_id in ids]}


def observations(*ids, provider="nvidia", **kwargs):
    return [Observation(provider=provider, model_id=i, **kwargs) for i in ids]


def registry(tmp_path, **policy_kwargs):
    return DynamicModelRegistry(tmp_path / "registry.json",
                                policy=DiscoveryPolicy(**policy_kwargs))


# ---------------------------------------------------------------------------
# 1-2. A provider adds and removes a model
# ---------------------------------------------------------------------------

def test_1_a_provider_adding_a_model_is_recorded_as_a_first_sighting(tmp_path):
    reg = registry(tmp_path)
    report = reg.reconcile("nvidia", observations("a", "b"), at=T0)
    assert sorted(report.discovered) == ["nvidia:a", "nvidia:b"]
    assert reg.get("nvidia:a").first_seen == T0
    assert reg.get("nvidia:a").last_seen == T0


def test_1b_a_catalogue_listing_never_makes_a_model_available(tmp_path):
    """
    The invariant the whole module rests on: NVIDIA listed 83 models on
    2026-08-28 and several of them 404ed or hung on inference.
    """
    reg = registry(tmp_path)
    reg.reconcile("nvidia", observations("a"), at=T0)
    assert reg.get("nvidia:a").availability == Availability.UNVERIFIED
    kept, dropped = reg.eligible()
    assert kept == []
    assert "UNVERIFIED" in dropped[0]["reasons"][0]


def test_2_one_disappearance_is_absence_not_retirement(tmp_path):
    """
    Measured: two models SERVING in the 2026-08-20 snapshot were missing from
    the 2026-08-28 listing and still answered 200 when called.
    """
    reg = registry(tmp_path, absences_before_retired=2)
    reg.reconcile("nvidia", observations("a", "b"), at=T0)
    report = reg.reconcile("nvidia", observations("a"), at=T1)
    assert report.absent == ["nvidia:b"]
    assert report.retired == []
    assert reg.get("nvidia:b").availability != Availability.RETIRED
    assert reg.get("nvidia:b").catalogue_present is False


def test_2b_sustained_absence_retires_the_model(tmp_path):
    reg = registry(tmp_path, absences_before_retired=2)
    reg.reconcile("nvidia", observations("a", "b"), at=T0)
    reg.reconcile("nvidia", observations("a"), at=T1)
    report = reg.reconcile("nvidia", observations("a"), at=T2)
    assert report.retired == ["nvidia:b"]
    record = reg.get("nvidia:b")
    assert record.availability == Availability.RETIRED
    assert record.retired_at == T2
    assert "consecutive" in record.retirement_reason


# ---------------------------------------------------------------------------
# 3-7. What each provider answer means
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status, body, expected", [
    (410, NVIDIA_410, Availability.RETIRED),
    (404, NVIDIA_404, Availability.NOT_SERVING),
    (402, '{"detail":"payment required"}', Availability.BILLING_BLOCKED),
    (429, "too many requests", Availability.RATE_LIMITED),
    (401, "invalid api key", Availability.AUTH_FAILED),
    (200, '{"choices":[{"message":{"content":"ok"}}]}', Availability.AVAILABLE),
])
def test_3_to_7_probe_outcomes_map_to_availability(tmp_path, status, body, expected):
    reg = registry(tmp_path)
    reg.reconcile("nvidia", observations("m"), at=T0)
    transport = FakeTransport(completions={"m": (status, body)})
    result = probe(nvidia_source(), "m", transport=transport,
                   env={"NVIDIA_API_KEY": "x"})
    reg.record_probe("nvidia:m", error=result.error, http_status=result.http_status,
                     latency_ms=result.latency_ms, at=T1)
    assert reg.get("nvidia:m").availability == expected


def test_3b_a_410_is_terminal_and_names_its_evidence(tmp_path):
    reg = registry(tmp_path)
    reg.reconcile("nvidia", observations("meta/llama-3.1-8b-instruct"), at=T0)
    reg.record_probe("nvidia:meta/llama-3.1-8b-instruct",
                     error=f"HTTP 410: {NVIDIA_410}", http_status=410, at=T1)
    record = reg.get("nvidia:meta/llama-3.1-8b-instruct")
    assert record.retired is True
    assert record.recheckable is False
    assert "410" in record.retirement_reason
    assert any(e["kind"] == EVENT_RETIRED for e in record.history)


def test_3c_a_404_is_not_a_410(tmp_path):
    """
    'Not found for this account' is an entitlement fact a billing change
    reverses. 'End of life' is not. Collapsing them puts a dead model on a
    permanent re-probe schedule and keeps a live one out of the pool.
    """
    assert classify(NVIDIA_404, http_status=404) == ProviderStatus.MODEL_UNAVAILABLE
    assert classify(NVIDIA_410, http_status=410) == ProviderStatus.MODEL_RETIRED
    reg = registry(tmp_path)
    reg.reconcile("nvidia", observations("x"), at=T0)
    reg.record_probe("nvidia:x", error=NVIDIA_404, http_status=404, at=T1)
    assert reg.get("nvidia:x").recheckable is True


def test_5_a_timeout_is_temporary_not_terminal(tmp_path):
    reg = registry(tmp_path)
    reg.reconcile("nvidia", observations("slow"), at=T0)
    transport = FakeTransport(completions={"slow": TimeoutError("read timed out")})
    result = probe(nvidia_source(), "slow", transport=transport,
                   env={"NVIDIA_API_KEY": "x"})
    reg.record_probe("nvidia:slow", error=result.error,
                     http_status=result.http_status, at=T1)
    record = reg.get("nvidia:slow")
    assert record.availability == Availability.TEMPORARILY_UNAVAILABLE
    assert record.recheckable is True


def test_7_a_model_that_recovers_becomes_available_again(tmp_path):
    reg = registry(tmp_path)
    reg.reconcile("nvidia", observations("m"), at=T0)
    reg.record_probe("nvidia:m", error="429 rate limit", http_status=429, at=T1)
    assert reg.get("nvidia:m").availability == Availability.RATE_LIMITED
    reg.record_probe("nvidia:m", http_status=200, latency_ms=41.0, at=T2)
    record = reg.get("nvidia:m")
    assert record.availability == Availability.AVAILABLE
    assert record.consecutive_failures == 0
    assert record.latency_ms_best == 41.0


# ---------------------------------------------------------------------------
# 8. Catalogue says available, inference fails
# ---------------------------------------------------------------------------

def test_8_catalogue_presence_does_not_survive_a_failed_probe(tmp_path):
    reg = registry(tmp_path)
    reg.reconcile("nvidia", observations("listed-but-dead"), at=T0)
    reg.record_probe("nvidia:listed-but-dead", error=NVIDIA_410, http_status=410, at=T1)
    record = reg.get("nvidia:listed-but-dead")
    # Still listed. Still gone.
    assert record.catalogue_present is False   # retirement clears the listing flag
    assert record.retired is True
    kept, _ = reg.eligible()
    assert record not in kept


# ---------------------------------------------------------------------------
# 9-10. Unknown is not yes, and unknown does not win a price sort
# ---------------------------------------------------------------------------

def test_9_unknown_capabilities_are_not_eligible_but_are_not_deleted(tmp_path):
    reg = registry(tmp_path)
    reg.reconcile("nvidia", observations("bare"), at=T0)
    reg.record_probe("nvidia:bare", http_status=200, at=T1)
    kept, dropped = reg.eligible(required_capabilities=("reasoning",))
    assert kept == []
    assert "unknown, and unknown is not yes" in dropped[0]["reasons"][0]
    # Not permanently excluded: still in the registry, still probeable.
    assert reg.get("nvidia:bare") is not None
    kept, _ = reg.eligible(required_capabilities=("reasoning",),
                           allow_unknown_capabilities=True)
    assert [r.key for r in kept] == ["nvidia:bare"]


def test_10_unknown_and_unpriced_cannot_beat_a_priced_model(tmp_path):
    reg = registry(tmp_path)
    reg.reconcile("openrouter", [
        Observation(provider="openrouter", model_id="priced", input_price=5.0),
        Observation(provider="openrouter", model_id="sentinel", input_price=-1.0,
                    price_stated=False),
        Observation(provider="openrouter", model_id="silent", input_price=None),
    ], at=T0)
    for key in ("openrouter:priced", "openrouter:sentinel", "openrouter:silent"):
        reg.record_probe(key, http_status=200, at=T1)
    assert reg.get("openrouter:priced").pricing_status == Pricing.PAID
    assert reg.get("openrouter:sentinel").pricing_status == Pricing.UNPRICED
    assert reg.get("openrouter:silent").pricing_status == Pricing.UNKNOWN

    order = [r.key for r in reg.shortlist()]
    assert order[0] == "openrouter:priced", order

    # And an unpriced entry cannot be shown to be within a budget.
    kept, dropped = reg.eligible(max_input_price=10.0)
    assert [r.key for r in kept] == ["openrouter:priced"]
    assert {row["key"] for row in dropped} == {"openrouter:sentinel", "openrouter:silent"}


def test_10b_a_real_zero_is_free_and_a_sentinel_is_not():
    assert price_state(0.0) == Pricing.FREE
    assert price_state(-1.0) == Pricing.UNPRICED
    assert price_state(None) == Pricing.UNKNOWN
    assert price_state(None, stated=False) == Pricing.UNPRICED
    assert price_state(2.5) == Pricing.PAID


# ---------------------------------------------------------------------------
# 11-12. A retired model leaves production; production replaces it
# ---------------------------------------------------------------------------

def test_11_a_retired_model_is_never_selected(tmp_path):
    reg = registry(tmp_path)
    reg.reconcile("nvidia", observations("dead", "alive"), at=T0)
    reg.record_probe("nvidia:dead", error=NVIDIA_410, http_status=410, at=T1)
    reg.record_probe("nvidia:alive", http_status=200, at=T1)

    router = QuintekRouter(
        [Candidate("nvidia:dead", "nvidia", "dead", {"medical_qa"}),
         Candidate("nvidia:alive", "nvidia", "alive", {"medical_qa"})],
        model_registry=reg, required_capabilities={"question_generation": ("medical_qa",)})
    decision = router.route("question_generation")
    assert decision.selected == "nvidia:alive"
    dropped = [c for c in decision.considered if c["key"] == "nvidia:dead"][0]
    assert dropped["dropped_at"] == "layer0_retired"
    # A withdrawal is not a quality signal.
    assert dropped["environmental"] is True


def test_12_production_replaces_a_retired_model_without_a_deployment(tmp_path):
    """
    The whole point. The candidate set and the code are unchanged between
    these two routes; only the registry file moved, which a cron discovery run
    writes.
    """
    reg = registry(tmp_path)
    reg.reconcile("nvidia", observations("first", "second"), at=T0)
    for key in ("nvidia:first", "nvidia:second"):
        reg.record_probe(key, http_status=200, at=T0)
    candidates = [Candidate("nvidia:first", "nvidia", "first", {"medical_qa"}),
                  Candidate("nvidia:second", "nvidia", "second", {"medical_qa"})]
    required = {"question_generation": ("medical_qa",)}

    before = QuintekRouter(candidates, model_registry=reg,
                           required_capabilities=required).route("question_generation")
    assert before.selected in {"nvidia:first", "nvidia:second"}

    reg.record_probe(f"{before.selected}", error=NVIDIA_410, http_status=410, at=T2)
    after = QuintekRouter(candidates, model_registry=reg,
                          required_capabilities=required).route("question_generation")
    assert after.selected != before.selected
    assert after.selected is not None


# ---------------------------------------------------------------------------
# 13. A frozen experiment does NOT silently replace a retired model
# ---------------------------------------------------------------------------

def test_13_a_frozen_experiment_is_reported_blocked_not_repointed(tmp_path):
    reg = registry(tmp_path)
    reg.reconcile("nvidia", observations("meta/llama-3.1-8b-instruct",
                                         "meta/llama-3.1-70b-instruct",
                                         "openai/gpt-oss-20b"), at=T0)
    reg.record_probe("nvidia:meta/llama-3.1-8b-instruct", error=NVIDIA_410,
                     http_status=410, at=T1)
    reg.record_probe("nvidia:meta/llama-3.1-70b-instruct", error=NVIDIA_410,
                     http_status=410, at=T1)
    reg.record_probe("nvidia:openai/gpt-oss-20b", http_status=200, at=T1)

    frozen = [{"seat": "candidate", "provider": "nvidia",
               "model": "meta/llama-3.1-8b-instruct"},
              {"seat": "judge", "provider": "nvidia",
               "model": "meta/llama-3.1-70b-instruct"}]
    blocked = reg.blocked_experiment_models(frozen)
    assert {row["seat"] for row in blocked} == {"candidate", "judge"}
    assert all(row["terminal"] for row in blocked)
    # It reports. It does not offer, name, or imply a replacement.
    assert all("gpt-oss" not in json.dumps(row) for row in blocked)


def test_13b_a_healthy_frozen_experiment_is_not_blocked(tmp_path):
    reg = registry(tmp_path)
    reg.reconcile("nvidia", observations("a", "b"), at=T0)
    reg.record_probe("nvidia:a", http_status=200, at=T1)
    reg.record_probe("nvidia:b", http_status=200, at=T1)
    assert reg.blocked_experiment_models(
        [{"seat": "candidate", "provider": "nvidia", "model": "a"},
         {"seat": "judge", "provider": "nvidia", "model": "b"}]) == []


# ---------------------------------------------------------------------------
# 14. History survives retirement
# ---------------------------------------------------------------------------

def test_14_a_retired_model_stays_queryable_with_its_whole_history(tmp_path):
    reg = registry(tmp_path)
    reg.reconcile("nvidia", observations("m"), at=T0)
    reg.record_probe("nvidia:m", http_status=200, latency_ms=100.0, at=T1)
    reg.record_probe("nvidia:m", error=NVIDIA_410, http_status=410, at=T2)
    reg.save()

    reopened = DynamicModelRegistry(tmp_path / "registry.json")
    record = reopened.get("nvidia:m")
    assert record is not None
    assert record.first_seen == T0
    assert record.probe_successes == 1        # the good day is still on the record
    assert record.latency_ms_best == 100.0
    assert record.retired_at == T2
    assert [e["kind"] for e in record.history][-1] == EVENT_RETIRED
    assert reopened.retired()[0].key == "nvidia:m"


# ---------------------------------------------------------------------------
# 15. Discovery requires no redeployment
# ---------------------------------------------------------------------------

def test_15_the_routable_set_changes_with_the_file_not_the_source(tmp_path):
    path = tmp_path / "registry.json"
    first = DynamicModelRegistry(path)
    first.reconcile("nvidia", observations("old"), at=T0)
    first.record_probe("nvidia:old", http_status=200, at=T0)
    first.save()
    assert [r.key for r in DynamicModelRegistry(path).shortlist()] == ["nvidia:old"]

    second = DynamicModelRegistry(path)
    second.reconcile("nvidia", observations("old", "new"), at=T1)
    second.record_probe("nvidia:new", http_status=200, at=T1)
    second.save()
    assert [r.key for r in DynamicModelRegistry(path).shortlist()] == [
        "nvidia:new", "nvidia:old"]


def test_15b_a_missing_config_file_uses_documented_defaults(tmp_path):
    assert DiscoveryPolicy.load(tmp_path / "nope.json") == DiscoveryPolicy()


def test_15c_a_misspelled_interval_is_refused_rather_than_ignored(tmp_path):
    path = tmp_path / "discovery.json"
    path.write_text(json.dumps({"catalog_refresh_seconds": 60}))
    with pytest.raises(ValueError, match="not a discovery setting"):
        DiscoveryPolicy.load(path)


# ---------------------------------------------------------------------------
# 16-18. Outage handling and wasted calls
# ---------------------------------------------------------------------------

def test_16_a_provider_outage_does_not_reconcile_an_empty_catalogue(tmp_path):
    """
    The dangerous failure. A 503 yields zero observations; reconciling against
    that would mark every model absent, and two such runs would retire the
    provider's entire catalogue.
    """
    result = fetch_catalogue(nvidia_source(),
                             transport=FakeTransport(catalogue_status=503),
                             env={"NVIDIA_API_KEY": "x"})
    assert result.ok is False
    assert result.observations == []
    assert "503" in result.error


def test_17_a_failed_probe_backs_off_further_each_time(tmp_path):
    reg = registry(tmp_path, failed_backoff_seconds=100.0,
                   failed_backoff_multiplier=2.0, failed_backoff_max_seconds=500.0)
    assert reg.policy.backoff_for(1) == 100.0
    assert reg.policy.backoff_for(2) == 200.0
    assert reg.policy.backoff_for(3) == 400.0
    assert reg.policy.backoff_for(9) == 500.0        # capped


def test_18_a_retired_model_is_never_scheduled_for_another_probe(tmp_path):
    reg = registry(tmp_path)
    reg.reconcile("nvidia", observations("dead", "alive"), at=T0)
    reg.record_probe("nvidia:dead", error=NVIDIA_410, http_status=410, at=T0)
    reg.record_probe("nvidia:alive", http_status=200, at=T0)
    later = datetime(2027, 1, 1, tzinfo=timezone.utc)
    due = {r.key for r in reg.due_for_recheck(now=later)}
    assert "nvidia:dead" not in due
    assert "nvidia:alive" in due


def test_18b_an_operator_can_opt_into_rechecking_retired_models(tmp_path):
    reg = registry(tmp_path, retired_recheck_seconds=60.0)
    reg.reconcile("nvidia", observations("dead"), at=T0)
    reg.record_probe("nvidia:dead", error=NVIDIA_410, http_status=410, at=T0)
    later = datetime(2027, 1, 1, tzinfo=timezone.utc)
    assert [r.key for r in reg.due_for_recheck(now=later)] == ["nvidia:dead"]


def test_18c_a_recently_verified_model_is_not_reprobed(tmp_path):
    reg = registry(tmp_path, availability_recheck_seconds=86_400.0)
    reg.reconcile("nvidia", observations("m"), at=T0)
    just_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    reg.record_probe("nvidia:m", http_status=200, at=just_now)
    soon = datetime.now(timezone.utc) + timedelta(minutes=5)
    assert reg.due_for_recheck(now=soon) == []


# ---------------------------------------------------------------------------
# 20-22. Identity, provenance and exploration
# ---------------------------------------------------------------------------

def test_20_two_models_from_different_providers_stay_distinguishable(tmp_path):
    reg = registry(tmp_path)
    reg.reconcile("nvidia", observations("meta/llama-3.1-8b-instruct"), at=T0)
    reg.reconcile("openrouter", observations("meta/llama-3.1-8b-instruct",
                                             provider="openrouter"), at=T0)
    assert len(reg.all()) == 2
    reg.record_probe("nvidia:meta/llama-3.1-8b-instruct", error=NVIDIA_410,
                     http_status=410, at=T1)
    reg.record_probe("openrouter:meta/llama-3.1-8b-instruct", http_status=200, at=T1)
    assert reg.get("nvidia:meta/llama-3.1-8b-instruct").retired is True
    assert reg.get("openrouter:meta/llama-3.1-8b-instruct").retired is False


def test_21_a_reappearing_model_returns_to_unverified_not_available(tmp_path):
    """
    Retired on absence alone, then listed again. The catalogue contradicts the
    retirement, so the record reopens -- but at UNVERIFIED, because the thing
    that retired it was never a probe.
    """
    reg = registry(tmp_path, absences_before_retired=1)
    reg.reconcile("nvidia", observations("m"), at=T0)
    reg.reconcile("nvidia", [], at=T1)
    assert reg.get("nvidia:m").retired is True

    report = reg.reconcile("nvidia", observations("m"), at=T2)
    assert report.returned == ["nvidia:m"]
    record = reg.get("nvidia:m")
    assert record.availability == Availability.UNVERIFIED
    assert record.retired_at == ""


def test_21b_a_410_retirement_is_not_undone_by_a_catalogue_listing(tmp_path):
    """
    The other direction, and the one that matters: NVIDIA went on listing
    models it had already retired. A listing must not resurrect a model the
    provider told us, in so many words, is gone.
    """
    reg = registry(tmp_path)
    reg.reconcile("nvidia", observations("m"), at=T0)
    reg.record_probe("nvidia:m", error=NVIDIA_410, http_status=410, at=T1)
    reg.reconcile("nvidia", observations("m"), at=T2)
    assert reg.get("nvidia:m").retired is True


def test_22_a_newly_discovered_model_enters_controlled_verification(tmp_path):
    """
    UNVERIFIED -> probe -> AVAILABLE -> eligible. Not eligible before the
    probe, and not permanently barred either.
    """
    reg = registry(tmp_path)
    reg.reconcile("nvidia", observations("new"), at=T0)
    assert reg.eligible()[0] == []
    assert reg.get("nvidia:new") in reg.due_for_recheck()
    reg.record_probe("nvidia:new", http_status=200, at=T1)
    assert [r.key for r in reg.eligible()[0]] == ["nvidia:new"]


# ---------------------------------------------------------------------------
# 23. No selection depends on a model or vendor name
# ---------------------------------------------------------------------------

def test_23_no_selection_depends_on_a_model_or_vendor_name(tmp_path):
    """
    Rename every model and provider to opaque strings and require the same
    decision. A filter that consulted reputation would change its answer.
    """
    def build(path, names):
        reg = DynamicModelRegistry(path)
        reg.reconcile(names["provider"], [
            Observation(provider=names["provider"], model_id=names["cheap"],
                        input_price=1.0, context_window=100_000,
                        capabilities={"reasoning": True, "structured_output": True}),
            Observation(provider=names["provider"], model_id=names["dear"],
                        input_price=9.0, context_window=100_000,
                        capabilities={"reasoning": True, "structured_output": True}),
        ], at=T0)
        for model in (names["cheap"], names["dear"]):
            reg.record_probe(f"{names['provider']}:{model}", http_status=200, at=T1)
        return reg

    famous = build(tmp_path / "a.json", {
        "provider": "nvidia", "cheap": "meta/llama-3.1-8b-instruct",
        "dear": "openai/gpt-oss-120b"})
    opaque = build(tmp_path / "b.json", {
        "provider": "p0", "cheap": "m0", "dear": "m1"})

    famous_order = [r.model_id for r in famous.shortlist(
        required_capabilities=("reasoning",))]
    opaque_order = [r.model_id for r in opaque.shortlist(
        required_capabilities=("reasoning",))]
    # Same positions, different names: the ordering came from price and
    # context, not from who made the model.
    assert famous_order == ["meta/llama-3.1-8b-instruct", "openai/gpt-oss-120b"]
    assert opaque_order == ["m0", "m1"]


def test_23b_a_router_entry_is_not_a_model(tmp_path):
    reg = registry(tmp_path)
    reg.reconcile("openrouter", [
        Observation(provider="openrouter", model_id="openrouter/free",
                    input_price=0.0, entry_kind="ROUTER"),
    ], at=T0)
    reg.record_probe("openrouter:openrouter/free", http_status=200, at=T1)
    kept, dropped = reg.eligible()
    assert kept == []
    assert "not a single model" in dropped[0]["reasons"][0]


# ---------------------------------------------------------------------------
# 24. No secret reaches a stored file
# ---------------------------------------------------------------------------

def test_24_a_credential_shaped_value_cannot_be_written_to_the_registry(tmp_path):
    reg = registry(tmp_path)
    reg.reconcile("nvidia", observations("m"), at=T0)
    reg.record_probe("nvidia:m",
                     error="401 rejected key nvapi-abcdefghijklmnopqrstuvwxyz012345",
                     http_status=401, at=T1)
    with pytest.raises(CredentialInRegistry, match="shaped like a credential"):
        reg.save()


def test_24b_the_registry_records_the_variable_name_never_the_value(tmp_path):
    reg = registry(tmp_path)
    reg.reconcile("nvidia", observations("m"), at=T0)
    reg.record_probe("nvidia:m", http_status=200, at=T1,
                     credential_ref="NVIDIA_API_KEY")
    reg.save()
    written = (tmp_path / "registry.json").read_text()
    assert "NVIDIA_API_KEY" in written
    assert "nvapi-" not in written


def test_24c_a_key_is_read_by_name_at_call_time_and_never_stored():
    source = nvidia_source()
    assert source.api_key_env == "NVIDIA_API_KEY"
    assert not any("nvapi" in str(v) for v in vars(source).values())
    headers = source.headers({"NVIDIA_API_KEY": "nvapi-secret"})
    assert headers["Authorization"] == "Bearer nvapi-secret"
    # Built and discarded; nothing was retained on the source.
    assert not any("nvapi" in str(v) for v in vars(source).values())




def test_24d_a_missing_credential_is_named_not_sent_unauthenticated():
    with pytest.raises(MissingCredential, match="NVIDIA_API_KEY"):
        nvidia_source().headers({})
    result = fetch_catalogue(nvidia_source(), transport=FakeTransport(), env={})
    assert result.ok is False
    assert "NVIDIA_API_KEY is not set" in result.error


# ---------------------------------------------------------------------------
# The fetchers themselves
# ---------------------------------------------------------------------------

def test_a_bare_catalogue_claims_nothing_about_capabilities():
    transport = FakeTransport(catalogue=catalogue_of("a", "b"))
    result = fetch_catalogue(nvidia_source(), transport=transport,
                             env={"NVIDIA_API_KEY": "x"})
    assert result.ok is True
    assert [o.model_id for o in result.observations] == ["a", "b"]
    assert result.observations[0].capabilities == {}
    assert result.observations[0].context_window is None
    assert result.observations[0].input_price is None



def test_reconciling_a_different_providers_observations_is_refused(tmp_path):
    reg = registry(tmp_path)
    with pytest.raises(ValueError, match="was handed to the"):
        reg.reconcile("nvidia", observations("a", provider="openrouter"), at=T0)


def test_a_probe_result_for_an_unknown_model_is_refused(tmp_path):
    reg = registry(tmp_path)
    with pytest.raises(KeyError, match="no record for"):
        reg.record_probe("nvidia:never-listed", http_status=200)


def test_a_metadata_change_is_recorded_as_an_event(tmp_path):
    reg = registry(tmp_path)
    reg.reconcile("openrouter", [Observation(provider="openrouter", model_id="m",
                                             context_window=8_000)], at=T0)
    report = reg.reconcile("openrouter", [Observation(provider="openrouter", model_id="m",
                                                      context_window=128_000)], at=T1)
    assert report.changed == ["openrouter:m"]
    assert "context_window" in reg.get("openrouter:m").history[-1]["detail"]


# ---------------------------------------------------------------------------
# 19. The breaker, and what a retirement does to it
# ---------------------------------------------------------------------------

def test_19_a_retirement_opens_the_circuit_and_never_closes_it():
    """
    Before MODEL_RETIRED existed a 410 classified as UNKNOWN_ERROR, whose
    policy retries once and reopens the circuit every 60 seconds -- a dead
    model on a permanent drumbeat. The retirement policy has no cooldown at
    all, which is the honest schedule for something that is not coming back.
    """
    from benchmark.provider_status import policy_for

    retired = policy_for(ProviderStatus.MODEL_RETIRED)
    assert retired.retryable is False
    assert retired.max_retries == 0
    assert retired.open_circuit is True
    assert retired.circuit_seconds is None          # until an operator intervenes
    assert retired.fallback is True
    # A withdrawal is not evidence about the model.
    assert retired.counts_against_quality is False

    unknown = policy_for(ProviderStatus.UNKNOWN_ERROR)
    assert unknown.circuit_seconds == 60.0          # what a 410 used to get


def test_19b_billing_blocked_does_not_repeatedly_consume_attempts():
    from benchmark.provider_status import policy_for

    billing = policy_for(ProviderStatus.BILLING_BLOCKED)
    assert billing.retryable is False
    assert billing.open_circuit is True
    assert billing.counts_against_quality is False


def test_19c_the_breaker_stops_calls_after_repeated_failures():
    from benchmark.health import BreakerPolicy, HealthRegistry, OPEN

    health = HealthRegistry(policy=BreakerPolicy(failure_threshold=3))
    for _ in range(3):
        health.observe("nvidia:m", success=False, error="500")
    assert health.breaker("nvidia:m").state == OPEN
    assert health.allows("nvidia:m") is False


# ---------------------------------------------------------------------------
# 16-17 (routing half). An outage moves work; it does not stop it
# ---------------------------------------------------------------------------

def test_16b_a_provider_outage_triggers_failover_to_another_provider(tmp_path):
    reg = registry(tmp_path)
    reg.reconcile("nvidia", observations("m"), at=T0)
    reg.reconcile("openrouter", observations("m", provider="openrouter"), at=T0)
    reg.record_probe("nvidia:m", http_status=200, at=T0)
    reg.record_probe("openrouter:m", http_status=200, at=T0)

    health = {"nvidia:m": {"usable_now": False, "circuit": {"last_error": "504"},
                           "environmental_failures": 1},
              "openrouter:m": {"usable_now": True}}
    router = QuintekRouter(
        [Candidate("nvidia:m", "nvidia", "m", {"medical_qa"}),
         Candidate("openrouter:m", "openrouter", "m", {"medical_qa"})],
        model_registry=reg, health_for=lambda key: health[key],
        required_capabilities={"question_generation": ("medical_qa",)})
    decision = router.route("question_generation")
    assert decision.selected == "openrouter:m"


def test_17b_a_model_outage_falls_over_to_another_model_on_the_same_provider(tmp_path):
    reg = registry(tmp_path)
    reg.reconcile("nvidia", observations("sick", "well"), at=T0)
    reg.record_probe("nvidia:sick", http_status=200, at=T0)
    reg.record_probe("nvidia:well", http_status=200, at=T0)
    router = QuintekRouter(
        [Candidate("nvidia:sick", "nvidia", "sick", {"medical_qa"}),
         Candidate("nvidia:well", "nvidia", "well", {"medical_qa"})],
        model_registry=reg,
        required_capabilities={"question_generation": ("medical_qa",)})
    decision = router.route_with_fallback("question_generation", failed={"nvidia:sick"})
    assert decision.selected == "nvidia:well"
    assert decision.fallback_from == "nvidia:sick"


def test_17c_a_whole_provider_retiring_does_not_freeze_the_engine(tmp_path):
    """
    Every NVIDIA model gone, one OpenRouter model alive: routing continues
    rather than raising. The failure mode this replaces is the engine calling
    a 410 in a loop.
    """
    reg = registry(tmp_path)
    reg.reconcile("nvidia", observations("a", "b"), at=T0)
    reg.reconcile("openrouter", observations("c", provider="openrouter"), at=T0)
    for key in ("nvidia:a", "nvidia:b"):
        reg.record_probe(key, error=NVIDIA_410, http_status=410, at=T1)
    reg.record_probe("openrouter:c", http_status=200, at=T1)

    router = QuintekRouter(
        [Candidate("nvidia:a", "nvidia", "a", {"medical_qa"}),
         Candidate("nvidia:b", "nvidia", "b", {"medical_qa"}),
         Candidate("openrouter:c", "openrouter", "c", {"medical_qa"})],
        model_registry=reg,
        required_capabilities={"question_generation": ("medical_qa",)})
    assert router.route("question_generation").selected == "openrouter:c"


# ---------------------------------------------------------------------------
# 19/20 (experiment half). The manifest stays immutable
# ---------------------------------------------------------------------------

def test_the_freeze_digest_does_not_move_when_the_catalogue_does(tmp_path):
    """
    Discovery must have no effect on a frozen manifest. Same configuration,
    same digest, whatever the registry now says about those models.
    """
    from validator import freeze as freeze_mod

    def manifest():
        return freeze_mod.build(
            corpus="corpus/validator_dev", corpus_hash="abc",
            models=[{"seat": "candidate", "provider": "nvidia",
                     "model": "meta/llama-3.1-8b-instruct"}],
            experiments=[{"name": "C", "layers": "C", "config": {}}],
            note="", created_at=T0)

    before = manifest().digest()
    reg = registry(tmp_path)
    reg.reconcile("nvidia", observations("meta/llama-3.1-8b-instruct"), at=T0)
    reg.record_probe("nvidia:meta/llama-3.1-8b-instruct", error=NVIDIA_410,
                     http_status=410, at=T1)
    assert manifest().digest() == before


def test_the_experiments_runner_refuses_to_start_against_a_retired_seat(tmp_path,
                                                                        monkeypatch):
    """
    End to end through the CLI's own preflight: a retired frozen model stops
    the run before any budget is committed, and the refusal names the seat
    without naming a substitute.
    """
    import tools_validator_eval as tool
    from benchmark import discovery as discovery_mod

    path = tmp_path / "model_registry.json"
    reg = DynamicModelRegistry(path)
    reg.reconcile("nvidia", observations("meta/llama-3.1-8b-instruct",
                                         "meta/llama-3.1-70b-instruct"), at=T0)
    for key in ("nvidia:meta/llama-3.1-8b-instruct", "nvidia:meta/llama-3.1-70b-instruct"):
        reg.record_probe(key, error=NVIDIA_410, http_status=410, at=T1)
    reg.save()
    monkeypatch.setattr(discovery_mod, "DEFAULT_REGISTRY_PATH", path)

    class Args:
        candidate = "nvidia:meta/llama-3.1-8b-instruct"
        judge = "nvidia:meta/llama-3.1-70b-instruct"

    blocked = tool._withdrawn_seats(Args())
    assert {row["seat"] for row in blocked} == {"candidate", "judge"}
    assert all(row["terminal"] for row in blocked)


def test_no_registry_on_disk_means_no_opinion(tmp_path, monkeypatch):
    """
    A missing registry is 'discovery has not run here', which is not evidence
    that the models are fine -- so the check says nothing rather than passing
    or failing.
    """
    import tools_validator_eval as tool
    from benchmark import discovery as discovery_mod

    monkeypatch.setattr(discovery_mod, "DEFAULT_REGISTRY_PATH", tmp_path / "absent.json")

    class Args:
        candidate = "nvidia:a"
        judge = "nvidia:b"

    assert tool._withdrawn_seats(Args()) == []


def test_a_provider_that_publishes_no_prices_is_unknown_not_unpriced():
    """
    Both fail a budget ceiling, and they are still different facts. UNPRICED
    means the provider has a price and will not commit to it in the catalogue;
    UNKNOWN means it does not publish prices. Reading NVIDIA's bare catalogue
    as UNPRICED would claim a sentinel that was never sent.
    """
    transport = FakeTransport(catalogue=catalogue_of("a"))
    result = fetch_catalogue(nvidia_source(), transport=transport,
                             env={"NVIDIA_API_KEY": "x"})
    assert result.observations[0].price_stated is True
    assert result.observations[0].input_price is None
    assert price_state(result.observations[0].input_price,
                       stated=result.observations[0].price_stated) == Pricing.UNKNOWN
