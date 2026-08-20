"""
The Razorpay HTTP transport.

Nothing here reaches the network. What is tested is the part that goes wrong
in production and cannot be tested against a live gateway: what happens to the
SECRET when a call fails, and what an absent credential does.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from billing.gateway import GatewayError
from billing.gateway_http import (ENV_KEY_ID, ENV_KEY_SECRET, ENV_WEBHOOK_SECRET,
                                  HttpTransport, adapter_from_env,
                                  credentials_from_env, redact)

SECRET = "s3cr3t-key-material"


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def opener_returning(payload, captured=None):
    def opener(request, timeout=None):
        if captured is not None:
            captured.append(request)
        return FakeResponse(json.dumps(payload).encode("utf-8"))
    return opener


def opener_raising(exc):
    def opener(request, timeout=None):
        raise exc
    return opener


# ------------------------------------------------------------------ requests

def test_a_successful_call_returns_the_parsed_body() -> None:
    transport = HttpTransport(opener=opener_returning({"id": "plan_123"}))
    assert transport("POST", "/v1/plans", {"period": "monthly"},
                     ("rzp_test_x", SECRET)) == {"id": "plan_123"}


def test_the_request_carries_basic_auth_and_json_headers() -> None:
    captured: list = []
    transport = HttpTransport(opener=opener_returning({}, captured))
    transport("GET", "/v1/plans", None, ("rzp_test_x", SECRET))
    request = captured[0]
    assert request.get_header("Authorization").startswith("Basic ")
    assert request.get_header("Content-type") == "application/json"
    assert request.get_method() == "GET"
    assert request.data is None, "a GET must not carry a body"


def test_a_body_is_sent_as_json_bytes() -> None:
    captured: list = []
    transport = HttpTransport(opener=opener_returning({}, captured))
    transport("POST", "/v1/plans", {"amount": 49900}, ("rzp_test_x", SECRET))
    assert json.loads(captured[0].data) == {"amount": 49900}


# ------------------------------------------------------------------ secrets

def test_redact_removes_the_secret() -> None:
    assert SECRET not in redact(f"failed with {SECRET} at the end", SECRET)


def test_an_http_error_never_echoes_the_secret() -> None:
    """
    The commonest way a gateway secret escapes: an error message that helpfully
    includes the request it failed on.
    """
    error = urllib.error.HTTPError(
        "https://api.razorpay.com/v1/plans", 400, "Bad Request", {},
        io.BytesIO(f'{{"error":"bad key {SECRET}"}}'.encode("utf-8")))
    transport = HttpTransport(opener=opener_raising(error))
    with pytest.raises(GatewayError) as exc:
        transport("POST", "/v1/plans", {}, ("rzp_test_x", SECRET))
    assert SECRET not in str(exc.value)
    assert "***" in str(exc.value)


def test_a_network_error_never_echoes_the_secret() -> None:
    transport = HttpTransport(opener=opener_raising(
        urllib.error.URLError(f"connection refused ({SECRET})")))
    with pytest.raises(GatewayError) as exc:
        transport("POST", "/v1/plans", {}, ("rzp_test_x", SECRET))
    assert SECRET not in str(exc.value)


def test_a_non_json_body_never_echoes_the_secret() -> None:
    def opener(request, timeout=None):
        return FakeResponse(f"<html>{SECRET}</html>".encode("utf-8"))
    with pytest.raises(GatewayError) as exc:
        HttpTransport(opener=opener)("GET", "/v1/plans", None, ("x", SECRET))
    assert SECRET not in str(exc.value)


def test_a_401_says_what_it_usually_means() -> None:
    """
    "Unauthorized" alone sends people hunting through their own code for a bug
    that is not there. A rejected key is nearly always a rotated or mistyped
    one.
    """
    error = urllib.error.HTTPError(
        "https://api.razorpay.com/v1/plans", 401, "Unauthorized", {},
        io.BytesIO(b'{"error":{"description":"Authentication failed"}}'))
    with pytest.raises(GatewayError) as exc:
        HttpTransport(opener=opener_raising(error))(
            "GET", "/v1/plans", None, ("rzp_test_x", SECRET))
    message = str(exc.value)
    assert "401" in message and "regenerated" in message


# ------------------------------------------------------------------ config

def test_a_call_without_credentials_refuses_before_the_network() -> None:
    called = []

    def opener(request, timeout=None):
        called.append(request)
        return FakeResponse(b"{}")

    with pytest.raises(GatewayError) as exc:
        HttpTransport(opener=opener)("GET", "/v1/plans", None, ("", ""))
    assert ENV_KEY_ID in str(exc.value)
    assert not called, "an unauthenticated call must not reach the network"


def test_credentials_report_presence_without_revealing_anything() -> None:
    report = credentials_from_env({ENV_KEY_ID: "rzp_test_abc",
                                   ENV_KEY_SECRET: SECRET})
    assert report["mode"] == "test"
    assert report["key_secret_present"] is True
    assert SECRET not in json.dumps(report)
    assert "rzp_test_abc" not in json.dumps(report)


def test_a_live_key_is_named_as_live() -> None:
    assert credentials_from_env({ENV_KEY_ID: "rzp_live_abc"})["mode"] == "live"


def test_an_unrecognised_prefix_is_unknown_not_test() -> None:
    assert credentials_from_env({ENV_KEY_ID: "something_else"})["mode"] == "unknown"


def test_stray_whitespace_is_reported_because_it_causes_401s() -> None:
    report = credentials_from_env({ENV_KEY_ID: "rzp_test_abc\n",
                                   ENV_KEY_SECRET: SECRET})
    assert report["whitespace_in_raw_values"] is True
    assert report["mode"] == "test", "the value is still usable once stripped"


def test_no_credentials_means_no_adapter_rather_than_a_broken_one() -> None:
    """
    An adapter that exists but cannot authenticate fails in the middle of
    somebody's checkout. Absent credentials disable the payment surface.
    """
    assert adapter_from_env({}) is None
    assert adapter_from_env({ENV_KEY_ID: "rzp_test_abc"}) is None
    assert adapter_from_env({ENV_KEY_SECRET: SECRET}) is None


def test_full_credentials_build_a_usable_adapter() -> None:
    adapter = adapter_from_env({ENV_KEY_ID: "rzp_test_abc",
                                ENV_KEY_SECRET: SECRET,
                                ENV_WEBHOOK_SECRET: "whsec"},
                               transport=opener_returning({}))
    assert adapter is not None
    assert adapter.key_id == "rzp_test_abc"
    assert adapter.webhook_secret == "whsec"


def test_credentials_are_stripped_before_use() -> None:
    adapter = adapter_from_env({ENV_KEY_ID: " rzp_test_abc\n",
                                ENV_KEY_SECRET: SECRET + "\n"},
                               transport=opener_returning({}))
    assert adapter.key_id == "rzp_test_abc"
    assert adapter.key_secret == SECRET
