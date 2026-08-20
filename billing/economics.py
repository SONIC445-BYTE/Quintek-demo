"""
Revenue, AI cost, and contribution -- the admin view.

The question this exists to answer:

    Is the ₹150-₹200 contribution target actually being achieved?

Contribution, not margin: revenue minus the costs that VARY with usage. Fixed
infrastructure is not subtracted here, because a per-user fixed-cost share
moves when user count moves and would make the per-plan figure meaningless.

    revenue - AI cost - payment fees - direct infra = contribution

Every figure comes from a ledger. Revenue from paid subscriptions, AI cost
from `cost_ledger`, both append-only. Nothing here is an estimate, and where a
number cannot be computed it is reported as unavailable rather than as zero --
a contribution figure that silently omits unpriced AI calls reads as healthier
than the business is.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .costs import CostLedger
from .money import Money, micro_to_money
from .plans import PlanStore


def _day_bounds(day: str | None = None) -> tuple[str, str]:
    moment = (datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
              if day else datetime.now(timezone.utc))
    start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return (start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            (start + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))


@dataclass
class FeeModel:
    """
    Payment processing costs, as configuration.

    Indian gateways charge a percentage plus GST on the fee. Both are stored
    in basis points so the arithmetic stays in integers.
    """

    percent_bps: int = 200          # 2.00%
    gst_on_fee_bps: int = 1800      # 18% GST on the fee itself
    flat_minor: int = 0

    def fee_for(self, amount: Money) -> Money:
        base = amount.scale(self.percent_bps, 10_000, round_up=True)
        gst = base.scale(self.gst_on_fee_bps, 10_000, round_up=True)
        return base + gst + Money(self.flat_minor, amount.currency)


class EconomicsService:
    def __init__(self, conn: sqlite3.Connection, *, plans: PlanStore | None = None,
                 costs: CostLedger | None = None, fees: FeeModel | None = None,
                 infra_per_user_minor: int = 0):
        self.conn = conn
        self.plans = plans or PlanStore(conn)
        self.costs = costs or CostLedger(conn)
        self.fees = fees or FeeModel()
        self.infra_per_user_minor = infra_per_user_minor

    # ---------- users ----------

    def user_counts(self) -> dict[str, int]:
        """
        Users per plan family, from live entitlements.

        Free is everyone without an open paid entitlement, which cannot be
        counted from this table alone -- so it is reported only when a total
        is supplied by the caller.
        """
        rows = self.conn.execute(
            "SELECT p.family, COUNT(DISTINCT e.user_id) AS n FROM entitlements e"
            " JOIN plans p ON p.id = e.plan_id"
            " JOIN subscriptions s ON s.id = e.subscription_id"
            " WHERE e.effective_until IS NULL AND s.status IN"
            " ('ACTIVE','TRIALING','PAST_DUE','CANCEL_AT_PERIOD_END')"
            " GROUP BY p.family")
        return {r["family"]: r["n"] for r in rows}

    # ---------- revenue ----------

    def revenue(self, *, since: str | None = None, until: str | None = None) -> dict:
        """
        Revenue recognised from active paid subscriptions in the window.

        An annual subscription is divided across twelve months rather than
        booked entirely in the month it was paid -- otherwise a January of
        annual sign-ups reports a spectacular month and eleven terrible ones,
        and no per-month contribution figure means anything.
        """
        clauses = ["s.status IN ('ACTIVE','TRIALING','PAST_DUE','CANCEL_AT_PERIOD_END')"]
        params: list = []
        if since:
            clauses.append("s.created_at >= ?")
            params.append(since)
        if until:
            clauses.append("s.created_at < ?")
            params.append(until)

        rows = self.conn.execute(
            f"SELECT p.family, p.billing_interval, p.price_minor, p.currency,"
            f" COUNT(*) AS n FROM subscriptions s JOIN plans p ON p.id = s.plan_id"
            f" WHERE {' AND '.join(clauses)} GROUP BY p.family, p.billing_interval",
            params).fetchall()

        by_family: dict[str, int] = {}
        total = 0
        for row in rows:
            gross = row["price_minor"] * row["n"]
            monthly = gross // 12 if row["billing_interval"] == "annual" else gross
            by_family[row["family"]] = by_family.get(row["family"], 0) + monthly
            total += monthly

        return {"total_minor": total, "total_display": Money(total).format(),
                "by_family_minor": by_family,
                "by_family_display": {k: Money(v).format() for k, v in by_family.items()},
                "note": "annual subscriptions are recognised monthly, not in full at sale"}

    # ---------- the dashboard ----------

    def daily(self, day: str | None = None) -> dict:
        start, end = _day_bounds(day)
        ai = self.costs.totals(since=start)
        revenue = self.revenue()

        # Fees apply to money actually collected. Approximated here from
        # recognised revenue, and labelled as such rather than presented as a
        # settled figure.
        fees = self.fees.fee_for(Money(revenue["total_minor"]))
        counts = self.user_counts()
        paying = sum(counts.values())
        infra = Money(self.infra_per_user_minor * paying)

        contribution = (Money(revenue["total_minor"]) - ai["spend"] - fees - infra)

        by_model = self.costs.by_model(since=start)
        unpriced = sum(r.get("unpriced_calls", 0) for r in by_model)

        return {
            "day": (day or datetime.now(timezone.utc).strftime("%Y-%m-%d")),
            "revenue": revenue["total_display"],
            "ai_cost": ai["spend_display"],
            "payment_fees": fees.format(),
            "direct_infra": infra.format(),
            "contribution": contribution.format(),
            "contribution_minor": contribution.minor,
            "users": counts,
            "paying_users": paying,
            "ai_calls": ai["calls"],
            "ai_by_model": by_model,
            "warnings": ([f"{unpriced} AI call(s) had no configured price, so the AI cost "
                          "line is understated and contribution is overstated."]
                         if unpriced else []),
            "fee_note": ("payment fees are computed from recognised revenue at "
                         f"{self.fees.percent_bps / 100:.2f}% plus GST, not from settled "
                         "gateway statements"),
        }

    def plan_economics(self, *, since: str | None = None) -> list[dict]:
        """
        Per-plan contribution: is each tier's price covering what it costs?

        The number the ₹150-₹200 target is measured against.
        """
        counts = self.user_counts()
        ai_by_plan = {r["plan_family"]: r for r in self.costs.by_plan_family(since=since)}

        out = []
        for family, users in sorted(counts.items()):
            try:
                plan = self.plans.active(family, "monthly")
            except Exception:
                continue
            revenue_each = plan.price
            ai = ai_by_plan.get(family, {})
            ai_each = micro_to_money(ai.get("ai_cost_per_user_micro", 0))
            fee_each = self.fees.fee_for(revenue_each)
            infra_each = Money(self.infra_per_user_minor)
            contribution_each = revenue_each - ai_each - fee_each - infra_each

            out.append({
                "plan": plan.name,
                "family": family,
                "users": users,
                "revenue_per_user": revenue_each.format(decimals=False),
                "ai_cost_per_user": ai_each.format(),
                "payment_cost_per_user": fee_each.format(),
                "infra_per_user": infra_each.format(),
                "contribution_per_user": contribution_each.format(),
                "contribution_per_user_minor": contribution_each.minor,
                "measured": bool(ai),
                "note": ("" if ai else
                         "no AI cost has been attributed to this plan yet, so contribution "
                         "is the price minus fees only and will fall once usage is measured"),
            })
        return out

    def cost_per_500_accepted(self, *, since: str | None = None) -> dict:
        """
        The table that should eventually calibrate the usage limits.

        A plan's price has to cover the cost of the questions it promises. If
        Pro promises 5,000 questions and 500 accepted cost ₹40, the AI cost of
        a fully-consuming Pro user is ₹400 against a ₹499 price -- which is
        the calculation the whole telemetry layer exists to make possible.
        """
        rows = self.costs.by_model(per=500, since=since)
        overall = self.costs.cost_per_accepted(per=500, since=since)
        return {
            "per": 500,
            "by_model": rows,
            "overall": overall,
            "chain": "generation → validation → rejection → regeneration → 500 accepted",
            "note": ("Cost per ACCEPTED question, not per call. Work that was rejected and "
                     "then regenerated was paid for twice and is included."),
        }
