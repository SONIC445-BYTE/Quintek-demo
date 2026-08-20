"""
Subscription lifecycle and idempotent webhook processing.

THE RULE THAT MATTERS MOST
--------------------------
A subscription is activated by a VERIFIED GATEWAY EVENT, never by the frontend
saying payment succeeded. The browser reaching a success page proves the
browser reached a success page.

    frontend "success"  ->  nothing happens
    signed webhook      ->  signature checked -> event stored -> entitlement

IDEMPOTENCY
-----------
Gateways retry. Razorpay will resend a webhook it believes was not
acknowledged, sometimes minutes later, sometimes after a deploy. Processing
`subscription.charged` twice would grant two months of allowance for one
payment. So every event is stored under `(gateway, gateway_event_id)` with a
UNIQUE constraint, and the insert IS the lock: a duplicate fails the insert
and is skipped before any state changes.

TRANSITIONS
-----------
Only some state changes make sense. A CANCELLED subscription becoming ACTIVE
because a late `subscription.charged` arrived would resurrect something the
user ended. The table below is checked before any write.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .entitlements import EntitlementEngine
from .gateway import (ACTIVE, ALL_STATES, CANCEL_AT_PERIOD_END, CANCELLED, EXPIRED,
                      PAST_DUE, PAYMENT_FAILED, PENDING, TRIALING, GatewayError,
                      GatewayEvent, SignatureInvalid)
from .plans import PlanStore, now_iso

# What may follow what. A state absent from a set is a transition that will be
# refused and logged rather than applied.
VALID_TRANSITIONS: dict[str, set[str]] = {
    PENDING: {TRIALING, ACTIVE, PAYMENT_FAILED, CANCELLED, EXPIRED},
    TRIALING: {ACTIVE, PAST_DUE, PAYMENT_FAILED, CANCELLED, CANCEL_AT_PERIOD_END, EXPIRED},
    ACTIVE: {PAST_DUE, PAYMENT_FAILED, CANCEL_AT_PERIOD_END, CANCELLED, EXPIRED, ACTIVE},
    PAST_DUE: {ACTIVE, PAYMENT_FAILED, CANCELLED, EXPIRED, CANCEL_AT_PERIOD_END},
    PAYMENT_FAILED: {ACTIVE, CANCELLED, EXPIRED},
    CANCEL_AT_PERIOD_END: {CANCELLED, EXPIRED, ACTIVE},   # ACTIVE = they resumed
    # Terminal. A late event must not resurrect an ended subscription.
    CANCELLED: set(),
    EXPIRED: set(),
}


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class SubscriptionError(RuntimeError):
    pass


@dataclass
class ProcessResult:
    status: str            # PROCESSED | IGNORED | FAILED
    reason: str = ""
    subscription_id: str = ""
    new_state: str = ""

    def as_dict(self) -> dict:
        return {"status": self.status, "reason": self.reason,
                "subscription_id": self.subscription_id, "new_state": self.new_state}


class SubscriptionService:
    def __init__(self, conn: sqlite3.Connection, *, gateway=None,
                 plans: PlanStore | None = None):
        self.conn = conn
        self.gateway = gateway
        self.plans = plans or PlanStore(conn)
        self.entitlements = EntitlementEngine(conn, self.plans)

    # ---------- creation ----------

    def begin_checkout(self, user_id: str, plan_id: str) -> dict:
        """
        Create a PENDING subscription and a checkout session.

        PENDING, not ACTIVE. Nothing is granted until the gateway confirms;
        this row exists so the webhook has something to attach to.
        """
        plan = self.plans.get(plan_id)
        subscription_id = new_id("sub")
        stamp = now_iso()
        self.conn.execute(
            "INSERT INTO subscriptions (id, user_id, plan_id, billing_interval, gateway,"
            " status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (subscription_id, user_id, plan.id, plan.billing_interval,
             getattr(self.gateway, "name", ""), PENDING, stamp, stamp))
        self.conn.commit()

        payload = {"subscription_id": subscription_id, "plan": plan.as_dict(),
                   "status": PENDING}
        if self.gateway is not None:
            # The GATEWAY's id for this plan, not Quintek's. Sending
            # "pro_monthly_v1" to Razorpay names nothing on its side: the call
            # fails, or worse succeeds against some unrelated record. Refusing
            # here keeps the failure at checkout, where it is one person seeing
            # an error, rather than at renewal, where it is silent.
            gateway_name, gateway_plan_id = self.plans.gateway_ref(plan.id)
            if not gateway_plan_id:
                raise GatewayError(
                    f"{plan.id} has no gateway plan id, so no subscription can be"
                    " created for it. Run tools_razorpay_sync.py to create the"
                    " plan on the gateway and record its id.")
            session = self.gateway.create_subscription(
                plan_ref=gateway_plan_id, user_ref=user_id,
                total_count=1 if plan.billing_interval == "annual" else 12)
            self.conn.execute(
                "UPDATE subscriptions SET gateway_subscription_id=?, updated_at=?"
                " WHERE id=?", (session.gateway_subscription_id, now_iso(), subscription_id))
            self.conn.commit()
            payload["checkout"] = session.checkout_payload
        return payload

    # ---------- webhooks ----------

    def handle_webhook(self, body: bytes, signature: str) -> ProcessResult:
        """
        Verify, deduplicate, store, apply. In that order.

        Verification comes first so an unsigned payload never reaches the
        parser. Deduplication comes before application so a retry cannot grant
        a second period.
        """
        if self.gateway is None:
            return ProcessResult("FAILED", "no gateway is configured")

        if not self.gateway.verify_signature(body, signature):
            # Deliberately not stored: an unverified payload is not evidence
            # of anything, and writing it invites someone to process it later.
            raise SignatureInvalid(
                "webhook signature did not verify; the payload is not from the gateway or "
                "was altered in transit")

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            return ProcessResult("FAILED", f"payload is not JSON: {exc}")

        event = self.gateway.parse_event(payload)
        if not event.event_id:
            return ProcessResult(
                "FAILED",
                "the event carries no id, so a replay could not be detected; refusing to "
                "process it")

        # The UNIQUE constraint IS the lock. A duplicate fails here, before
        # anything is applied.
        try:
            self.conn.execute(
                "INSERT INTO webhook_events (id, gateway, gateway_event_id, event_type,"
                " payload, signature_valid, received_at, processing_status)"
                " VALUES (?,?,?,?,?,1,?, 'RECEIVED')",
                (new_id("whk"), event.gateway, event.event_id, event.event_type,
                 body.decode("utf-8", "replace"), now_iso()))
            self.conn.commit()
        except sqlite3.IntegrityError:
            return ProcessResult("IGNORED",
                                 f"event {event.event_id} has already been processed")

        try:
            result = self._apply(event)
            self.conn.execute(
                "UPDATE webhook_events SET processing_status=?, processed_at=?, error=?"
                " WHERE gateway=? AND gateway_event_id=?",
                (result.status, now_iso(), result.reason if result.status == "FAILED" else
                 None, event.gateway, event.event_id))
            self.conn.commit()
            return result
        except Exception as exc:
            self.conn.execute(
                "UPDATE webhook_events SET processing_status='FAILED', processed_at=?,"
                " error=? WHERE gateway=? AND gateway_event_id=?",
                (now_iso(), f"{type(exc).__name__}: {exc}", event.gateway, event.event_id))
            self.conn.commit()
            raise

    def _apply(self, event: GatewayEvent) -> ProcessResult:
        if not event.mapped_status:
            return ProcessResult("IGNORED", f"{event.event_type} needs no state change")

        row = self.conn.execute(
            "SELECT * FROM subscriptions WHERE gateway=? AND gateway_subscription_id=?",
            (event.gateway, event.subscription_ref)).fetchone()
        if row is None:
            return ProcessResult(
                "FAILED",
                f"no subscription matches {event.subscription_ref!r}; the event cannot be "
                "attributed to a customer")

        current, target = row["status"], event.mapped_status
        allowed = VALID_TRANSITIONS.get(current, set())
        if target not in allowed:
            return ProcessResult(
                "IGNORED",
                f"{current} -> {target} is not a permitted transition"
                + (" (this subscription has ended; a late event must not resurrect it)"
                   if current in (CANCELLED, EXPIRED) else ""),
                subscription_id=row["id"], new_state=current)

        self.conn.execute(
            "UPDATE subscriptions SET status=?, current_period_start=COALESCE(?,"
            " current_period_start), current_period_end=COALESCE(?, current_period_end),"
            " updated_at=? WHERE id=?",
            (target, event.period_start, event.period_end, now_iso(), row["id"]))

        # Entitlement follows the money, and only forward.
        if target in (ACTIVE, TRIALING):
            self._grant_entitlement(row["user_id"], row["id"], row["plan_id"])
        elif target in (CANCELLED, EXPIRED):
            self._revoke_entitlement(row["user_id"])

        self.conn.commit()
        return ProcessResult("PROCESSED", f"{current} -> {target}",
                             subscription_id=row["id"], new_state=target)

    # ---------- entitlements ----------

    def _grant_entitlement(self, user_id: str, subscription_id: str, plan_id: str) -> str:
        plan = self.plans.get(plan_id)
        # Close any open entitlement rather than deleting it: what someone was
        # entitled to last month has to stay answerable.
        self.conn.execute(
            "UPDATE entitlements SET effective_until=? WHERE user_id=? AND"
            " effective_until IS NULL", (now_iso(), user_id))
        entitlement_id = new_id("ent")
        self.conn.execute(
            "INSERT INTO entitlements (id, user_id, subscription_id, plan_id, source,"
            " monthly_allowance, daily_limit, session_limit, rollover_balance,"
            " effective_from, created_at) VALUES (?,?,?,?, 'subscription', ?,?,?,0,?,?)",
            (entitlement_id, user_id, subscription_id, plan.id,
             plan.monthly_question_allowance, plan.daily_question_limit,
             plan.session_question_limit, now_iso(), now_iso()))
        return entitlement_id

    def _revoke_entitlement(self, user_id: str) -> None:
        """
        End the paid entitlement. The user falls back to Free, not to nothing.

        `EntitlementEngine.for_user` returns the Free plan when no entitlement
        is open, so closing the row is sufficient and no Free row is written.
        """
        self.conn.execute(
            "UPDATE entitlements SET effective_until=? WHERE user_id=? AND"
            " effective_until IS NULL", (now_iso(), user_id))

    # ---------- user-initiated changes ----------

    def schedule_downgrade(self, user_id: str, target_plan_id: str) -> dict:
        """
        Downgrade at the next billing date, keeping the current plan until then.

        The user paid for this period; taking the allowance away early would
        be taking back something already bought.
        """
        row = self._active_subscription(user_id)
        target = self.plans.get(target_plan_id)
        self.conn.execute(
            "UPDATE subscriptions SET scheduled_plan_id=?, scheduled_effective_at=?,"
            " updated_at=? WHERE id=?",
            (target.id, row["current_period_end"], now_iso(), row["id"]))
        self.conn.commit()
        return {
            "subscription_id": row["id"],
            "current_plan": self.plans.get(row["plan_id"]).name,
            "scheduled_plan": target.name,
            "effective_at": row["current_period_end"],
            "message": (f"Your {self.plans.get(row['plan_id']).name} plan remains active "
                        f"until {(row['current_period_end'] or 'the end of the period')[:10]}. "
                        f"{target.name} will begin on your next billing date."),
        }

    def cancel(self, user_id: str) -> dict:
        """
        Cancel renewal, retain access to the end of the period.

        `cancel_at_period_end` rather than an immediate cancellation, so the
        entitlement stays live until the period the user paid for actually
        ends.
        """
        row = self._active_subscription(user_id)
        if self.gateway is not None and row["gateway_subscription_id"]:
            self.gateway.cancel(row["gateway_subscription_id"], at_period_end=True)
        self.conn.execute(
            "UPDATE subscriptions SET status=?, cancel_at_period_end=1, updated_at=?"
            " WHERE id=?", (CANCEL_AT_PERIOD_END, now_iso(), row["id"]))
        self.conn.commit()
        return {
            "subscription_id": row["id"], "status": CANCEL_AT_PERIOD_END,
            "access_until": row["current_period_end"],
            "message": (f"Your plan will not renew. You keep full access until "
                        f"{(row['current_period_end'] or 'the end of your period')[:10]}, "
                        "after which you move to the Free plan."),
        }

    def _active_subscription(self, user_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM subscriptions WHERE user_id=? AND status NOT IN (?,?)"
            " ORDER BY created_at DESC LIMIT 1", (user_id, CANCELLED, EXPIRED)).fetchone()
        if row is None:
            raise SubscriptionError(f"no active subscription for {user_id!r}")
        return row

    def expire_due(self, *, at: datetime | None = None) -> int:
        """
        Move CANCEL_AT_PERIOD_END subscriptions to EXPIRED once the period ends.

        Run on a schedule. Without it a cancelled subscription keeps its
        entitlement forever, because nothing else moves that state.
        """
        moment = (at or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = self.conn.execute(
            "SELECT * FROM subscriptions WHERE status=? AND current_period_end IS NOT NULL"
            " AND current_period_end <= ?", (CANCEL_AT_PERIOD_END, moment)).fetchall()
        for row in rows:
            self.conn.execute("UPDATE subscriptions SET status=?, updated_at=? WHERE id=?",
                              (EXPIRED, now_iso(), row["id"]))
            self._revoke_entitlement(row["user_id"])
        self.conn.commit()
        return len(rows)
