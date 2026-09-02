"""
NVIDIA NIM provider adapter.

No live network call in this suite -- `urllib.request.urlopen` is mocked so
correctness is provable without spending API budget or depending on network
access this sandbox's egress policy currently blocks (see
IMPLEMENTATION_STATUS.md). The adapter is written so the moment connectivity
exists, nothing here needs to change to run for real.
"""

from __future__ import annotations

import json
import urllib.error
from io import BytesIO
from unittest.mock import patch

import pytest

from benchmark.providers.base import GenerationRequest, RetryPolicy
from benchmark.providers.nvidia import NVIDIAProvider, _infer_family, extract_json_object


class _FakeHTTPResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _chat_completion_payload(content: str, prompt_tokens=10, completion_tokens=5) -> bytes:
    return json.dumps({
        "id": "chatcmpl-1",
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                  "total_tokens": prompt_tokens + completion_tokens},
    }).encode("utf-8")


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-key")


def test_missing_api_key_surfaces_as_response_error(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    provider = NVIDIAProvider("meta/llama-3.1-70b-instruct")
    provider.retry_policy = RetryPolicy(max_retries=0, timeout_seconds=5.0)
    resp = provider.generate(GenerationRequest(item_id="q1", prompt="What is the first-line treatment?"))
    assert not resp.ok
    assert "NVIDIA_API_KEY" in resp.error


def test_successful_call_parses_json_and_records_usage():
    body = _chat_completion_payload('{"answer": "B"}', prompt_tokens=42, completion_tokens=7)
    with patch("benchmark.providers.nvidia.urllib.request.urlopen",
              return_value=_FakeHTTPResponse(body)) as mock_open:
        provider = NVIDIAProvider("meta/llama-3.1-70b-instruct", model_version="2026-01")
        resp = provider.generate(GenerationRequest(item_id="q1", prompt="pick A or B"))

    assert resp.ok
    assert resp.parsed == {"answer": "B"}
    assert resp.input_tokens == 42
    assert resp.output_tokens == 7
    assert resp.attempts == 1
    assert resp.provider == "nvidia"
    assert resp.model == "meta/llama-3.1-70b-instruct"
    mock_open.assert_called_once()


def test_request_carries_authorization_header_and_model():
    body = _chat_completion_payload('{"answer": "A"}')
    with patch("benchmark.providers.nvidia.urllib.request.urlopen",
              return_value=_FakeHTTPResponse(body)) as mock_open:
        provider = NVIDIAProvider("meta/llama-3.1-70b-instruct")
        provider.generate(GenerationRequest(item_id="q1", prompt="hello", system="be terse"))

    sent_request = mock_open.call_args[0][0]
    assert sent_request.get_header("Authorization") == "Bearer nvapi-test-key"
    payload = json.loads(sent_request.data)
    assert payload["model"] == "meta/llama-3.1-70b-instruct"
    assert payload["messages"][0] == {"role": "system", "content": "be terse"}
    assert payload["messages"][1] == {"role": "user", "content": "hello"}


def test_unparseable_content_is_invalid_not_a_crash():
    body = _chat_completion_payload("Sure, the answer is probably B, hard to say.")
    with patch("benchmark.providers.nvidia.urllib.request.urlopen",
              return_value=_FakeHTTPResponse(body)):
        provider = NVIDIAProvider("meta/llama-3.1-70b-instruct")
        resp = provider.generate(GenerationRequest(item_id="q1", prompt="pick A or B"))

    assert resp.ok  # the HTTP call succeeded
    assert resp.parsed is None  # but nothing parseable came back


def test_http_429_is_retried_then_succeeds():
    rate_limited = urllib.error.HTTPError(
        url="x", code=429, msg="Too Many Requests",
        hdrs=None, fp=BytesIO(b'{"error": "rate limited"}'),
    )
    success = _FakeHTTPResponse(_chat_completion_payload('{"answer": "C"}'))

    with patch("benchmark.providers.nvidia.urllib.request.urlopen",
              side_effect=[rate_limited, success]):
        provider = NVIDIAProvider("meta/llama-3.1-70b-instruct")
        provider.retry_policy = RetryPolicy(max_retries=1, timeout_seconds=5.0)
        resp = provider.generate(GenerationRequest(item_id="q1", prompt="x"))

    assert resp.ok
    assert resp.attempts == 2
    assert resp.parsed == {"answer": "C"}


def test_http_error_exhausting_retries_is_a_clean_failure_not_a_fake_answer():
    error = urllib.error.HTTPError(
        url="x", code=500, msg="Internal Server Error",
        hdrs=None, fp=BytesIO(b"upstream error"),
    )
    with patch("benchmark.providers.nvidia.urllib.request.urlopen", side_effect=error):
        provider = NVIDIAProvider("meta/llama-3.1-70b-instruct")
        provider.retry_policy = RetryPolicy(max_retries=2, timeout_seconds=5.0)
        resp = provider.generate(GenerationRequest(item_id="q1", prompt="x"))

    assert not resp.ok
    assert resp.parsed is None
    assert resp.attempts == 3
    assert "500" in resp.error


def test_connection_failure_raises_timeout_error_not_silently_swallowed():
    with patch("benchmark.providers.nvidia.urllib.request.urlopen",
              side_effect=urllib.error.URLError("egress policy denied")):
        provider = NVIDIAProvider("meta/llama-3.1-70b-instruct")
        provider.retry_policy = RetryPolicy(max_retries=0, timeout_seconds=5.0)
        resp = provider.generate(GenerationRequest(item_id="q1", prompt="x"))

    assert not resp.ok
    assert "egress policy denied" in resp.error


def test_manifest_reports_provider_model_and_family():
    provider = NVIDIAProvider("openai/gpt-oss-120b", model_version="2026-08")
    m = provider.manifest()
    assert m["provider"] == "nvidia"
    assert m["model_id"] == "openai/gpt-oss-120b"
    assert m["model_version"] == "2026-08"
    assert m["model_family"] == "gpt-oss"


def test_key_is_never_present_in_the_manifest():
    """The manifest travels into candidate_manifest / report.json -- it must
    never carry the secret."""
    provider = NVIDIAProvider("meta/llama-3.1-70b-instruct")
    manifest_text = json.dumps(provider.manifest())
    assert "nvapi-test-key" not in manifest_text


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def test_infer_family_recognizes_common_families():
    assert _infer_family("meta/llama-3.1-70b-instruct") == "llama"
    assert _infer_family("openai/gpt-oss-120b") == "gpt-oss"
    assert _infer_family("google/gemma-2-27b-it") == "gemma"
    assert _infer_family("mistralai/mixtral-8x22b-instruct") == "mixtral"


def test_extract_json_object_finds_embedded_json():
    text = 'Sure! Here is my answer: {"answer": "B", "confidence": 0.9} Hope that helps.'
    assert extract_json_object(text) == {"answer": "B", "confidence": 0.9}


def test_extract_json_object_returns_none_for_no_json():
    assert extract_json_object("The answer is B.") is None


def test_extract_json_object_returns_none_for_malformed_json():
    assert extract_json_object("{answer: B, this is not valid json}") is None


# ---------------------------------------------------------------------------
# The defect Phase 0 found on 2026-09-02
# ---------------------------------------------------------------------------

def test_a_reasoning_model_with_null_content_is_parsed_not_crashed():
    """
    NIM reasoning models leave `message.content` null and put the reply in
    `reasoning_content`. The adapter read only `content`, handed None to
    `extract_json_object`, and raised "expected string or bytes-like object,
    got 'NoneType'" -- which the validator recorded as a backend OUTAGE, so
    the item was lost rather than judged.

    Phase 0 lost items to exactly this against a reasoning candidate. The same
    bug had already been fixed in benchmark/capability_probe.py and not here,
    which is why the extraction now has one definition in providers/base.py.
    """
    import json as _json
    from benchmark.providers.base import content_of

    body = {"choices": [{"message": {"role": "assistant", "content": None,
                                     "reasoning_content": '{"answer": "B"}'}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4}}
    message = body["choices"][0]["message"]
    text = content_of(message)
    assert text == '{"answer": "B"}'
    assert extract_json_object(text) == {"answer": "B"}


def test_an_entirely_empty_reply_fails_to_parse_rather_than_raising():
    """
    "" is the right shape for a reply with no text: a parse failure is a
    recorded, classifiable outcome; an exception is an outage that loses the
    item and blames the backend.
    """
    from benchmark.providers.base import content_of

    assert content_of({"content": None}) == ""
    assert content_of({}) == ""
    assert extract_json_object(content_of({"content": None})) is None


def test_content_parts_lists_are_joined():
    from benchmark.providers.base import content_of

    assert content_of({"content": [{"type": "text", "text": "a"},
                                   {"type": "text", "text": "b"}]}) == "ab"


def test_the_probe_and_the_adapter_share_one_definition():
    """
    Two call sites with two answers to "where is the reply's text" is how one
    of them stayed wrong for a month.
    """
    from benchmark import capability_probe
    from benchmark.providers import base

    assert capability_probe.content_of is base.content_of
    assert capability_probe.TEXT_FIELDS is base.TEXT_FIELDS
