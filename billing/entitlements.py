"""
What a user is entitled to right now, and whether a request fits.

THE DISTINCTION THIS MODULE EXISTS TO PRESERVE
----------------------------------------------
Monthly allowance and daily cap are not the same pool, and confusing them is
the single most likely bug in a usage system.

A Pro user with 5,000 monthly and 300 daily who has used 250 today has:

    monthly remaining  4,750
    daily remaining       50

The 4,750 cannot be spent today. The daily cap is a safety mechanism, not a
slice of the monthly figure, and `available_now()` returns the MINIMUM of the
three constraints -- monthly, daily, session -- never the largest or the sum.

RESERVATIONS COUNT AS SPENT
---------------------------
Held reservations are subtracted from every remaining figure. A batch that has
been authorised but not yet finished has already committed that capacity; if
it did not count, two concurrent requests could each see the same headroom.
See `billing/usage.py` for the transaction that makes that safe.

THE FRONTEND NEVER DECIDES
--------------------------
`snapshot()` produces the payload `GET /me/entitlements` returns. It is for
DISPLAY. Authorisation happens in `authorize()`, server-side, at request time,
against freshly read counters -- never against a number the client sent back.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from .plans import Plan, PlanStore, now_iso


def today_iso(at: datetime | None = None) -> str:
    return (at or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


def period_start_for(at: datetime | None = None, *, anchor_day: int = 1) -> str:
    """
    The billing period a moment falls in.

    Defaults to calendar months. A real subscription anchors to its own start
    date; `anchor_day` exists so that is expressible without changing callers.
    """
    moment = at or datetime.now(timezone.utc)
    if moment.day >= anchor_day:
        start = moment.replace(day=min(anchor_day, 28))
    else:
        previous = moment.replace(day=1) - timedelta(days=1)
        start = previous.replace(day=min(anchor_day, 28))
    return start.strftime("%Y-%m-%d")


@dataclass
class Entitlement:
    user_id: str
    plan: Plan
    monthly_allowance: int
    daily_limit: int
    session_limit: int
    rollover_balance: int = 0
    subscription_id: str | None = None
    status: str = "ACTIVE"

    @property
    def effective_monthly(self) -> int:
        """Base allowance plus whatever rolled over."""
        return self.monthly_allowance + self.rollover_balance


@dataclass
class Availability:
    """
    The three constraints, and what actually binds.

    `binding_constraint` is carried because the UI has to explain a refusal,
    and "you have 3,160 left this month but only 173 today" is only sayable if
    the system knows which number stopped the request.
    """

    monthly_allowance: int
    monthly_used: int
    daily_limit: int
    daily_used: int
    session_limit: int
    rollover_balance: int
    held: int = 0

    @property
    def monthly_remaining(self) -> int:
        return max(0, self.monthly_allowance + self.rollover_balance - self.monthly_used)

    @property
    def daily_remaining(self) -> int:
        return max(0, self.daily_limit - self.daily_used)

    @property
    def available_now(self) -> int:
        """
        The most a single request may consume, right now.

        The MINIMUM of the three. Never the sum, never the monthly figure --
        that is the confusion this whole module is arranged to prevent.
        """
        return min(self.monthly_remaining, self.daily_remaining, self.session_limit)

    @property
    def binding_constraint(self) -> str:
        candidates = [
            ("daily", self.daily_remaining),
            ("monthly", self.monthly_remaining),
            ("session", self.session_limit),
        ]
        return min(candidates, key=lambda pair: pair[1])[0]

    def as_dict(self) -> dict:
        return {
            "monthly_allowance": self.monthly_allowance,
            "monthly_used": self.monthly_used,
            "monthly_remaining": self.monthly_remaining,
            "daily_limit": self.daily_limit,
            "daily_used": self.daily_used,
            "daily_remaining": self.daily_remaining,
            "session_limit": self.session_limit,
            "rollover": self.rollover_balance,
            "held_in_flight": self.held,
            "available_now": self.available_now,
            "binding_constraint": self.binding_constraint,
        }


@dataclass
class Decision:
    """The answer to "may this request proceed, and for how much?"."""

    allowed: bool
    granted: int
    requested: int
    reason: str
    availability: Availability
    partial: bool = False
    actions: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"allowed": self.allowed, "granted": self.granted, "requested": self.requested,
                "partial": self.partial, "reason": self.reason, "actions": list(self.actions),
                "availability": self.availability.as_dict()}


# Subscription states that may consume. PAST_DUE is deliberately included:
# a failed renewal should not cut a paying customer off mid-study while the
# gateway retries, and the grace window is a business decision made here
# rather than an accident of which states happened to be checked.
CONSUMING_STATES = {"TRIALING", "ACTIVE", "CANCEL_AT_PERIOD_END", "PAST_DUE"}


class EntitlementEngine:
    def __init__(self, conn: sqlite3.Connection, plans: PlanStore | None = None):
        self.conn = conn
        self.plans = plans or PlanStore(conn)

    # ---------- resolving the entitlement ----------

    def for_user(self, user_id: str, *, at: datetime | None = None) -> Entitlement:
        """
        The user's current entitlement, defaulting to Free.

        Everyone has an entitlement. A user with no subscription is not an
        error state, they are on Free -- and returning None here would push a
        null check into every caller and eventually one of them would forget.
        """
        row = self.conn.execute(
            "SELECT e.*, s.status AS sub_status FROM entitlements e"
            " LEFT JOIN subscriptions s ON s.id = e.subscription_id"
            " WHERE e.user_id = ? AND (e.effective_until IS NULL OR e.effective_until > ?)"
            " ORDER BY e.effective_from DESC LIMIT 1",
            (user_id, now_iso())).fetchone()

        if row is None:
            free = self.plans.free_plan()
            return Entitlement(
                user_id=user_id, plan=free,
                monthly_allowance=free.monthly_question_allowance,
                daily_limit=free.daily_question_limit,
                session_limit=free.session_question_limit,
                status="ACTIVE")

        plan = self.plans.get(row["plan_id"])
        status = row["sub_status"] or "ACTIVE"
        if status not in CONSUMING_STATES:
            # Expired or cancelled: fall back to Free rather than to nothing.
            free = self.plans.free_plan()
            return Entitlement(
                user_id=user_id, plan=free,
                monthly_allowance=free.monthly_question_allowance,
                daily_limit=free.daily_question_limit,
                session_limit=free.session_question_limit,
                subscription_id=row["subscription_id"], status=status)

        return Entitlement(
            user_id=user_id, plan=plan,
            monthly_allowance=row["monthly_allowance"], daily_limit=row["daily_limit"],
            session_limit=row["session_limit"], rollover_balance=row["rollover_balance"],
            subscription_id=row["subscription_id"], status=status)

    # ---------- counting ----------

    def availability(self, user_id: str, *, at: datetime | None = None) -> Availability:
        entitlement = self.for_user(user_id, at=at)
        day = today_iso(at)
        period = period_start_for(at)

        monthly_used = self.conn.execute(
            "SELECT COALESCE(SUM(question_units),0) AS n FROM usage_ledger"
            " WHERE user_id=? AND period_start=?", (user_id, period)).fetchone()["n"]
        daily_used = self.conn.execute(
            "SELECT COALESCE(SUM(question_units),0) AS n FROM usage_ledger"
            " WHERE user_id=? AND usage_date=?", (user_id, day)).fetchone()["n"]

        # Held reservations are already spent as far as any new request is
        # concerned. Omitting this is precisely how two concurrent batches
        # both pass the same check.
        held_today = self.conn.execute(
            "SELECT COALESCE(SUM(question_units),0) AS n FROM reservations"
            " WHERE user_id=? AND status='HELD' AND usage_date=? AND expires_at > ?",
            (user_id, day, now_iso())).fetchone()["n"]
        held_period = self.conn.execute(
            "SELECT COALESCE(SUM(question_units),0) AS n FROM reservations"
            " WHERE user_id=? AND status='HELD' AND period_start=? AND expires_at > ?",
            (user_id, period, now_iso())).fetchone()["n"]

        return Availability(
            monthly_allowance=entitlement.monthly_allowance,
            monthly_used=monthly_used + held_period,
            daily_limit=entitlement.daily_limit,
            daily_used=daily_used + held_today,
            session_limit=entitlement.session_limit,
            rollover_balance=entitlement.rollover_balance,
            held=held_today)

    # ---------- authorising ----------

    def authorize(self, user_id: str, requested: int, *,
                  at: datetime | None = None, allow_partial: bool = True) -> Decision:
        """
        May this user generate `requested` questions now?

        Partial grants are the default, per the product decision: a user who
        asks for 500 with 150 left is offered the 150 rather than refused.
        What never happens is granting more than the backend authorises --
        `granted` is capped at `available_now` in every branch.
        """
        availability = self.availability(user_id, at=at)
        entitlement = self.for_user(user_id, at=at)

        if requested < 1:
            return Decision(False, 0, requested, "a request must be for at least one question",
                            availability)

        if entitlement.status not in CONSUMING_STATES:
            return Decision(
                False, 0, requested,
                f"subscription is {entitlement.status}; Free-plan limits apply",
                availability, actions=["view_plans"])

        available = availability.available_now
        if available <= 0:
            binding = availability.binding_constraint
            reason = {
                "daily": f"You have used your {availability.daily_limit} questions for today. "
                         "Your daily allowance resets tomorrow.",
                "monthly": "You have used your monthly allowance.",
                "session": "This plan does not permit generating questions.",
            }[binding]
            return Decision(False, 0, requested, reason, availability,
                            actions=["upgrade", "view_usage"])

        if requested <= available:
            return Decision(True, requested, requested, "within your current allowance",
                            availability)

        if not allow_partial:
            return Decision(False, 0, requested,
                            f"only {available} questions are available right now",
                            availability, actions=["upgrade", "view_usage"])

        # Partial. The message names the constraint that actually bound, so
        # the user is not told "monthly limit" when it was the daily cap.
        binding = availability.binding_constraint
        explanation = {
            "daily": (f"You can generate {available} questions today. Your plan allows up to "
                      f"{availability.session_limit} per session, but your remaining daily "
                      f"allowance is {availability.daily_remaining}."),
            "monthly": (f"You can generate {available} questions. That is what remains of your "
                        "monthly allowance."),
            "session": (f"You can generate {available} questions in one session. Your plan's "
                        f"session limit is {availability.session_limit}."),
        }[binding]
        return Decision(True, available, requested, explanation, availability,
                        partial=True, actions=["generate_available", "upgrade", "view_usage"])

    # ---------- the display payload ----------

    def snapshot(self, user_id: str, *, at: datetime | None = None) -> dict:
        """
        `GET /me/entitlements`. Display only -- never authorisation.

        Carries no provider, model, token or cost figure. What a question cost
        Quintek to produce is an admin concern.
        """
        entitlement = self.for_user(user_id, at=at)
        availability = self.availability(user_id, at=at)
        return {
            "plan": entitlement.plan.family,
            "plan_name": entitlement.plan.name,
            "billing_interval": entitlement.plan.billing_interval,
            "status": entitlement.status,
            **availability.as_dict(),
            # Restated for the client, which should not have to recompute it.
            "can_generate_now": availability.available_now > 0,
        }
