"""
Tests for the provider boundary.

The load-bearing property is negative: a spec that names a real model and
cannot build it must fail, loudly, rather than quietly handing back the
scripted provider. That substitution would make fabricated answers
indistinguishable from real ones, which is the same defect as the frontend
fixtures fallback, one layer down.
"""

from __future__ import annotations

import pytest

from benchmark.providers.registry import (
    BUILDERS, ProviderUnavailable, UnknownProvider, available, build_provider,
    describe, spec_from_env,
)


def test_the_known_providers_are_named(monkeypatch):
    assert set(available()) == {"scripted", "nvidia", "openai-compatible", "local",
                                "cerebras", "openrouter"}


def test_cerebras_and_openrouter_need_their_own_keys(monkeypatch):
    for provider, key_env, model in (("cerebras", "CEREBRAS_API_KEY", "llama3.1-8b"),
                                     ("openrouter", "OPENROUTER_API_KEY", "x/y")):
        monkeypatch.delenv(key_env, raising=False)
        with pytest.raises(ProviderUnavailable, match=key_env):
            build_provider({"provider": provider, "model_id": model})


def test_each_gateway_keeps_its_own_default_endpoint(monkeypatch):
    """
    A model id from one provider passed to another is the obvious mistake.
    Endpoints are per-provider so it fails loudly rather than being rewritten.
    """
    monkeypatch.setenv("CEREBRAS_API_KEY", "k")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    cerebras = build_provider({"provider": "cerebras", "model_id": "llama3.1-8b"})
    openrouter = build_provider({"provider": "openrouter", "model_id": "meta-llama/x"})
    assert "cerebras.ai" in cerebras.base_url
    assert "openrouter.ai" in openrouter.base_url
    assert cerebras.base_url != openrouter.base_url


def test_a_bare_string_is_taken_as_the_provider_name():
    assert build_provider("scripted").name == "scripted-test-harness"


def test_an_unknown_provider_names_the_ones_that_exist():
    with pytest.raises(UnknownProvider, match="scripted"):
        build_provider("wishful-thinking")


def test_an_empty_spec_is_refused():
    with pytest.raises(UnknownProvider):
        build_provider({})


# ---------- the substitution rule ----------

def test_a_real_provider_that_cannot_authenticate_fails_rather_than_falling_back(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    with pytest.raises(ProviderUnavailable, match="NVIDIA_API_KEY"):
        build_provider({"provider": "nvidia", "model_id": "meta/llama-3.3-70b-instruct"})


def test_no_builder_ever_returns_the_scripted_provider_for_a_real_spec(monkeypatch):
    """
    Belt and braces: if a real builder somehow succeeded, what it returns must
    not be the test double.
    """
    from benchmark.providers.scripted import ScriptedProvider

    monkeypatch.setenv("NVIDIA_API_KEY", "test-key-not-real")
    provider = build_provider({"provider": "nvidia", "model_id": "meta/llama-3.3-70b-instruct"})
    assert not isinstance(provider, ScriptedProvider)
    assert provider.model == "meta/llama-3.3-70b-instruct"


def test_a_real_provider_needs_an_explicit_model_id(monkeypatch):
    """Guessing a default would misattribute every result it produced."""
    monkeypatch.setenv("NVIDIA_API_KEY", "test-key-not-real")
    with pytest.raises(ProviderUnavailable, match="model_id"):
        build_provider({"provider": "nvidia"})


# ---------- failing at construction, not at call time ----------

def test_describe_reports_buildability_without_making_a_call(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    report = describe({"provider": "nvidia", "model_id": "meta/llama-3.3-70b-instruct"})
    assert report["known"] is True
    assert report["buildable"] is False
    assert "NVIDIA_API_KEY" in report["reason"]
    assert report["is_real_model"] is True


def test_describe_marks_the_scripted_provider_as_not_a_real_model():
    report = describe("scripted")
    assert report["buildable"] is True
    assert report["is_real_model"] is False


def test_describe_does_not_raise_on_an_unknown_provider():
    report = describe("nonesuch")
    assert report["known"] is False
    assert report["buildable"] is False


# ---------- openai-compatible and local ----------

def test_openai_compatible_needs_a_base_url(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    with pytest.raises(ProviderUnavailable, match="base_url"):
        build_provider({"provider": "openai-compatible", "model_id": "m"})


def test_openai_compatible_builds_against_an_arbitrary_endpoint(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    provider = build_provider({"provider": "openai-compatible", "model_id": "some/model",
                               "base_url": "https://example.test/v1/chat/completions"})
    assert provider.model == "some/model"
    assert provider.base_url == "https://example.test/v1/chat/completions"


def test_local_needs_no_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider = build_provider({"provider": "local", "model_id": "llama-3-8b"})
    assert "127.0.0.1" in provider.base_url


# ---------- environment configuration ----------

def test_env_configuration_defaults_to_scripted_not_to_a_paid_provider(monkeypatch):
    for var in ("QUINTEK_PROVIDER", "QUINTEK_MODEL_ID", "QUINTEK_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    assert spec_from_env()["provider"] == "scripted"


def test_env_configuration_is_read_through(monkeypatch):
    monkeypatch.setenv("QUINTEK_PROVIDER", "nvidia")
    monkeypatch.setenv("QUINTEK_MODEL_ID", "meta/llama-3.3-70b-instruct")
    monkeypatch.setenv("QUINTEK_MODEL_VERSION", "2024-12")
    monkeypatch.setenv("QUINTEK_TIMEOUT_SECONDS", "180")
    spec = spec_from_env()
    assert spec == {"provider": "nvidia", "model_id": "meta/llama-3.3-70b-instruct",
                    "model_version": "2024-12", "timeout_seconds": 180.0}


def test_every_registered_builder_is_callable_with_a_spec():
    """A builder that cannot be called is a name that lies about existing."""
    for name, builder in BUILDERS.items():
        assert callable(builder), name
