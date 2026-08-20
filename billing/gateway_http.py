"""
A real HTTP transport for the Razorpay adapter.

Separate from `gateway.py` on purpose. The adapter holds the parts that must be
correct -- the status mapping and the signature check -- and both are pure
functions that a test can exercise without a network or a live key. This file
holds the part that needs the network, and it is injected, so nothing above it
has to be mocked to be tested.

Credentials are read from the environment and never written anywhere: not to a
log line, not into an exception message, not into the checkout payload the
browser receives. `redact()` exists because the single most common way a
gateway secret escapes is an error message that helpfully includes the request
it failed on.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request

from .gateway import GatewayError

API_BASE = "https://api.razorpay.com"
TIMEOUT_SECONDS = 20

# Environment variable names, in one place. The secret belongs on the backend
# and nowhere else -- not in the repository, not in a frontend build variable.
ENV_KEY_ID = "RAZORPAY_KEY_ID"
ENV_KEY_SECRET = "RAZORPAY_KEY_SECRET"
ENV_WEBHOOK_SECRET = "RAZORPAY_WEBHOOK_SECRET"


# ---------------------------------------------------------------------------
# Failure classes
# ---------------------------------------------------------------------------
# A 401 from Razorpay means two entirely different things and the difference
# decides who has to act.
#
#   "Authentication failed"  -- the key id or secret is wrong. A developer's
#                              problem, fixed by copying the key again.
#   "Unauthorized"           -- the key is CORRECT and the account is not
#                              permitted to call this endpoint. An account
#                              problem, fixed by enabling the product in the
#                              Razorpay dashboard. No amount of re-copying the
#                              key will change it.
#
# Reporting the second as the first sends someone hunting for a typo that does
# not exist -- which is exactly what happened here before these constants did.
AUTH_FAILED = "AUTH_FAILED"
PRODUCT_NOT_ENABLED = "PRODUCT_NOT_ENABLED"
CREDENTIALS_MISSING = "CREDENTIALS_MISSING"
RATE_LIMITED = "RATE_LIMITED"
BAD_REQUEST = "BAD_REQUEST"
GATEWAY_ERROR = "GATEWAY_ERROR"
UNREACHABLE = "UNREACHABLE"
OK = "OK"

# Endpoints every activated account can call, whatever products are enabled.
# Used as a CONTROL: if this works and Subscriptions does not, the credentials
# are not the problem.
CONTROL_PATH = "/v1/orders?count=1"
SUBSCRIPTION_PATH = "/v1/plans?count=1"


def classify(status: int, body: str) -> str:
    """
    Name what a Razorpay error actually is.

    Razorpay's own wording is the signal. An authenticated-but-forbidden call
    returns a bare `{"error":"Unauthorized"}`, while a rejected key returns the
    structured `{"error":{"description":"Authentication failed",...}}`. The
    shapes differ because they come from different layers.
    """
    text = (body or "").lower()
    if status == 401:
        if "authentication failed" in text or "provide your api key" in text:
            return AUTH_FAILED
        return PRODUCT_NOT_ENABLED
    if status == 429:
        return RATE_LIMITED
    if status in (400, 404, 422):
        return BAD_REQUEST
    if status >= 500:
        return GATEWAY_ERROR
    return OK if status < 400 else GATEWAY_ERROR


REMEDIES = {
    AUTH_FAILED: ("the key id or secret was rejected. Copy them again from the"
                  " Razorpay dashboard -- a regenerated secret or a trailing"
                  " newline is the usual cause."),
    PRODUCT_NOT_ENABLED: ("the credentials are VALID but this account is not"
                          " permitted to call this API. For Plans and"
                          " Subscriptions this means the Subscriptions product"
                          " has not been enabled on the account; enable it from"
                          " the Razorpay dashboard. Re-copying the key will not"
                          " help."),
    CREDENTIALS_MISSING: f"set {ENV_KEY_ID} and {ENV_KEY_SECRET} on the backend.",
    RATE_LIMITED: "too many requests; retry with backoff.",
    BAD_REQUEST: "the request was rejected on its contents, not its credentials.",
    GATEWAY_ERROR: "Razorpay returned a server error; retry later.",
    UNREACHABLE: "Razorpay could not be reached from this network.",
}


def redact(text: str, *secrets: str) -> str:
    """Remove anything secret from text that is about to be shown to someone."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


class HttpTransport:
    """
    `transport(method, path, body, auth) -> dict`, over urllib.

    Deliberately stdlib. A payment integration that needs an install step to
    start is one more thing to get wrong on the day it has to work.
    """

    def __init__(self, base: str = API_BASE, *, timeout: int = TIMEOUT_SECONDS,
                 opener=None):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.opener = opener or urllib.request.urlopen

    def __call__(self, method: str, path: str, body: dict | None,
                 auth: tuple[str, str]) -> dict:
        key_id, key_secret = auth
        if not key_id or not key_secret:
            error = GatewayError(
                "no Razorpay credentials are configured, so no call can be made."
                f" Set {ENV_KEY_ID} and {ENV_KEY_SECRET} on the backend.")
            error.failure_class = CREDENTIALS_MISSING
            raise error from None

        url = f"{self.base}{path}"
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        token = base64.b64encode(f"{key_id}:{key_secret}".encode("utf-8")).decode("ascii")
        request = urllib.request.Request(url, data=payload, method=method)
        request.add_header("Authorization", f"Basic {token}")
        request.add_header("Content-Type", "application/json")
        request.add_header("Accept", "application/json")

        try:
            with self.opener(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8")
            except Exception:                      # noqa: BLE001 -- best effort
                detail = exc.reason or ""
            failure = classify(exc.code, detail)
            error = GatewayError(
                f"Razorpay returned HTTP {exc.code} for {method} {path}"
                f" [{failure}] -- {REMEDIES.get(failure, '')}"
                f" Response: {redact(detail, key_secret)[:400]}")
            error.failure_class = failure
            error.status_code = exc.code
            raise error from None
        except urllib.error.URLError as exc:
            error = GatewayError(
                f"Razorpay could not be reached for {method} {path}"
                f" [{UNREACHABLE}]: {redact(str(exc.reason), key_secret)}")
            error.failure_class = UNREACHABLE
            raise error from None

        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            raise GatewayError(
                f"Razorpay returned a non-JSON body for {method} {path}:"
                f" {redact(raw, key_secret)[:200]}") from None


def credentials_from_env(env: dict | None = None) -> dict:
    """
    What is configured, WITHOUT revealing any of it.

    Returns presence and shape only. Deployments get this wrong constantly --
    a live key in a test environment, a trailing newline from a copy-paste --
    and every one of those is diagnosable without printing the value.
    """
    env = env if env is not None else os.environ
    key_id = (env.get(ENV_KEY_ID) or "").strip()
    secret = (env.get(ENV_KEY_SECRET) or "").strip()
    webhook = (env.get(ENV_WEBHOOK_SECRET) or "").strip()
    mode = ("test" if key_id.startswith("rzp_test_")
            else "live" if key_id.startswith("rzp_live_")
            else "unknown" if key_id else "absent")
    return {
        "key_id_present": bool(key_id),
        "key_secret_present": bool(secret),
        "webhook_secret_present": bool(webhook),
        "mode": mode,
        "key_id_length": len(key_id),
        "key_secret_length": len(secret),
        "whitespace_in_raw_values": any(
            (env.get(name) or "") != (env.get(name) or "").strip()
            for name in (ENV_KEY_ID, ENV_KEY_SECRET, ENV_WEBHOOK_SECRET)),
    }


def adapter_from_env(env: dict | None = None, *, transport=None):
    """
    Build a configured `RazorpayAdapter`, or `None` when nothing is configured.

    `None` rather than an adapter with empty keys: an adapter that exists but
    cannot authenticate fails at the worst possible moment, which is the middle
    of somebody's checkout. Absent credentials should disable the payment
    surface, not arm it.
    """
    from .gateway import RazorpayAdapter

    env = env if env is not None else os.environ
    key_id = (env.get(ENV_KEY_ID) or "").strip()
    secret = (env.get(ENV_KEY_SECRET) or "").strip()
    if not key_id or not secret:
        return None
    return RazorpayAdapter(
        key_id=key_id, key_secret=secret,
        webhook_secret=(env.get(ENV_WEBHOOK_SECRET) or "").strip(),
        transport=transport or HttpTransport())


def diagnose(adapter) -> dict:
    """
    Tell a credential problem apart from an account problem, with a control.

    Calls an endpoint every activated account can reach BEFORE calling the one
    that matters. Without the control, an "Unauthorized" on /v1/plans is
    indistinguishable from a bad key, and the wrong person spends an afternoon
    re-copying a key that was right all along.
    """
    result = {"control": None, "subscriptions": None,
              "credentials_valid": None, "subscriptions_enabled": None}

    def probe(path):
        try:
            adapter._call("GET", path, None)
            return OK, ""
        except GatewayError as exc:
            return getattr(exc, "failure_class", GATEWAY_ERROR), str(exc)

    result["control"], control_detail = probe(CONTROL_PATH)
    result["subscriptions"], subs_detail = probe(SUBSCRIPTION_PATH)
    result["control_detail"] = control_detail
    result["subscriptions_detail"] = subs_detail

    result["credentials_valid"] = result["control"] == OK
    result["subscriptions_enabled"] = result["subscriptions"] == OK

    if result["credentials_valid"] and not result["subscriptions_enabled"]:
        result["verdict"] = PRODUCT_NOT_ENABLED
        result["remedy"] = REMEDIES[PRODUCT_NOT_ENABLED]
    elif not result["credentials_valid"]:
        result["verdict"] = result["control"]
        result["remedy"] = REMEDIES.get(result["control"], "")
    else:
        result["verdict"] = OK
        result["remedy"] = ""
    return result
