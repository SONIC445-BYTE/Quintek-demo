"""
The payment gateway boundary.

Quintek talks to `PaymentGateway`. It does not talk to Razorpay. The whole
point is the escape hatch:

    PaymentGateway
          |
          +-- RazorpayAdapter      (today)
          +-- CashfreeAdapter      (later, without touching the app)
          +-- StripeAdapter        (later, ditto)

Two rules the design turns on.

**Gateway states never leak.** Razorpay says `halted`, `authenticated`,
`charged`. Quintek says `PAST_DUE`, `TRIALING`, `ACTIVE`. Every adapter maps
into Quintek's vocabulary, because a gateway's states appearing in application
logic is a gateway you cannot replace -- and the second gateway will have
different words for the same things.

**A payment is real only when the gateway says so.** The frontend reporting
success means the browser reached a success page. The signed webhook, verified
server-side, is the event that grants access. Every method here that could
activate a subscription requires gateway-side evidence.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from typing import Protocol

# Quintek's own subscription states. The gateway's vocabulary maps INTO this.
TRIALING = "TRIALING"
ACTIVE = "ACTIVE"
PAST_DUE = "PAST_DUE"
CANCEL_AT_PERIOD_END = "CANCEL_AT_PERIOD_END"
CANCELLED = "CANCELLED"
EXPIRED = "EXPIRED"
PAYMENT_FAILED = "PAYMENT_FAILED"
PENDING = "PENDING"

ALL_STATES = (TRIALING, ACTIVE, PAST_DUE, CANCEL_AT_PERIOD_END, CANCELLED, EXPIRED,
              PAYMENT_FAILED, PENDING)


class GatewayError(RuntimeError):
    pass


class SignatureInvalid(GatewayError):
    """The payload did not come from the gateway, or was tampered with."""


@dataclass
class CheckoutSession:
    """What the client needs to open the gateway's checkout. No secrets."""

    gateway: str
    subscription_id: str
    gateway_subscription_id: str
    checkout_payload: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"gateway": self.gateway, "subscription_id": self.subscription_id,
                "gateway_subscription_id": self.gateway_subscription_id,
                "checkout": dict(self.checkout_payload)}


@dataclass
class GatewayEvent:
    """A webhook, normalised."""

    gateway: str
    event_id: str
    event_type: str
    subscription_ref: str = ""
    mapped_status: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    amount_minor: int | None = None
    raw: dict = field(default_factory=dict)


class PaymentGateway(Protocol):
    name: str

    def create_subscription(self, *, plan_ref: str, user_ref: str,
                            total_count: int = 12) -> CheckoutSession: ...

    def verify_signature(self, body: bytes, signature: str) -> bool: ...

    def parse_event(self, payload: dict) -> GatewayEvent: ...

    def cancel(self, gateway_subscription_id: str, *,
               at_period_end: bool = True) -> dict: ...


class RazorpayAdapter:
    """
    Razorpay subscriptions.

    HTTP is injected (`transport`) so the mapping and signature logic can be
    tested without a network or live keys -- the parts that must be right are
    the state mapping and the signature check, and both are pure functions of
    their inputs.
    """

    name = "razorpay"

    # Razorpay's vocabulary -> Quintek's. Every value on the right is one of
    # ALL_STATES; nothing else may escape this dict.
    STATUS_MAP = {
        "created": PENDING,
        "authenticated": TRIALING,
        "active": ACTIVE,
        "pending": PAST_DUE,        # a charge failed and is being retried
        "halted": PAYMENT_FAILED,   # retries exhausted
        "cancelled": CANCELLED,
        "completed": EXPIRED,
        "expired": EXPIRED,
        "paused": PAST_DUE,
    }

    EVENT_MAP = {
        "subscription.activated": ACTIVE,
        "subscription.charged": ACTIVE,
        "subscription.authenticated": TRIALING,
        "subscription.pending": PAST_DUE,
        "subscription.halted": PAYMENT_FAILED,
        "subscription.cancelled": CANCELLED,
        "subscription.completed": EXPIRED,
        "subscription.paused": PAST_DUE,
        "payment.failed": PAYMENT_FAILED,
    }

    def __init__(self, *, key_id: str = "", key_secret: str = "",
                 webhook_secret: str = "", transport=None):
        self.key_id = key_id
        self.key_secret = key_secret
        self.webhook_secret = webhook_secret
        self.transport = transport

    # ---------- state mapping ----------

    @classmethod
    def map_status(cls, gateway_status: str) -> str:
        """
        Unknown states map to PENDING, never to ACTIVE.

        A gateway can add a status in a release. Defaulting an unrecognised
        one to ACTIVE would grant paid access on a state nobody has read;
        PENDING withholds it until someone looks.
        """
        return cls.STATUS_MAP.get((gateway_status or "").lower(), PENDING)

    # ---------- signature ----------

    def verify_signature(self, body: bytes, signature: str) -> bool:
        """
        HMAC-SHA256 over the raw body, compared in constant time.

        The RAW bytes matter: re-serialising the JSON changes whitespace and
        key order, and the signature is over what was actually sent. An
        adapter that verifies against a re-encoded payload rejects valid
        webhooks and, worse, tempts someone to disable the check.
        """
        if not self.webhook_secret:
            raise GatewayError(
                "no webhook secret is configured, so no webhook can be trusted. Refusing to "
                "treat an unverified payload as genuine.")
        expected = hmac.new(self.webhook_secret.encode("utf-8"), body,
                            hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, (signature or "").strip())

    # ---------- events ----------

    def parse_event(self, payload: dict) -> GatewayEvent:
        event_type = payload.get("event", "")
        entity = (((payload.get("payload") or {}).get("subscription") or {})
                  .get("entity") or {})
        status = entity.get("status", "")

        # The event type is more specific than the entity's status, so it wins
        # where both are present -- `subscription.halted` is unambiguous while
        # the entity may not have caught up.
        mapped = self.EVENT_MAP.get(event_type) or (self.map_status(status)
                                                    if status else None)

        def stamp(key):
            value = entity.get(key)
            if not value:
                return None
            from datetime import datetime, timezone
            return datetime.fromtimestamp(int(value), timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ")

        return GatewayEvent(
            gateway=self.name,
            # Razorpay puts a unique id on the event; without one there is no
            # idempotency key and a replay cannot be detected.
            event_id=payload.get("id") or payload.get("event_id") or "",
            event_type=event_type,
            subscription_ref=entity.get("id", ""),
            mapped_status=mapped,
            period_start=stamp("current_start"),
            period_end=stamp("current_end"),
            amount_minor=entity.get("amount"),
            raw=payload)

    # ---------- calls ----------

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        if self.transport is None:
            raise GatewayError(
                "no transport is configured on this adapter, so it cannot reach Razorpay. "
                "Inject one in production; tests supply a double.")
        return self.transport(method, path, body, (self.key_id, self.key_secret))

    def create_subscription(self, *, plan_ref: str, user_ref: str,
                            total_count: int = 12) -> CheckoutSession:
        response = self._call("POST", "/v1/subscriptions", {
            "plan_id": plan_ref, "total_count": total_count,
            "customer_notify": 1, "notes": {"quintek_user": user_ref}})
        return CheckoutSession(
            gateway=self.name, subscription_id="",
            gateway_subscription_id=response.get("id", ""),
            # Only what the client needs to open checkout. The key SECRET is
            # never in this payload.
            checkout_payload={"key": self.key_id,
                              "subscription_id": response.get("id", ""),
                              "name": "Quintek", "recurring": 1})

    def cancel(self, gateway_subscription_id: str, *, at_period_end: bool = True) -> dict:
        return self._call("POST", f"/v1/subscriptions/{gateway_subscription_id}/cancel",
                          {"cancel_at_cycle_end": 1 if at_period_end else 0})


def signature_for(body: bytes, secret: str) -> str:
    """Compute a webhook signature. For tests and for local verification."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
