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
            raise GatewayError(
                "no Razorpay credentials are configured, so no call can be made."
                f" Set {ENV_KEY_ID} and {ENV_KEY_SECRET} on the backend.")

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
            # 401 is the one worth naming: it is almost always a rotated or
            # mistyped key, and "Unauthorized" alone sends people hunting
            # through their own code for a bug that is not there.
            hint = (" -- the key id and secret were rejected by Razorpay."
                    " Check that the secret has not been regenerated since it"
                    " was copied." if exc.code == 401 else "")
            raise GatewayError(
                f"Razorpay returned HTTP {exc.code} for {method} {path}{hint}:"
                f" {redact(detail, key_secret)[:400]}") from None
        except urllib.error.URLError as exc:
            raise GatewayError(
                f"Razorpay could not be reached for {method} {path}:"
                f" {redact(str(exc.reason), key_secret)}") from None

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
