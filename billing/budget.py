"""
How much AI compute can Quintek afford?

Two financial systems meet here, and they are not the same system:

    customers  --Razorpay-->  Quintek      (revenue, in rupees, per month)
    Quintek    --credits-->   providers    (cost, in tokens, continuously)

Nothing links them automatically. A user's ₹499 does not pay for that user's
inference; it lands in a bank account, and separately Quintek tops up NVIDIA,
OpenRouter or Cerebras. So the question that decides whether the business
survives is not "what should a plan cost" but:

    GIVEN ₹X OF REVENUE, WHAT IS THE MAXIMUM AI COMPUTE WE CAN SAFELY CONSUME?

This module answers it as a waterfall rather than a single number, because the
deductions are the argument. Four of them are routinely forgotten, and each one
is individually capable of turning an apparently profitable plan into a loss:

  1. GST is not revenue. On a GST-inclusive ₹499, ₹76 belongs to the
     government from the moment it is collected.

  2. Annual plans collect twelve months of cash and owe twelve months of
     compute. Spending that cash in month one is the classic way to die with a
     healthy-looking bank balance, so the input here is RECOGNISED revenue.

  3. Allowances already sold and not yet consumed are a liability denominated
     in compute. A user who paid for 3,000 questions and has used 100 is owed
     2,900 questions' worth of inference, and that must be reserved before any
     of this month's revenue is called spendable.

  4. Free-tier questions are consumed out of the same provider credit as paid
     ones. The free plan has no revenue line of its own; it is funded out of
     this budget or it is not funded at all.

What comes out is a ceiling in rupees, a daily burn rate, and -- the number the
router actually needs -- a maximum cost per ACCEPTED question. That last one is
what makes the AI router a financial control layer rather than a quality
optimiser: a candidate whose measured cost per accepted question exceeds the
ceiling is unaffordable, whatever its benchmark score.

Every figure is integer minor units, and every deduction rounds UP. Rounding a
deduction in Quintek's favour would be the same class of error as floating
point money: small, systematic, and always in the direction that hides a loss.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .economics import FeeModel
from .entitlements import EntitlementEngine, period_start_for
from .money import MICRO, Money, micro_to_money
from .plans import PlanStore

# Verdicts. Named rather than boolean because "we can afford this" and "we can
# afford this with nothing to spare" call for different decisions.
OK = "OK"              # measured cost comfortably under the ceiling
TIGHT = "TIGHT"        # under the ceiling, but inside the margin
OVER = "OVER"          # measured cost exceeds what revenue can fund
UNKNOWN = "UNKNOWN"    # nothing measured -- not the same as safe

# Below this fraction of headroom the answer is TIGHT rather than OK. A budget
# with 5% to spare is not a budget, it is a coincidence.
TIGHT_HEADROOM_BPS = 1_500     # 15%


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Deduction:
    """One line of the waterfall, with the basis it was computed on."""

    name: str
    amount: Money
    basis: str
    note: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "amount_minor": self.amount.minor,
                "amount_display": self.amount.format(), "basis": self.basis,
                "note": self.note}


@dataclass(frozen=True)
class BudgetPolicy:
    """
    Every assumption in the calculation, in one place, as configuration.

    Defaults are deliberately conservative: an optimistic default here does not
    produce an optimistic forecast, it produces an overdrawn provider account.
    """

    # Indian SaaS is GST-rated. If displayed prices include it -- and consumer
    # prices in India normally do -- the tax is extracted from the gross rather
    # than added to it.
    gst_bps: int = 1_800
    price_includes_gst: bool = True

    fees: FeeModel = field(default_factory=FeeModel)

    # Money set aside for refunds and chargebacks. Quintek issues no automatic
    # refunds, but "no automatic refunds" is a policy, not a guarantee that no
    # refund is ever issued.
    refund_reserve_bps: int = 100          # 1%

    # Everything that is not inference: hosting, database, storage, egress.
    fixed_costs_minor: int = 0

    # What must survive after compute, expressed both ways. The larger of the
    # two applies -- a percentage alone collapses to nothing at low volume, and
    # an absolute alone stops scaling.
    target_contribution_bps: int = 2_500   # 25% of ex-GST revenue
    target_contribution_minor: int = 0

    # Whether unconsumed free-tier allowance is reserved as a commitment. It is
    # real compute that will be consumed; treating it as free is the reason
    # free tiers bankrupt people.
    cover_free_tier: bool = True

    currency: str = "INR"

    def as_dict(self) -> dict:
        return {
            "gst_bps": self.gst_bps, "price_includes_gst": self.price_includes_gst,
            "gateway_percent_bps": self.fees.percent_bps,
            "gateway_gst_on_fee_bps": self.fees.gst_on_fee_bps,
            "refund_reserve_bps": self.refund_reserve_bps,
            "fixed_costs_minor": self.fixed_costs_minor,
            "target_contribution_bps": self.target_contribution_bps,
            "target_contribution_minor": self.target_contribution_minor,
            "cover_free_tier": self.cover_free_tier,
        }


@dataclass(frozen=True)
class ComputeBudget:
    """The answer, with its whole derivation attached."""

    gross: Money
    deductions: tuple[Deduction, ...]
    ceiling: Money
    shortfall: Money
    period_days: int
    liability_measured: bool
    warnings: tuple[str, ...] = ()

    @property
    def solvent(self) -> bool:
        return self.shortfall.minor == 0

    @property
    def per_day(self) -> Money:
        return self.ceiling.scale(1, max(1, self.period_days))

    @property
    def ceiling_micro(self) -> int:
        return self.ceiling.minor * MICRO

    def per_question_ceiling_micro(self, expected_accepted: int) -> int | None:
        """
        The most Quintek may spend per accepted question and stay within budget.

        `None` when no volume is expected: dividing by zero questions would
        report an infinite allowance, which is exactly the wrong answer to give
        a router that is about to choose a model.
        """
        if expected_accepted <= 0:
            return None
        return self.ceiling_micro // expected_accepted

    def verdict(self, measured_cost_per_accepted_micro: int | None,
                expected_accepted: int) -> dict:
        """
        Compare a measured cost per accepted question against the ceiling.

        An unmeasured cost returns UNKNOWN. It does not return OK: a model
        nobody has costed is not a cheap model, it is an uncosted one.
        """
        allowed = self.per_question_ceiling_micro(expected_accepted)
        if measured_cost_per_accepted_micro is None or allowed is None:
            return {
                "verdict": UNKNOWN,
                "allowed_per_question_micro": allowed,
                "measured_per_question_micro": measured_cost_per_accepted_micro,
                "headroom_bps": None,
                "reason": ("no cost has been measured for this configuration"
                           if measured_cost_per_accepted_micro is None
                           else "no accepted-question volume is expected"),
            }
        measured = measured_cost_per_accepted_micro
        if measured > allowed:
            verdict, reason = OVER, "measured cost exceeds what revenue can fund"
        else:
            headroom = allowed - measured
            if allowed and (headroom * 10_000) // allowed < TIGHT_HEADROOM_BPS:
                verdict, reason = TIGHT, "affordable, but inside the safety margin"
            else:
                verdict, reason = OK, "affordable"
        headroom_bps = ((allowed - measured) * 10_000 // allowed) if allowed else None
        return {"verdict": verdict, "allowed_per_question_micro": allowed,
                "measured_per_question_micro": measured,
                "headroom_bps": headroom_bps, "reason": reason}

    def as_dict(self) -> dict:
        return {
            "gross_minor": self.gross.minor,
            "gross_display": self.gross.format(),
            "deductions": [d.as_dict() for d in self.deductions],
            "ceiling_minor": self.ceiling.minor,
            "ceiling_display": self.ceiling.format(),
            "per_day_display": self.per_day.format(),
            "shortfall_minor": self.shortfall.minor,
            "shortfall_display": self.shortfall.format(),
            "solvent": self.solvent,
            "period_days": self.period_days,
            "liability_measured": self.liability_measured,
            "warnings": list(self.warnings),
            "note": ("this is the ceiling on PROVIDER spend, not on revenue;"
                     " customers pay Quintek and Quintek separately funds"
                     " provider accounts"),
        }


def compute_budget(recognised_revenue: Money, policy: BudgetPolicy | None = None, *,
                   paid_liability: Money | None = None,
                   free_commitment: Money | None = None,
                   period_days: int = 30) -> ComputeBudget:
    """
    Run the waterfall.

    `recognised_revenue` is deliberately not "cash collected". Passing a year
    of annual subscriptions here would authorise a year of compute in a month.

    `paid_liability` is the compute value of allowances sold and not yet
    consumed; `None` means it has never been measured, which is recorded as a
    warning and NOT silently treated as zero.
    """
    policy = policy or BudgetPolicy()
    currency = recognised_revenue.currency
    zero = Money.zero(currency)
    gross = recognised_revenue
    warnings: list[str] = []

    deductions: list[Deduction] = []
    remaining = gross

    # 1. GST -- collected on the government's behalf, never Quintek's money.
    if policy.price_includes_gst and policy.gst_bps:
        gst = gross.scale(policy.gst_bps, 10_000 + policy.gst_bps, round_up=True)
        deductions.append(Deduction(
            "gst_collected", gst, "gross",
            "extracted from a GST-inclusive price; payable to the government"))
        remaining = remaining - gst
    ex_gst = remaining

    # 2. Gateway fees -- charged on the full amount the customer was charged,
    #    GST included, so the basis is gross rather than ex-GST.
    fee = policy.fees.fee_for(gross)
    deductions.append(Deduction("gateway_fees", fee, "gross",
                                "percentage plus GST on the fee itself"))
    remaining = remaining - fee

    # 3. Refunds and chargebacks.
    if policy.refund_reserve_bps:
        reserve = ex_gst.scale(policy.refund_reserve_bps, 10_000, round_up=True)
        deductions.append(Deduction("refund_reserve", reserve, "ex-GST revenue",
                                    "no refunds are automatic; some still happen"))
        remaining = remaining - reserve

    # 4. Everything that is not inference.
    if policy.fixed_costs_minor:
        fixed = Money(policy.fixed_costs_minor, currency)
        deductions.append(Deduction("fixed_costs", fixed, "absolute",
                                    "hosting, database, storage, egress"))
        remaining = remaining - fixed

    # 5. What has to survive.
    pct = ex_gst.scale(policy.target_contribution_bps, 10_000, round_up=True)
    absolute = Money(policy.target_contribution_minor, currency)
    target = pct if pct.minor >= absolute.minor else absolute
    if target.minor:
        deductions.append(Deduction(
            "target_contribution", target,
            "ex-GST revenue" if target is pct else "absolute",
            "the larger of the percentage and the absolute floor"))
        remaining = remaining - target

    # 6. Compute already owed to people who have already paid.
    liability_measured = paid_liability is not None
    if paid_liability is not None:
        if paid_liability.minor:
            deductions.append(Deduction(
                "outstanding_paid_allowance", paid_liability, "measured",
                "questions sold and not yet consumed, at measured unit cost"))
            remaining = remaining - paid_liability
    else:
        warnings.append(
            "outstanding paid allowance is UNMEASURED and has not been deducted;"
            " the ceiling below is an upper bound, not a safe number")

    if policy.cover_free_tier:
        if free_commitment is not None:
            if free_commitment.minor:
                deductions.append(Deduction(
                    "free_tier_commitment", free_commitment, "measured",
                    "free-tier questions consume the same provider credit"))
                remaining = remaining - free_commitment
        else:
            warnings.append(
                "free-tier commitment is UNMEASURED and has not been deducted")

    if remaining.minor < 0:
        shortfall = Money(-remaining.minor, currency)
        ceiling = zero
        warnings.append(
            "deductions exceed recognised revenue: there is no compute budget"
            " at this revenue, and the shortfall is being funded from capital")
    else:
        shortfall = zero
        ceiling = remaining

    return ComputeBudget(gross=gross, deductions=tuple(deductions), ceiling=ceiling,
                         shortfall=shortfall, period_days=period_days,
                         liability_measured=liability_measured,
                         warnings=tuple(warnings))


def runway_days(credit_balance: Money, daily_burn: Money) -> int | None:
    """
    How many days of provider credit remain at the current burn.

    `None` when nothing is being burned -- infinite runway is not a number, and
    reporting a huge integer would make an idle system look healthy.
    """
    if daily_burn.minor <= 0:
        return None
    if credit_balance.minor <= 0:
        return 0
    return credit_balance.minor // daily_burn.minor


@dataclass(frozen=True)
class PlanBudget:
    """The per-subscription view: what one seat of a plan can afford."""

    plan_id: str
    family: str
    billing_interval: str
    monthly_revenue: Money
    ceiling: Money
    allowance: int
    per_question_micro: int | None
    deductions: tuple[Deduction, ...]

    def as_dict(self) -> dict:
        return {
            "plan_id": self.plan_id, "family": self.family,
            "billing_interval": self.billing_interval,
            "monthly_revenue_display": self.monthly_revenue.format(),
            "compute_ceiling_display": self.ceiling.format(),
            "monthly_allowance": self.allowance,
            "max_cost_per_accepted_micro": self.per_question_micro,
            "max_cost_per_accepted_display": (
                micro_to_money(self.per_question_micro).format()
                if self.per_question_micro is not None else "—"),
            "deductions": [d.as_dict() for d in self.deductions],
        }


def plan_budget(plan, policy: BudgetPolicy | None = None, *,
                fixed_cost_per_user_minor: int = 0) -> PlanBudget:
    """
    The same waterfall for a single subscription of one plan.

    Annual plans are divided by twelve first, so an annual seat and a monthly
    seat of the same family are compared on the same basis. Portfolio fixed
    costs do not apply per seat, so they are replaced by an explicit per-user
    figure that defaults to zero.
    """
    policy = policy or BudgetPolicy()
    interval = getattr(plan, "billing_interval", "monthly")
    price = Money(getattr(plan, "price_minor", 0), policy.currency)
    monthly = price.scale(1, 12) if interval == "annual" else price

    seat_policy = BudgetPolicy(
        gst_bps=policy.gst_bps, price_includes_gst=policy.price_includes_gst,
        fees=policy.fees, refund_reserve_bps=policy.refund_reserve_bps,
        fixed_costs_minor=fixed_cost_per_user_minor,
        target_contribution_bps=policy.target_contribution_bps,
        target_contribution_minor=policy.target_contribution_minor,
        cover_free_tier=False, currency=policy.currency)

    # A seat's own unconsumed allowance is not an ADDITIONAL liability on top
    # of its own budget -- it IS what the budget is for. Deducting it here
    # would double-count.
    budget = compute_budget(monthly, seat_policy, paid_liability=Money.zero(policy.currency),
                            free_commitment=Money.zero(policy.currency))

    allowance = int(getattr(plan, "monthly_question_allowance", 0) or 0)
    return PlanBudget(
        plan_id=getattr(plan, "id", ""), family=getattr(plan, "family", ""),
        billing_interval=interval, monthly_revenue=monthly, ceiling=budget.ceiling,
        allowance=allowance,
        per_question_micro=budget.per_question_ceiling_micro(allowance),
        deductions=budget.deductions)


class BudgetService:
    """
    The live answer, from the database.

    Revenue comes from recognised subscriptions, the unit cost from the cost
    ledger, and the liability from entitlements minus what has been consumed.
    Nothing here is a forecast: every input is something the system has already
    recorded, and where it has recorded nothing the result says so.
    """

    def __init__(self, conn: sqlite3.Connection, *, policy: BudgetPolicy | None = None,
                 plans: PlanStore | None = None):
        self.conn = conn
        self.policy = policy or BudgetPolicy()
        self.plans = plans or PlanStore(conn)
        self.entitlements = EntitlementEngine(conn, self.plans)

    # ---------- inputs ----------

    def unit_cost_micro(self, *, since: str | None = None) -> int | None:
        """
        Measured cost of ONE accepted question, in micro minor units.

        Divided out of the cost ledger's own aggregate rather than recomputed,
        so this cannot drift from what the economics dashboard reports.
        """
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_micro),0) AS spend,"
            " COALESCE(SUM(questions_accepted),0) AS accepted FROM cost_ledger"
            + (" WHERE created_at >= ?" if since else ""),
            ((since,) if since else ())).fetchone()
        if not row or not row["accepted"]:
            return None
        return row["spend"] // row["accepted"]

    def recognised_revenue(self) -> Money:
        rows = self.conn.execute(
            "SELECT p.billing_interval, p.price_minor, COUNT(*) AS n"
            " FROM subscriptions s JOIN plans p ON p.id = s.plan_id"
            " WHERE s.status IN ('ACTIVE','TRIALING','PAST_DUE','CANCEL_AT_PERIOD_END')"
            " GROUP BY p.billing_interval, p.price_minor").fetchall()
        total = 0
        for row in rows:
            gross = row["price_minor"] * row["n"]
            total += gross // 12 if row["billing_interval"] == "annual" else gross
        return Money(total, self.policy.currency)

    def unconsumed_units(self, *, at: datetime | None = None) -> dict:
        """
        Questions paid for (or granted) and not yet consumed, split paid/free.

        Resolved through `EntitlementEngine.for_user` rather than by summing
        the entitlements table directly, so rollover, promotions and overlapping
        grants are counted exactly as the entitlement check counts them. That
        matters more than the query being one statement: a liability computed
        by different rules than the check it funds is not a liability figure.
        """
        period = period_start_for(at)
        users = [r["user_id"] for r in self.conn.execute(
            "SELECT DISTINCT user_id FROM entitlements"
            " WHERE effective_until IS NULL OR effective_until > ?",
            (_now_iso(),))]

        paid_units = free_units = 0
        for user_id in users:
            ent = self.entitlements.for_user(user_id, at=at)
            allowance = ent.effective_monthly
            used = self.conn.execute(
                "SELECT COALESCE(SUM(question_units),0) AS n FROM usage_ledger"
                " WHERE user_id=? AND period_start=?", (user_id, period)
            ).fetchone()["n"]
            remaining = max(0, allowance - used)
            paid = self.conn.execute(
                "SELECT 1 FROM subscriptions s JOIN plans p ON p.id = s.plan_id"
                " WHERE s.user_id=? AND p.price_minor > 0"
                " AND s.status IN ('ACTIVE','TRIALING','PAST_DUE','CANCEL_AT_PERIOD_END')"
                " LIMIT 1", (user_id,)).fetchone() is not None
            if paid:
                paid_units += remaining
            else:
                free_units += remaining
        return {"paid_units": paid_units, "free_units": free_units,
                "users": len(users), "period_start": period}

    # ---------- the answer ----------

    def budget(self, *, period_days: int = 30, since: str | None = None,
               at: datetime | None = None) -> ComputeBudget:
        unit = self.unit_cost_micro(since=since)
        units = self.unconsumed_units(at=at)
        if unit is None:
            paid = free = None
        else:
            paid = micro_to_money(units["paid_units"] * unit, self.policy.currency)
            free = micro_to_money(units["free_units"] * unit, self.policy.currency)
        return compute_budget(self.recognised_revenue(), self.policy,
                              paid_liability=paid, free_commitment=free,
                              period_days=period_days)

    def report(self, *, period_days: int = 30, expected_accepted: int | None = None,
               since: str | None = None) -> dict:
        """
        Everything an operator needs to answer the question in one call.

        `expected_accepted` defaults to the unconsumed balance -- the volume
        Quintek is already committed to producing -- rather than to a guess.
        """
        budget = self.budget(period_days=period_days, since=since)
        units = self.unconsumed_units()
        expected = (expected_accepted if expected_accepted is not None
                    else units["paid_units"] + units["free_units"])
        unit = self.unit_cost_micro(since=since)
        return {
            "budget": budget.as_dict(),
            "policy": self.policy.as_dict(),
            "unconsumed": units,
            "expected_accepted": expected,
            "measured_cost_per_accepted_micro": unit,
            "measured_cost_per_accepted_display": (
                micro_to_money(unit).format() if unit is not None else "—"),
            "verdict": budget.verdict(unit, expected),
            "plans": [plan_budget(p, self.policy).as_dict()
                      for p in self.plans.all_active()
                      if getattr(p, "price_minor", 0) > 0],
        }
