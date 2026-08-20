"""
What Quintek actually pays, per operation, and what it got for the money.

The metric this module exists to produce:

    COST PER 500 ACCEPTED QUESTIONS

not cost per call, and not cost per question generated. A model that is cheap
per call and has half its output rejected by validation is not cheap -- the
rejected work was paid for, and the regeneration was paid for again. The
denominator has to be ACCEPTED questions or the number flatters exactly the
models that should be avoided:

    generation -> validation -> rejection -> regeneration -> 500 accepted
                                                            ^^^^^^^^^^^^
                                                    the only useful output

WHY THIS IS SEPARATE FROM THE USAGE LEDGER
------------------------------------------
`usage_ledger` counts what the USER consumed, in questions. `cost_ledger`
records what QUINTEK spent, in money and tokens. They are different numbers
with different audiences: a learner is billed for one question whether it took
one generation or three, and Quintek pays for all three. Merging them would
make it impossible to see the gap, and the gap is the contribution margin.

PRECISION
---------
Costs are stored in MICRO minor units. A single call costs a fraction of a
paise; rounding each row to whole paise inflated a measured 10,000-call total
from ₹30 to ₹100. Rounding happens once, at aggregation. See `billing/money.py`.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .money import MICRO, Money, micro_to_money, token_cost_micro
from .plans import now_iso


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


@dataclass
class ModelPrice:
    """
    A model's price, in MICRO minor units per million tokens.

    Held per `provider:model` because the same model on two hosts is two
    prices, and attributing one host's cost to the other misstates both.
    """

    provider: str
    model: str
    input_per_million_micro: int
    output_per_million_micro: int
    currency: str = "INR"

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.model}"

    @classmethod
    def from_usd_per_million(cls, provider: str, model: str, usd_in: float,
                             usd_out: float, *, usd_to_inr_paise: int = 8_500) -> "ModelPrice":
        """
        Convert a provider's published USD price to INR micro-minor units.

        `usd_to_inr_paise` is the rate in paise per dollar and is a parameter,
        not a constant: an exchange rate baked into code is a cost model that
        silently drifts from reality. It should be refreshed from whatever the
        finance process uses.
        """
        def convert(usd: float) -> int:
            # usd -> paise -> micro-minor. Done in integers after one multiply
            # so the fraction of a paise survives.
            return int(round(usd * usd_to_inr_paise * MICRO))
        return cls(provider, model, convert(usd_in), convert(usd_out))


@dataclass
class OperationCost:
    """One AI call: what it cost and what it produced."""

    provider: str
    model: str
    operation: str = ""
    user_id: str = ""
    plan_family: str = ""
    batch_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost_micro: int = 0
    compute_units: int = 0
    questions_produced: int = 0
    questions_accepted: int = 0
    questions_rejected: int = 0
    regenerations: int = 0
    latency_ms: float | None = None
    currency: str = "INR"

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.model}"


class CostLedger:
    """Append-only record of real spend. Never estimates."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._prices: dict[str, ModelPrice] = {}

    # ---------- prices ----------

    def set_price(self, price: ModelPrice) -> None:
        self._prices[price.key] = price

    def price_for(self, provider: str, model: str) -> ModelPrice | None:
        return self._prices.get(f"{provider}:{model}")

    def cost_of(self, provider: str, model: str, *, input_tokens: int = 0,
                output_tokens: int = 0) -> tuple[int, bool]:
        """
        `(cost_micro, priced)`. `priced` is False when no price is configured.

        An unpriced model returns zero cost AND says so, rather than silently
        contributing nothing to the AI-cost line. A cost dashboard that
        reports an unpriced model as free is worse than one that reports it as
        unknown, because free looks like good news.
        """
        price = self.price_for(provider, model)
        if price is None:
            return 0, False
        return (token_cost_micro(input_tokens, price.input_per_million_micro)
                + token_cost_micro(output_tokens, price.output_per_million_micro)), True

    # ---------- recording ----------

    def record(self, cost: OperationCost) -> str:
        priced = True
        if not cost.cost_micro and (cost.input_tokens or cost.output_tokens):
            cost.cost_micro, priced = self.cost_of(
                cost.provider, cost.model, input_tokens=cost.input_tokens,
                output_tokens=cost.output_tokens)

        price = self.price_for(cost.provider, cost.model)
        row_id = new_id("cost")
        self.conn.execute(
            "INSERT INTO cost_ledger (id, user_id, plan_family, batch_id, operation, provider,"
            " model, input_tokens, output_tokens, cached_tokens, price_in_micro,"
            " price_out_micro, cost_micro, currency, compute_units, questions_produced,"
            " questions_accepted, questions_rejected, regenerations, latency_ms, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row_id, cost.user_id, cost.plan_family, cost.batch_id, cost.operation,
             cost.provider, cost.model, cost.input_tokens, cost.output_tokens,
             cost.cached_tokens,
             price.input_per_million_micro if price else None,
             price.output_per_million_micro if price else None,
             cost.cost_micro, cost.currency, cost.compute_units, cost.questions_produced,
             cost.questions_accepted, cost.questions_rejected, cost.regenerations,
             cost.latency_ms, now_iso()))
        self.conn.commit()
        return row_id

    # ---------- the metric ----------

    def cost_per_accepted(self, *, per: int = 500, provider: str | None = None,
                          model: str | None = None, since: str | None = None,
                          batch_id: str | None = None) -> dict:
        """
        Cost per `per` ACCEPTED questions.

        Everything spent -- generation, validation, rejected output,
        regeneration -- divided by what survived. That is the number the price
        of a plan has to cover, and it is frequently several times the naive
        cost-per-call.
        """
        clauses, params = [], []
        for column, value in (("provider", provider), ("model", model),
                              ("batch_id", batch_id)):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        if since:
            clauses.append("created_at >= ?")
            params.append(since)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        row = self.conn.execute(
            f"SELECT COALESCE(SUM(cost_micro),0) AS spend,"
            f" COALESCE(SUM(questions_accepted),0) AS accepted,"
            f" COALESCE(SUM(questions_produced),0) AS produced,"
            f" COALESCE(SUM(questions_rejected),0) AS rejected,"
            f" COALESCE(SUM(regenerations),0) AS regens,"
            f" COUNT(*) AS calls,"
            # Rows that actually reached a model. The denominator for the
            # unpriced caveat has to match its numerator, or "1 of 2" counts a
            # settlement row as a call nobody could have priced.
            f" SUM(CASE WHEN input_tokens > 0 OR output_tokens > 0"
            f"     THEN 1 ELSE 0 END) AS model_calls,"
            # Only calls that actually consumed tokens can be "unpriced". A
            # settlement row carries the yield of a batch and no tokens at
            # all; counting it here would make every batch look partly
            # uncosted and put a false caveat on a correct figure.
            f" SUM(CASE WHEN price_in_micro IS NULL"
            f"          AND (input_tokens > 0 OR output_tokens > 0)"
            f"     THEN 1 ELSE 0 END) AS unpriced"
            f" FROM cost_ledger {where}", params).fetchone()

        accepted = row["accepted"]
        produced = row["produced"]
        spend_micro = row["spend"]

        if not accepted:
            return {
                "per": per, "accepted": 0, "produced": produced,
                "total_spend": micro_to_money(spend_micro).format(),
                "cost_per_batch": None, "cost_per_batch_display": "—",
                "acceptance_rate": (0.0 if produced else None),
                "calls": row["calls"], "unpriced_calls": row["unpriced"] or 0,
                "note": ("Nothing has been accepted yet, so cost per accepted question is "
                         "undefined. Dividing by produced instead would flatter a model whose "
                         "output is being rejected."),
            }

        per_accepted_micro = spend_micro / accepted
        batch_micro = int(per_accepted_micro * per)

        unpriced = row["unpriced"] or 0
        return {
            "per": per,
            "accepted": accepted,
            "produced": produced,
            "rejected": row["rejected"],
            "regenerations": row["regens"],
            "calls": row["calls"],
            "acceptance_rate": accepted / produced if produced else None,
            "total_spend": micro_to_money(spend_micro).format(),
            "cost_per_batch": batch_micro,
            "cost_per_batch_display": micro_to_money(batch_micro).format(),
            "unpriced_calls": unpriced,
            # Said plainly rather than left to be inferred from a small number.
            "note": (f"{unpriced} of {row['model_calls']} calls had no configured price and "
                     "contributed nothing to this figure, so the real cost is higher."
                     if unpriced else ""),
        }

    def by_model(self, *, per: int = 500, since: str | None = None) -> list[dict]:
        """
        The comparison table: cost per 500 accepted, per `provider:model`.

        This is what should eventually calibrate the usage limits -- a plan's
        price has to cover the cost of the questions it promises, and only
        this number says what that is.
        """
        params, where = [], ""
        if since:
            where, params = "WHERE created_at >= ?", [since]
        rows = self.conn.execute(
            f"SELECT provider, model FROM cost_ledger {where}"
            f" GROUP BY provider, model ORDER BY provider, model", params)
        out = []
        for row in rows:
            stats = self.cost_per_accepted(per=per, provider=row["provider"],
                                           model=row["model"], since=since)
            out.append({"provider": row["provider"], "model": row["model"], **stats})
        # Cheapest per accepted question first; unmeasured last.
        out.sort(key=lambda r: (r["cost_per_batch"] is None, r["cost_per_batch"] or 0))
        return out

    def totals(self, *, since: str | None = None) -> dict:
        params, where = [], ""
        if since:
            where, params = "WHERE created_at >= ?", [since]
        row = self.conn.execute(
            f"SELECT COALESCE(SUM(cost_micro),0) AS spend, COUNT(*) AS calls,"
            f" COALESCE(SUM(questions_accepted),0) AS accepted,"
            f" COALESCE(SUM(input_tokens),0) AS tin,"
            f" COALESCE(SUM(output_tokens),0) AS tout FROM cost_ledger {where}",
            params).fetchone()
        return {"spend_micro": row["spend"],
                "spend": micro_to_money(row["spend"]),
                "spend_display": micro_to_money(row["spend"]).format(),
                "calls": row["calls"], "accepted": row["accepted"],
                "input_tokens": row["tin"], "output_tokens": row["tout"]}

    def by_plan_family(self, *, since: str | None = None) -> list[dict]:
        """AI cost attributed to each plan, for the contribution calculation."""
        params, where = [], ""
        if since:
            where, params = "WHERE created_at >= ?", [since]
        rows = self.conn.execute(
            f"SELECT plan_family, COALESCE(SUM(cost_micro),0) AS spend,"
            f" COUNT(DISTINCT user_id) AS users, COUNT(*) AS calls,"
            f" COALESCE(SUM(questions_accepted),0) AS accepted"
            f" FROM cost_ledger {where} GROUP BY plan_family", params)
        out = []
        for row in rows:
            users = row["users"] or 1
            out.append({
                "plan_family": row["plan_family"] or "(unattributed)",
                "users": row["users"], "calls": row["calls"],
                "accepted": row["accepted"],
                "ai_cost_micro": row["spend"],
                "ai_cost_display": micro_to_money(row["spend"]).format(),
                "ai_cost_per_user_micro": row["spend"] // users,
                "ai_cost_per_user_display": micro_to_money(row["spend"] // users).format(),
            })
        return out
