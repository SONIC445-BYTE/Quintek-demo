"""
DeepSeek is registered, not reached through `openai-compatible`.

WHY THAT DISTINCTION IS WORTH A TEST FILE
------------------------------------------
`openai-compatible` plus `--endpoint` would have talked to DeepSeek today,
with no code at all. It was rejected for three reasons, and each one is a
defect this project has already paid for once:

  1. `tools_validator_eval` applies ONE `--endpoint` to BOTH seats, so
     reaching a host that way forces the candidate and the judge onto the same
     endpoint. `judge.assert_independent` exists because a second opinion from
     the same weights is the first opinion again.
  2. `openai-compatible` hard-codes `name="openai-compatible"`, so the record
     would name no vendor -- the defect fixed in "The adapter was telling
     every host it was NVIDIA", reintroduced by another route.
  3. `openai-compatible` defaults to `OPENAI_API_KEY` and the CLI cannot
     override it, so a DeepSeek key would have to live under another vendor's
     name.

So these tests assert the three properties, not the registration.
"""

from __future__ import annotations

import pytest

from benchmark.providers.registry import (ProviderUnavailable, available,
                                          build_provider, describe)
from tools_validator_eval import parse_seat


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "not-a-real-key")
    monkeypatch.setenv("GROQ_API_KEY", "not-a-real-key")


def test_it_is_registered():
    assert "deepseek" in available()


class TestTheSeatsCanSitOnDifferentHosts:
    """Reason 1, and the disqualifying one."""

    def test_no_endpoint_is_needed_so_each_seat_keeps_its_own_host(self, keys):
        candidate = build_provider(parse_seat("deepseek:some-deepseek-model"))
        judge = build_provider(parse_seat("groq:some-groq-model"))
        assert "api.deepseek.com" in candidate.base_url
        assert "api.groq.com" in judge.base_url
        assert candidate.base_url != judge.base_url

    def test_one_endpoint_would_have_collapsed_both_seats_onto_one_host(self, keys):
        """
        The mechanism being avoided, stated as a fact about `parse_seat`: a
        single `--endpoint` overrides the base_url of BOTH seats, whatever
        providers they name.
        """
        shared = "https://api.deepseek.com/v1/chat/completions"
        candidate = build_provider(parse_seat("deepseek:model-a", endpoint=shared))
        judge = build_provider(parse_seat("groq:model-b", endpoint=shared))
        assert candidate.base_url == judge.base_url == shared, (
            "if this ever stops being true, the reason this provider was registered "
            "rather than reached through --endpoint has changed, and the docstring "
            "above should be revisited.")

    def test_two_registered_hosts_read_two_different_credentials(self, keys):
        candidate = build_provider(parse_seat("deepseek:model-a"))
        judge = build_provider(parse_seat("groq:model-b"))
        assert candidate.api_key_env == "DEEPSEEK_API_KEY"
        assert judge.api_key_env == "GROQ_API_KEY"


class TestTheRecordNamesTheHost:
    """Reason 2."""

    def test_the_provider_name_is_deepseek(self, keys):
        assert build_provider({"provider": "deepseek", "model_id": "m"}).name == "deepseek"

    def test_the_error_message_names_the_host_that_refused(self, keys):
        provider = build_provider({"provider": "deepseek", "model_id": "m"})
        assert provider._host_label() == "deepseek (api.deepseek.com)"

    def test_it_does_not_inherit_another_vendors_name(self, keys):
        provider = build_provider({"provider": "deepseek", "model_id": "m"})
        for wrong in ("nvidia", "openai-compatible", "groq"):
            assert provider.name != wrong


class TestTheCredential:
    """Reason 3, plus the rule that holds for every host in this registry."""

    def test_it_has_its_own_variable(self, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        with pytest.raises(ProviderUnavailable, match="DEEPSEEK_API_KEY"):
            build_provider({"provider": "deepseek", "model_id": "m"})

    def test_it_does_not_fall_back_to_another_vendors_key(self, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")
        monkeypatch.setenv("GROQ_API_KEY", "not-a-real-key")
        with pytest.raises(ProviderUnavailable, match="DEEPSEEK_API_KEY"):
            build_provider({"provider": "deepseek", "model_id": "m"})

    def test_the_value_never_reaches_the_instance_or_the_manifest(self, monkeypatch):
        """
        The standing rule: only the NAME of a credential may be recorded.
        `_openai_host` reads the variable to decide buildability and throws the
        value away; `_api_key` reads it again at call time.
        """
        secret = "ds-secret-value-that-must-not-escape"
        monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
        provider = build_provider({"provider": "deepseek", "model_id": "m"})
        assert secret not in repr(vars(provider))
        assert secret not in str(provider.manifest())
        assert secret not in str(describe({"provider": "deepseek", "model_id": "m"}))


class TestNoModelIdIsAssumed:

    def test_a_missing_model_id_is_refused_rather_than_defaulted(self, keys):
        """
        The catalogue changes and a stale default is a run spent on a 404. Same
        rule as groq, fireworks and together.
        """
        with pytest.raises(ProviderUnavailable, match="model_id"):
            build_provider({"provider": "deepseek"})

    def test_the_refusal_says_how_to_check_the_id(self, keys):
        with pytest.raises(ProviderUnavailable, match="DeepSeek"):
            build_provider({"provider": "deepseek"})


class TestPreflight:

    def test_the_key_table_covers_it(self):
        from tools_provider_preflight import DEFAULT_KEY_ENV
        assert DEFAULT_KEY_ENV["deepseek"] == "DEEPSEEK_API_KEY"

    def test_the_key_table_covers_every_host_that_needs_a_key(self):
        """
        `tools_provider_preflight` keeps its own copy of the provider-to-
        variable mapping. Two tables that must agree are two tables that will
        disagree, so this fails when a new host is added to one and not the
        other -- which is how preflight would otherwise report the wrong
        variable name for a host it had never heard of.
        """
        from tests.test_provider_registry import KEYLESS
        from tools_provider_preflight import DEFAULT_KEY_ENV
        missing = sorted(set(available()) - KEYLESS - set(DEFAULT_KEY_ENV))
        assert not missing, (
            f"{missing} are registered providers with no entry in "
            "tools_provider_preflight.DEFAULT_KEY_ENV")

    def test_it_reports_deepseek_without_making_a_request(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "not-a-real-key")
        import urllib.request
        from tools_provider_preflight import preflight

        def refuse(*_a, **_kw):
            raise AssertionError("preflight must not make a request")

        monkeypatch.setattr(urllib.request, "urlopen", refuse)
        report = preflight({"provider": "deepseek", "model_id": "some-model"})
        assert report["provider"] == "deepseek"
        assert report["buildable"] is True
