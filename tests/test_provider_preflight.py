"""
Preflight: what a configuration WOULD do, checked without doing it.

The exit condition for provider readiness is that dropping a credential into
the environment is the only remaining step before a live call. This file
proves the parts of that which are provable without one -- and is explicit
about the parts that are not, because a mocked transport cannot tell you an
endpoint is correct.
"""

from __future__ import annotations

import json

import pytest

from tools_provider_preflight import main, preflight

KEY = "not-a-real-credential-value"


def test_a_scripted_default_is_reported_as_not_a_real_model(monkeypatch):
    monkeypatch.delenv("QUINTEK_PROVIDER", raising=False)
    report = preflight({"provider": "scripted"})
    assert report["buildable"] is True
    assert report["is_real_model"] is False
    assert report["unverified"] == [], "nothing is unverified about a double"


def test_a_missing_credential_is_reported_by_name_and_blocks_the_build(monkeypatch):
    monkeypatch.delenv("FIREWORKS_API_KEY", raising=False)
    report = preflight({"provider": "fireworks", "model_id": "accounts/x/models/y"})
    assert report["credential_ref"] == "FIREWORKS_API_KEY"
    assert report["credential_present"] is False
    assert report["buildable"] is False
    assert "FIREWORKS_API_KEY" in report["reason"]


def test_a_present_credential_makes_the_configuration_buildable(monkeypatch):
    monkeypatch.setenv("TOGETHER_API_KEY", KEY)
    report = preflight({"provider": "together",
                        "model_id": "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo"})
    assert report["credential_present"] is True
    assert report["buildable"] is True
    assert report["is_real_model"] is True


def test_the_credential_value_never_appears_in_the_report(monkeypatch):
    """A preflight that printed a key would be a leak with a helpful tone."""
    monkeypatch.setenv("FIREWORKS_API_KEY", KEY)
    report = preflight({"provider": "fireworks", "model_id": "accounts/x/models/y"})
    assert KEY not in json.dumps(report), "the report carries the credential VALUE"
    assert report["credential_ref"] == "FIREWORKS_API_KEY", "the NAME is what is reported"


def test_it_reports_what_would_be_spent_per_call(monkeypatch):
    monkeypatch.setenv("FIREWORKS_API_KEY", KEY)
    report = preflight({"provider": "fireworks", "model_id": "a/b/c",
                        "timeout_seconds": 45.0, "max_retries": 3})
    assert report["timeout_seconds"] == 45.0
    assert report["max_retries"] == 3
    assert report["outbound_attempts_per_call"] == 4, (
        "the budget is counted in outbound attempts, so a reader needs 1 + retries")


def test_a_real_provider_lists_what_preflight_cannot_check(monkeypatch):
    monkeypatch.setenv("FIREWORKS_API_KEY", KEY)
    report = preflight({"provider": "fireworks", "model_id": "a/b/c"})
    joined = " ".join(report["unverified"])
    assert "endpoint" in joined and "credential" in joined, (
        "a preflight that implied it had verified the endpoint would be worse "
        "than one that made no claim at all")


def test_an_unknown_provider_is_refused_and_lists_the_known_names():
    report = preflight({"provider": "invented-host"})
    assert report["known"] is False
    assert report["buildable"] is False


def test_no_request_is_made(monkeypatch):
    """The whole point: it is safe to run with a live credential present."""
    monkeypatch.setenv("FIREWORKS_API_KEY", KEY)
    from unittest.mock import patch
    with patch("urllib.request.urlopen", side_effect=AssertionError("preflight called out")):
        report = preflight({"provider": "fireworks", "model_id": "a/b/c"})
    assert report["buildable"] is True


def test_the_command_exits_non_zero_when_it_would_not_run(monkeypatch, capsys):
    monkeypatch.delenv("FIREWORKS_API_KEY", raising=False)
    code = main(["--provider", "fireworks", "--model-id", "a/b/c"])
    assert code == 1
    assert "NOT BUILDABLE" in capsys.readouterr().out


def test_the_command_reads_the_environment_by_default(monkeypatch, capsys):
    monkeypatch.setenv("QUINTEK_PROVIDER", "together")
    monkeypatch.setenv("QUINTEK_MODEL_ID", "org/model")
    monkeypatch.setenv("QUINTEK_MAX_RETRIES", "1")
    monkeypatch.setenv("TOGETHER_API_KEY", KEY)
    assert main(["--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["provider"] == "together"
    assert report["model_id"] == "org/model"
    assert report["max_retries"] == 1
    assert KEY not in json.dumps(report)


@pytest.mark.parametrize("provider,key_env", [
    ("fireworks", "FIREWORKS_API_KEY"),
    ("together", "TOGETHER_API_KEY"),
    ("groq", "GROQ_API_KEY"),
])
def test_both_candidate_hosts_are_registered_and_behave_identically(
        provider, key_env, monkeypatch):
    """
    Neither was chosen. The interface does not differ in any way that would
    make choosing early worth doing -- both speak /v1/chat/completions through
    the same adapter -- so both exist and the credential decides.
    """
    monkeypatch.setenv(key_env, KEY)
    report = preflight({"provider": provider, "model_id": "some/model"})
    assert report["buildable"] is True
    assert report["credential_ref"] == key_env
    assert report["base_url"].endswith("/chat/completions")


# ---------------------------------------------------------------------------
# The exit condition
# ---------------------------------------------------------------------------

def test_the_only_missing_step_is_the_credential(monkeypatch):
    """
    Phase B's exit condition, as a test: with a placeholder credential and a
    mocked transport, the whole path runs -- environment to spec to provider to
    request to parsed reply. Nothing is left to build.

    What this does NOT prove, and cannot: that the endpoint is right, that the
    model id exists on that host, or that a real credential is accepted. A
    mocked socket will agree with any URL you give it.
    """
    import json as _json
    from unittest.mock import patch

    from benchmark.providers.base import GenerationRequest
    from benchmark.providers.registry import build_provider, spec_from_env

    monkeypatch.setenv("FIREWORKS_API_KEY", KEY)
    monkeypatch.setenv("QUINTEK_PROVIDER", "fireworks")
    monkeypatch.setenv("QUINTEK_MODEL_ID", "accounts/fireworks/models/llama-v3p1-70b-instruct")
    monkeypatch.setenv("QUINTEK_MAX_RETRIES", "2")
    monkeypatch.setenv("QUINTEK_TIMEOUT_SECONDS", "45")

    spec = spec_from_env()
    provider = build_provider(spec)
    assert provider.retry_policy.max_retries == 2
    assert provider.retry_policy.timeout_seconds == 45.0

    body = _json.dumps({
        "id": "c1",
        "choices": [{"message": {"role": "assistant",
                                 "content": '{"supported": ["A"]}'},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
    }).encode()

    seen = {}

    class _Resp:
        def read(self):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake(req, timeout=None):
        seen["url"] = req.full_url
        seen["timeout"] = timeout
        seen["auth"] = any(k.lower() == "authorization" for k in req.headers)
        return _Resp()

    with patch("urllib.request.urlopen", side_effect=_fake):
        response = provider.generate(
            GenerationRequest(item_id="q1", prompt="check this", max_tokens=4096))

    assert seen["url"] == "https://api.fireworks.ai/inference/v1/chat/completions"
    assert seen["timeout"] == 45.0
    assert seen["auth"] is True, "the credential is sent as a header at call time"
    assert response.ok and response.parsed == {"supported": ["A"]}
    assert response.input_tokens == 120 and response.output_tokens == 30
    assert KEY not in _json.dumps(response.__dict__, default=str)


def test_the_credential_is_not_read_until_call_time(monkeypatch):
    """
    Building must not capture the key. If it did, a process that started before
    the credential was exported would hold a stale absence for its lifetime,
    and the value would live on the instance where a manifest could reach it.
    """
    from benchmark.providers.registry import build_provider

    monkeypatch.setenv("FIREWORKS_API_KEY", KEY)
    provider = build_provider({"provider": "fireworks", "model_id": "a/b/c"})

    assert KEY not in json.dumps(vars(provider), default=str), (
        "the key value is stored on the provider instance")
    assert provider.api_key_env == "FIREWORKS_API_KEY", "only the NAME is held"

    # Remove it after building: the failure must appear at the call, not before.
    monkeypatch.delenv("FIREWORKS_API_KEY")
    from benchmark.providers.base import GenerationRequest
    response = provider.generate(GenerationRequest(item_id="q", prompt="x"))
    assert response.ok is False
    assert "FIREWORKS_API_KEY" in response.error


# ---------------------------------------------------------------------------
# Groq
# ---------------------------------------------------------------------------

def test_groq_needs_an_explicit_model_id(monkeypatch):
    """
    Groq's catalogue changes and its ids are plain names, so guessing one would
    produce a 404 that looks like a broken key. None is defaulted.
    """
    monkeypatch.setenv("GROQ_API_KEY", KEY)
    report = preflight({"provider": "groq"})
    assert report["buildable"] is False
    assert "model_id" in report["reason"]


def test_groq_preflight_prints_what_must_be_checked_by_hand(monkeypatch, capsys):
    """
    The endpoint and the model id are the two things nothing here can verify,
    so both are printed for checking against Groq's docs before a call.
    """
    monkeypatch.setenv("GROQ_API_KEY", KEY)
    assert main(["--provider", "groq", "--model-id", "a-model-id"]) == 0
    out = capsys.readouterr().out
    assert "https://api.groq.com/openai/v1/chat/completions" in out
    assert "a-model-id" in out
    assert "GROQ_API_KEY" in out and KEY not in out
    assert "no request was made" in out


def test_groq_reports_rate_as_the_binding_limit(monkeypatch):
    """
    Different from the paid hosts, and it changes how a run is planned: Groq
    meters requests per minute, so wall clock binds before spend does.
    """
    monkeypatch.setenv("GROQ_API_KEY", KEY)
    groq = preflight({"provider": "groq", "model_id": "m"})
    assert "per minute" in groq["binding_constraint"]
    assert "daily" in groq["binding_constraint"]

    monkeypatch.setenv("FIREWORKS_API_KEY", KEY)
    paid = preflight({"provider": "fireworks", "model_id": "a/b/c"})
    assert paid["binding_constraint"] == "token spend"


def test_groq_preflight_makes_no_request(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", KEY)
    from unittest.mock import patch
    with patch("urllib.request.urlopen", side_effect=AssertionError("preflight called out")):
        assert preflight({"provider": "groq", "model_id": "m"})["buildable"] is True
