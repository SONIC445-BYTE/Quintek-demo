"""
The transport failures a real host produces, which nothing here had exercised.

The audit that preceded this file found that `benchmark/providers/nvidia.py`
was tested only against mocked success, a 429, and a generic connection
failure. Nothing covered TLS through a proxy, a reset mid-body, a request
timeout, a truncated reply, or an HTTP-level auth rejection -- and those are
the failures a paid host actually produces on a bad day.

They are tested here at the level that matters for the harness: whether the
adapter turns each into a CLEAN, ATTRIBUTABLE response rather than a crash or,
worse, something a later layer could mistake for an answer. Every one of these
must end as `response.ok is False` with the reason preserved, because the
validator's outage record is built from exactly those fields
(`validator/outage.py`).

Still mocked, still no live call. A mocked transport cannot prove an endpoint
is right -- see `tools_provider_preflight.py` for what remains unverifiable
until a credential exists.
"""

from __future__ import annotations

import json
import socket
import ssl
import urllib.error
from io import BytesIO
from unittest.mock import patch

import pytest

from benchmark.providers.base import GenerationRequest, RetryPolicy
from benchmark.providers.nvidia import NVIDIAProvider

REQUEST = GenerationRequest(item_id="q1", prompt="ask something", max_tokens=64)


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "test-key-not-a-real-credential")


def _provider(**kw):
    kw.setdefault("max_retries", 0)
    kw.setdefault("timeout_seconds", 5.0)
    return NVIDIAProvider("meta/llama-3.1-70b-instruct", **kw)


def _http_error(code, body=b"{}", reason="err"):
    return urllib.error.HTTPError(
        url="https://host/v1/chat/completions", code=code, msg=reason,
        hdrs=None, fp=BytesIO(body))


# ---------------------------------------------------------------------------
# The five never-covered failures
# ---------------------------------------------------------------------------

def test_tls_verification_failure_through_a_proxy_is_a_clean_error():
    """
    A proxy with an untrusted certificate. `urlopen` raises URLError wrapping
    an SSLError, and the adapter must not let that escape as a crash inside a
    run that has already spent budget on earlier items.
    """
    boom = urllib.error.URLError(ssl.SSLCertVerificationError(
        "certificate verify failed: unable to get local issuer certificate"))
    with patch("urllib.request.urlopen", side_effect=boom):
        response = _provider().generate(REQUEST)

    assert response.ok is False
    assert response.parsed is None
    assert "certificate verify failed" in response.error
    assert response.raw_output == "", "nothing may look like a reply"

    # KNOWN MISLABELLING, recorded rather than silently tolerated. `_call`
    # maps every URLError to TimeoutError, so a certificate problem is typed
    # as a timeout and sends a reader to look at latency. The cause survives
    # in the message, which is why this is a wart and not a defect -- but
    # changing the type would change the retry contract in base.py, which
    # documents TimeoutError as the retryable transport signal. Left alone
    # deliberately; this assertion is here so a fix is a deliberate change to
    # a written expectation.
    assert response.error.startswith("TimeoutError:")


def test_a_connection_reset_mid_body_is_not_a_partial_answer():
    """
    Headers arrive, then the peer resets. `resp.read()` raises after the
    context manager has opened -- a different path from a failed connect, and
    the dangerous one, because a half-read body could otherwise be parsed.
    """
    class _ResettingResponse:
        def read(self):
            raise ConnectionResetError(104, "Connection reset by peer")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    with patch("urllib.request.urlopen", return_value=_ResettingResponse()):
        response = _provider().generate(REQUEST)

    assert response.ok is False
    assert "ConnectionResetError" in response.error
    assert response.parsed is None


def test_a_request_timeout_is_reported_as_one():
    """`socket.timeout` is `TimeoutError`; the retry loop treats it as retryable."""
    with patch("urllib.request.urlopen", side_effect=socket.timeout("timed out")):
        response = _provider().generate(REQUEST)

    assert response.ok is False
    assert "timed out" in response.error
    assert response.attempts == 1, "max_retries=0 means one attempt"


def test_a_timeout_is_retried_when_the_policy_allows_it():
    calls = {"n": 0}

    def _flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise socket.timeout("timed out")
        return _ok_response("{\"answer\": \"A\"}")

    with patch("urllib.request.urlopen", side_effect=_flaky):
        response = _provider(max_retries=2).generate(REQUEST)

    assert response.ok is True
    assert response.attempts == 3, (
        "the attempt count is evidence about stability and must survive")


def test_a_truncated_body_fails_to_parse_rather_than_half_parsing():
    """
    A body cut off mid-JSON. The envelope itself is unparseable, which is a
    different failure from a model emitting prose, and must not surface as an
    empty-but-successful answer.
    """
    truncated = b'{"id": "chatcmpl-1", "choices": [{"message": {"content": "{\\"ans'
    with patch("urllib.request.urlopen", return_value=_raw_response(truncated)):
        response = _provider().generate(REQUEST)

    assert response.ok is False
    assert response.parsed is None
    assert "JSONDecodeError" in response.error or "Expecting" in response.error


@pytest.mark.parametrize("code,label", [(401, "unauthorized"), (403, "forbidden")])
def test_http_auth_rejection_is_distinct_from_a_missing_key(code, label):
    """
    A key that is PRESENT and REJECTED. Different from the unset-variable case
    already covered: the request was made, so it is a transport failure with a
    status, and the status has to reach the record for anyone to tell a wrong
    key from a wrong endpoint.
    """
    with patch("urllib.request.urlopen",
               side_effect=_http_error(code, json.dumps({"error": label}).encode())):
        response = _provider().generate(REQUEST)

    assert response.ok is False
    assert f"HTTP {code}" in response.error
    assert label in response.error


def test_an_auth_rejection_is_not_retried_into_a_bill():
    """
    A rejected credential will be rejected again. The current policy retries
    every exception, so this records what it actually does rather than what
    would be ideal -- stated as a test so a future change to selective retry
    is a deliberate change to a written expectation, not a silent one.
    """
    calls = {"n": 0}

    def _always_401(*a, **kw):
        calls["n"] += 1
        raise _http_error(401, b'{"error": "unauthorized"}')

    with patch("urllib.request.urlopen", side_effect=_always_401):
        response = _provider(max_retries=2).generate(REQUEST)

    assert response.ok is False
    assert calls["n"] == 3, (
        "today every exception is retried, auth included. If that changes to "
        "fail fast on 4xx, change this expectation deliberately.")
    assert response.attempts == 3


def test_no_failure_path_leaks_the_credential():
    """The key is in a header on every one of these paths."""
    failures = [
        urllib.error.URLError(ssl.SSLCertVerificationError("bad cert")),
        socket.timeout("timed out"),
        _http_error(401, b'{"error": "unauthorized"}'),
        _http_error(500, b"internal"),
    ]
    for boom in failures:
        with patch("urllib.request.urlopen", side_effect=boom):
            response = _provider().generate(REQUEST)
        blob = json.dumps(response.as_dict() if hasattr(response, "as_dict")
                          else response.__dict__, default=str)
        assert "test-key-not-a-real-credential" not in blob, (
            f"the credential reached the response for {type(boom).__name__}")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class _Response:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _raw_response(body: bytes):
    return _Response(body)


def _ok_response(content: str):
    return _Response(json.dumps({
        "id": "chatcmpl-1",
        "choices": [{"message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }).encode("utf-8"))
