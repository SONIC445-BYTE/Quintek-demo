"""
Billing HTTP surface. Transport-independent, like `student/api.py`.

The routes divide into three audiences and the division is enforced:

  * **Public** -- `/pricing`. No auth. Plans and prices only.
  * **Learner** -- `/me/*`. Their own entitlement, usage and subscription.
  * **Admin** -- `/admin/economics/*`. Revenue, AI cost, contribution.

`/me/entitlements` is DISPLAY. Authorisation happens in
`/me/usage/reserve`, server-side, against freshly read counters. A client that
posts back a remaining count it computed itself is ignored -- the request body
for a reservation carries only how many questions are wanted.

Nothing under `/pricing` or `/me/*` exposes a provider, model, token or cost
figure. Those are admin routes because a learner buys Quintek, not access to
an inference market, and publishing the cost base helps only competitors.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .costs import CostLedger
from .economics import EconomicsService
from .entitlements import EntitlementEngine
from .plans import PlanStore
from .subscriptions import SubscriptionService
from .usage import InsufficientAllowance, ReservationError, UsageService


class ApiError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.payload = {"error": message, **extra}


class BillingAPI:
    def __init__(self, conn: sqlite3.Connection, *, gateway=None,
                 economics: EconomicsService | None = None):
        self.conn = conn
        self.plans = PlanStore(conn)
        self.entitlements = EntitlementEngine(conn, self.plans)
        self.usage = UsageService(conn, self.entitlements)
        self.subscriptions = SubscriptionService(conn, gateway=gateway, plans=self.plans)
        self.costs = CostLedger(conn)
        self.economics = economics or EconomicsService(conn, plans=self.plans,
                                                       costs=self.costs)

    # ---------- dispatch ----------

    def handle(self, method: str, path: str, params: dict | None = None,
               body: dict | None = None, *, user_id: str | None = None,
               is_admin: bool = False, raw_body: bytes = b"",
               signature: str = "") -> tuple[int, Any]:
        params, body = params or {}, body or {}
        segments = [s for s in path.strip("/").split("/") if s]
        try:
            return self._route(method, segments, params, body, user_id, is_admin,
                               raw_body, signature)
        except ApiError as exc:
            return exc.status, exc.payload
        except InsufficientAllowance as exc:
            # A refusal the UI must render helpfully, not a server error.
            return 402, {"error": str(exc), "available": exc.available,
                         "requested": exc.requested,
                         "actions": ["upgrade", "view_usage"]}
        except (ReservationError, ValueError) as exc:
            return 400, {"error": str(exc)}

    def _route(self, method, seg, params, body, user_id, is_admin, raw_body, signature):
        # ---- public ----
        if seg == ["pricing"] and method == "GET":
            return 200, self.plans.pricing_page()

        # ---- webhooks (authenticated by signature, not by session) ----
        if len(seg) == 2 and seg[0] == "webhooks" and method == "POST":
            result = self.subscriptions.handle_webhook(raw_body, signature)
            # 200 even for IGNORED: the gateway must stop retrying an event we
            # have already seen. Only a genuine processing failure asks for a
            # retry.
            return (500 if result.status == "FAILED" else 200), result.as_dict()

        # ---- learner ----
        if seg and seg[0] == "me":
            if not user_id:
                raise ApiError(401, "sign in to view your plan")
            return self._me(method, seg[1:], params, body, user_id)

        # ---- admin ----
        if seg and seg[0] == "admin":
            if not is_admin:
                # 404, not 403: whether an admin surface exists is itself
                # something a learner does not need to learn.
                raise ApiError(404, f"no such endpoint: {method} /{'/'.join(seg)}")
            return self._admin(method, seg[1:], params, body)

        raise ApiError(404, f"no such endpoint: {method} /{'/'.join(seg)}")

    # ---------- learner routes ----------

    def _me(self, method, seg, params, body, user_id):
        if seg == ["entitlements"] and method == "GET":
            return 200, self.entitlements.snapshot(user_id)

        if seg == ["usage"] and method == "GET":
            snapshot = self.entitlements.snapshot(user_id)
            return 200, {
                **snapshot,
                # The dashboard's own framing, so the client does no arithmetic.
                "this_month": {"used": snapshot["monthly_used"],
                               "allowance": snapshot["monthly_allowance"],
                               "remaining": snapshot["monthly_remaining"]},
                "today": {"used": snapshot["daily_used"],
                          "limit": snapshot["daily_limit"],
                          "remaining": snapshot["daily_remaining"]},
                "session": {"limit": snapshot["session_limit"],
                            "available_now": snapshot["available_now"]},
            }

        if seg == ["usage", "check"] and method == "POST":
            requested = int(body.get("questions", 0))
            return 200, self.entitlements.authorize(user_id, requested).as_dict()

        if seg == ["usage", "reserve"] and method == "POST":
            # Only `questions` is read. Any remaining count the client sends
            # is ignored -- the server recomputes from the ledger.
            requested = int(body.get("questions", 0))
            reservation = self.usage.reserve(
                user_id, requested,
                question_type=str(body.get("question_type", "mcq")),
                allow_partial=bool(body.get("allow_partial", True)))
            return 201, reservation.as_dict()

        if len(seg) == 3 and seg[:2] == ["usage", "reservations"] and method == "POST":
            raise ApiError(400, "specify an action: /me/usage/reservations/<id>/commit")

        if len(seg) == 4 and seg[:2] == ["usage", "reservations"] and method == "POST":
            reservation_id, action = seg[2], seg[3]
            self._own_reservation(user_id, reservation_id)
            if action == "commit":
                return 200, self.usage.commit(
                    reservation_id, actual_units=body.get("actual_units"),
                    question_type=str(body.get("question_type", "mcq")))
            if action == "release":
                return 200, self.usage.release(reservation_id,
                                               reason=str(body.get("reason", "")))
            raise ApiError(404, f"no such action: {action}")

        if seg == ["subscription"] and method == "GET":
            return 200, self._subscription_view(user_id)

        if seg == ["subscription", "checkout"] and method == "POST":
            plan_id = body.get("plan_id")
            if not plan_id:
                raise ApiError(400, "a plan_id is required")
            return 201, self.subscriptions.begin_checkout(user_id, str(plan_id))

        if seg == ["subscription", "cancel"] and method == "POST":
            return 200, self.subscriptions.cancel(user_id)

        if seg == ["subscription", "downgrade"] and method == "POST":
            plan_id = body.get("plan_id")
            if not plan_id:
                raise ApiError(400, "a plan_id is required")
            return 200, self.subscriptions.schedule_downgrade(user_id, str(plan_id))

        if seg == ["subscription", "refund"] and method == "POST":
            # Deliberately not implemented. Refunds are a manual decision.
            raise ApiError(
                403,
                "refunds are handled by support, not from the app. A refund is a manual "
                "decision followed by a gateway refund and a reconciliation; automating it "
                "from a client request would let anyone trigger a payout.")

        raise ApiError(404, f"no such endpoint: {method} /me/{'/'.join(seg)}")

    def _own_reservation(self, user_id: str, reservation_id: str) -> None:
        row = self.conn.execute("SELECT user_id FROM reservations WHERE id=?",
                                (reservation_id,)).fetchone()
        if row is None or row["user_id"] != user_id:
            # 404 rather than 403: whether someone else's reservation exists
            # is not a learner's business.
            raise ApiError(404, "no such reservation")

    def _subscription_view(self, user_id: str) -> dict:
        """The Billing screen, reached from More."""
        row = self.conn.execute(
            "SELECT s.*, p.name AS plan_name, p.family, p.price_minor, p.currency"
            " FROM subscriptions s JOIN plans p ON p.id = s.plan_id"
            " WHERE s.user_id=? ORDER BY s.created_at DESC LIMIT 1", (user_id,)).fetchone()
        snapshot = self.entitlements.snapshot(user_id)

        if row is None:
            return {"plan": "free", "plan_name": "Free", "status": "ACTIVE",
                    "manageable": False, "usage": snapshot,
                    "message": "You are on the Free plan.",
                    "actions": ["view_plans"]}

        from .money import Money
        scheduled = None
        if row["scheduled_plan_id"]:
            scheduled = {"plan": self.plans.get(row["scheduled_plan_id"]).name,
                         "effective_at": row["scheduled_effective_at"]}
        return {
            "plan": row["family"], "plan_name": row["plan_name"],
            "status": row["status"],
            "billing_interval": row["billing_interval"],
            "price_display": Money(row["price_minor"], row["currency"]).format(decimals=False),
            "current_period_end": row["current_period_end"],
            "cancel_at_period_end": bool(row["cancel_at_period_end"]),
            "scheduled_change": scheduled,
            "manageable": row["status"] in ("ACTIVE", "TRIALING", "PAST_DUE"),
            "usage": snapshot,
            "actions": ["change_plan", "cancel"] if row["status"] in ("ACTIVE", "TRIALING")
                       else ["view_plans"],
        }

    # ---------- admin routes ----------

    def _admin(self, method, seg, params, body):
        if seg == ["economics"] and method == "GET":
            return 200, self.economics.daily(params.get("day"))
        if seg == ["economics", "plans"] and method == "GET":
            return 200, {"plans": self.economics.plan_economics(since=params.get("since"))}
        if seg == ["economics", "cost-per-500"] and method == "GET":
            return 200, self.economics.cost_per_500_accepted(since=params.get("since"))
        if seg == ["economics", "models"] and method == "GET":
            return 200, {"models": self.costs.by_model(since=params.get("since"))}
        raise ApiError(404, f"no such endpoint: {method} /admin/{'/'.join(seg)}")
