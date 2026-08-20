"""
Plans, loaded from configuration. No allowance is written in Python.

The business rule that shapes this file:

    The displayed limits are launch configuration, not permanent promises.

So `configs/plans.json` is the source, the database is the runtime store, and
nothing in the codebase says `if plan == "pro": allowance = 5000`. Changing an
allowance is a config edit and a reseed, not a release.

VERSIONING RATHER THAN EDITING
------------------------------
A plan row is never mutated. Changing Pro's allowance writes version 2 and
deactivates version 1; existing subscriptions keep pointing at the version
they were sold under until they renew. Without this, lowering an allowance
would retroactively shrink what a paying customer already bought, which is
both a support problem and arguably not legal.

MONTHLY AND ANNUAL ARE ONE FAMILY
---------------------------------
`student_monthly` and `student_annual` are two billing intervals of one plan
family with identical allowances, not two products. The pricing page toggle
switches interval; everything downstream keys on family.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .money import Money

DEFAULT_CONFIG = Path("configs/plans.json")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class PlanError(ValueError):
    pass


@dataclass(frozen=True)
class Plan:
    id: str
    family: str
    name: str
    billing_interval: str
    price_minor: int
    currency: str
    monthly_question_allowance: int
    daily_question_limit: int
    session_question_limit: int
    rollover_percent: int
    version: int
    active: bool
    sort_order: int

    @property
    def price(self) -> Money:
        return Money(self.price_minor, self.currency)

    @property
    def is_free(self) -> bool:
        return self.price_minor == 0

    @property
    def max_rollover(self) -> int:
        """
        The ceiling on carried-over questions.

        Derived from `rollover_percent` rather than stored, so "at most 50% of
        next month's normal allowance" stays one rule expressed once. Pro:
        50% of 5,000 = 2,500.
        """
        return (self.monthly_question_allowance * self.rollover_percent) // 100

    def monthly_equivalent(self) -> Money:
        """What an annual plan costs per month, for the toggle's comparison."""
        if self.billing_interval != "annual":
            return self.price
        return self.price.scale(1, 12)

    def as_dict(self) -> dict:
        return {
            "id": self.id, "family": self.family, "name": self.name,
            "billing_interval": self.billing_interval,
            "price_minor": self.price_minor, "price_display": self.price.format(decimals=False),
            "currency": self.currency,
            "monthly_question_allowance": self.monthly_question_allowance,
            "daily_question_limit": self.daily_question_limit,
            "session_question_limit": self.session_question_limit,
            "rollover_percent": self.rollover_percent,
            "max_rollover": self.max_rollover,
            "version": self.version, "active": self.active, "sort_order": self.sort_order,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Plan":
        return cls(
            id=row["id"], family=row["family"], name=row["name"],
            billing_interval=row["billing_interval"], price_minor=row["price_minor"],
            currency=row["currency"],
            monthly_question_allowance=row["monthly_question_allowance"],
            daily_question_limit=row["daily_question_limit"],
            session_question_limit=row["session_question_limit"],
            rollover_percent=row["rollover_percent"], version=row["version"],
            active=bool(row["active"]), sort_order=row["sort_order"])


def plan_id(family: str, interval: str, version: int) -> str:
    return f"{family}_{interval}_v{version}"


def validate(spec: dict) -> None:
    """
    Refuse a plan that cannot be honoured.

    Each check is a configuration mistake that would otherwise surface as a
    user being unable to do what their plan says they can.
    """
    for field in ("family", "name", "billing_interval", "monthly_question_allowance",
                  "daily_question_limit", "session_question_limit"):
        if spec.get(field) is None:
            raise PlanError(f"plan is missing {field!r}: {spec}")

    monthly = spec["monthly_question_allowance"]
    daily = spec["daily_question_limit"]
    session = spec["session_question_limit"]

    if daily > monthly:
        raise PlanError(
            f"{spec['name']} {spec['billing_interval']}: daily limit {daily} exceeds the "
            f"monthly allowance {monthly}, so the daily cap could never be reached")
    # session > daily is INTENTIONAL, not a misconfiguration.
    #
    # Every plan in the launch config has it: Free 50/20, Student 500/150,
    # Pro 500/300. The session limit is the ceiling on a single generation
    # request; the daily cap is an independent safety mechanism that can bind
    # first. A Pro user really may ask for 500 in one session, and really will
    # be held to 300 that day.
    #
    # That is a real UX hazard -- it is the exact situation the partial-capacity
    # screen exists for -- but it is a deliberate product decision, so it is
    # surfaced as a note rather than refused. The effective cap is always
    # min(session, daily_remaining, monthly_remaining), computed in
    # billing/entitlements.py, and never the advertised session number alone.
    notes = []
    if session > daily:
        notes.append(
            f"session limit {session} exceeds daily limit {daily}: a user may be offered "
            f"fewer than {session} even on an unused day")
    spec.setdefault("_notes", []).extend(notes)
    if not 0 <= spec.get("rollover_percent", 0) <= 100:
        raise PlanError(f"{spec['name']}: rollover_percent must be between 0 and 100")
    if spec.get("price_minor", 0) < 0:
        raise PlanError(f"{spec['name']}: price cannot be negative")
    if not isinstance(spec.get("price_minor", 0), int):
        raise PlanError(
            f"{spec['name']}: price_minor must be an integer number of minor units "
            "(₹499 is 49900), never a float")


class PlanStore:
    """Reads and seeds plans. The only place allowances come from."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def seed_from_config(self, path: str | Path = DEFAULT_CONFIG) -> list[Plan]:
        """
        Load `configs/plans.json` into the database.

        A family+interval whose values are unchanged is left alone. One whose
        values differ gets a NEW version and the old one is deactivated --
        never an UPDATE, so a subscription sold under v1 keeps v1's terms.
        """
        config = json.loads(Path(path).read_text(encoding="utf-8"))
        currency = config.get("currency", "INR")
        seeded = []
        for spec in config["plans"]:
            validate(spec)
            seeded.append(self._upsert_version(spec, currency))

        for weight in config.get("compute_unit_weights", []):
            self.conn.execute(
                "INSERT INTO compute_unit_weights (question_type, weight, note, updated_at)"
                " VALUES (?,?,?,?) ON CONFLICT(question_type) DO UPDATE SET"
                " weight=excluded.weight, note=excluded.note, updated_at=excluded.updated_at",
                (weight["question_type"], weight["weight"], weight.get("note", ""), now_iso()))
        self.conn.commit()
        return seeded

    def _upsert_version(self, spec: dict, currency: str) -> Plan:
        family, interval = spec["family"], spec["billing_interval"]
        current = self.conn.execute(
            "SELECT * FROM plans WHERE family=? AND billing_interval=? AND active=1"
            " ORDER BY version DESC LIMIT 1", (family, interval)).fetchone()

        fields = ("price_minor", "monthly_question_allowance", "daily_question_limit",
                  "session_question_limit", "rollover_percent")
        if current is not None:
            unchanged = all(current[f] == spec.get(f, 0) for f in fields)
            if unchanged:
                return Plan.from_row(current)
            # Terms changed: new version, old one retired rather than rewritten.
            self.conn.execute("UPDATE plans SET active=0, updated_at=? WHERE id=?",
                              (now_iso(), current["id"]))
            version = current["version"] + 1
        else:
            version = 1

        new_id = plan_id(family, interval, version)
        stamp = now_iso()
        self.conn.execute(
            "INSERT INTO plans (id, family, name, billing_interval, price_minor, currency,"
            " monthly_question_allowance, daily_question_limit, session_question_limit,"
            " rollover_percent, version, active, sort_order, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)",
            (new_id, family, spec["name"], interval, spec.get("price_minor", 0), currency,
             spec["monthly_question_allowance"], spec["daily_question_limit"],
             spec["session_question_limit"], spec.get("rollover_percent", 0), version,
             spec.get("sort_order", 0), stamp, stamp))
        return self.get(new_id)

    # ---------- reading ----------

    def get(self, plan_identifier: str) -> Plan:
        row = self.conn.execute("SELECT * FROM plans WHERE id=?",
                                (plan_identifier,)).fetchone()
        if row is None:
            raise PlanError(f"no plan {plan_identifier!r}")
        return Plan.from_row(row)

    def active(self, family: str, interval: str) -> Plan:
        row = self.conn.execute(
            "SELECT * FROM plans WHERE family=? AND billing_interval=? AND active=1"
            " ORDER BY version DESC LIMIT 1", (family, interval)).fetchone()
        if row is None:
            raise PlanError(f"no active {family}/{interval} plan")
        return Plan.from_row(row)

    def free_plan(self) -> Plan:
        return self.active("free", "none")

    def all_active(self) -> list[Plan]:
        rows = self.conn.execute(
            "SELECT * FROM plans WHERE active=1 ORDER BY sort_order, billing_interval")
        return [Plan.from_row(r) for r in rows]

    def compute_weights(self) -> dict[str, int]:
        return {r["question_type"]: r["weight"]
                for r in self.conn.execute("SELECT * FROM compute_unit_weights")}

    # ---------- the pricing page ----------

    def pricing_page(self) -> dict:
        """
        Everything the pricing page renders, with nothing it must not show.

        No provider, model, token or compute figure appears in this payload.
        A learner buys Quintek; the AI economics underneath are an admin
        concern and leaking them here would both confuse the offer and hand
        competitors the cost base.
        """
        by_family: dict[str, dict] = {}
        for plan in self.all_active():
            entry = by_family.setdefault(plan.family, {
                "family": plan.family, "name": plan.name,
                "sort_order": plan.sort_order, "intervals": {},
                "monthly_question_allowance": plan.monthly_question_allowance,
                "daily_question_limit": plan.daily_question_limit,
                "session_question_limit": plan.session_question_limit,
            })
            entry["intervals"][plan.billing_interval] = {
                "plan_id": plan.id,
                "price_minor": plan.price_minor,
                "price_display": plan.price.format(decimals=False),
                "monthly_equivalent_display": plan.monthly_equivalent().format(decimals=False),
            }

        families = sorted(by_family.values(), key=lambda f: f["sort_order"])
        for family in families:
            monthly = family["intervals"].get("monthly")
            annual = family["intervals"].get("annual")
            if monthly and annual and monthly["price_minor"]:
                twelve = monthly["price_minor"] * 12
                saved = twelve - annual["price_minor"]
                months_free = saved // monthly["price_minor"]
                family["annual_saving"] = {
                    "amount_display": Money(saved).format(decimals=False),
                    "months_free": months_free,
                    "label": f"Save ~{months_free} months",
                }
        return {"currency": "INR", "families": families}
