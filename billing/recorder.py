"""
The join between the two financial systems.

    customers  --Razorpay-->  Quintek      (billing/, priced in rupees)
    Quintek    --credits-->   providers    (benchmark/, priced in tokens)

Nothing connects them automatically, and until this file existed nothing
connected them at all: the engine made model calls and `cost_ledger` stayed
empty, so `cost_per_accepted` reported "unmeasured" forever and every
economics figure in the admin console was an em dash. The compute budget was
being compared against a cost nobody was recording.

Two rules hold here:

  Telemetry must never break the call it is measuring. Every path swallows its
  own failure. A learner losing their generated questions because a cost row
  would not insert is a worse outcome than an incomplete ledger.

  An unpriced model is recorded as unpriced, not as free. `cost_per_accepted`
  counts those calls separately and says so beside the figure, because a
  dashboard reporting an unpriced model as ₹0.00 is worse than one reporting
  it as unknown -- free looks like good news.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .costs import CostLedger, ModelPrice, OperationCost

DEFAULT_PRICES = Path("configs/model_prices.json")


def load_prices(ledger: CostLedger, path: str | Path = DEFAULT_PRICES,
                *, usd_to_inr_paise: int | None = None) -> int:
    """
    Seed prices from configuration. Returns how many were loaded.

    Prices are configuration and not code for the same reason allowances are:
    a provider changes them without asking, and a price baked into a module is
    a cost model that silently drifts from what is actually being charged.
    """
    path = Path(path)
    if not path.exists():
        return 0
    payload = json.loads(path.read_text(encoding="utf-8"))
    rate = usd_to_inr_paise or int(payload.get("usd_to_inr_paise", 8_500))
    loaded = 0
    for entry in payload.get("prices", []):
        ledger.set_price(ModelPrice.from_usd_per_million(
            entry["provider"], entry["model"],
            float(entry["usd_in_per_million"]), float(entry["usd_out_per_million"]),
            usd_to_inr_paise=rate))
        loaded += 1
    return loaded


class CostRecorder:
    """
    Callable hook the AI engine invokes after every model call.

    Deliberately a callback rather than an import: `student/` does not depend
    on `billing/`, so the learning engine can be run, tested and reasoned about
    without a billing database in the picture.
    """

    def __init__(self, conn: sqlite3.Connection, *, ledger: CostLedger | None = None,
                 prices: str | Path | None = DEFAULT_PRICES):
        self.conn = conn
        self.ledger = ledger or CostLedger(conn)
        self.priced_models = load_prices(self.ledger, prices) if prices else 0

    # ---------- the hook ----------

    def __call__(self, call: dict) -> str | None:
        """
        Record one model call.

        `questions_produced` is NOT set here. At call time nobody knows how
        many questions survived parsing, let alone validation, and writing a
        guess would make every cost-per-accepted figure downstream a guess too.
        Attribution happens in `record_outcome`, against the same batch.
        """
        try:
            return self.ledger.record(OperationCost(
                provider=call.get("provider", ""),
                model=call.get("model", ""),
                operation=call.get("operation", ""),
                user_id=call.get("user_id", ""),
                plan_family=call.get("plan_family", ""),
                batch_id=call.get("batch_id", ""),
                input_tokens=int(call.get("input_tokens") or 0),
                output_tokens=int(call.get("output_tokens") or 0),
                cached_tokens=int(call.get("cached_tokens") or 0),
                latency_ms=call.get("latency_ms"),
            ))
        except Exception:
            # Never take down the call being measured.
            return None

    # ---------- attribution ----------

    def record_outcome(self, batch_id: str, *, produced: int = 0, accepted: int = 0,
                       rejected: int = 0, regenerations: int = 0,
                       user_id: str = "", plan_family: str = "") -> str | None:
        """
        Attach what a batch actually produced, as a SEPARATE zero-cost row.

        A new row rather than an update to the call rows: the ledger's value is
        that it is append-only, and `cost_per_accepted` sums over the batch, so
        a settlement row aggregates correctly without anything being rewritten.
        It also keeps the trail readable -- the spend and the yield are two
        observations made at two different times, and they look like it.
        """
        if not batch_id:
            return None
        try:
            return self.ledger.record(OperationCost(
                provider="", model="", operation="outcome", batch_id=batch_id,
                user_id=user_id, plan_family=plan_family,
                questions_produced=produced, questions_accepted=accepted,
                questions_rejected=rejected, regenerations=regenerations,
                cost_micro=0))
        except Exception:
            return None

    # ---------- reporting ----------

    def unpriced_since(self, since: str | None = None) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM cost_ledger WHERE price_in_micro IS NULL"
            " AND (input_tokens > 0 OR output_tokens > 0)"
            + (" AND created_at >= ?" if since else ""),
            ((since,) if since else ())).fetchone()
        return row["n"] if row else 0
