r"""
Reserving capacity before spending it, and recording what was spent.

THE CONCURRENCY PROBLEM THIS SOLVES
-----------------------------------
Daily remaining is 300. Two requests arrive at once, each for 200. Both read
300, both pass, and 400 questions are authorised against a 300 limit.

Checking then acting is not enough, because another request can interleave
between the check and the act. So capacity is RESERVED inside a single
immediate transaction: the read of current usage and the write of the hold
happen with the database lock already held, and the second request's read
therefore sees the first one's hold.

    AVAILABLE -> RESERVE (atomic) -> PROCESS -> COMMIT actual usage
                                             \-> RELEASE the unused part

`BEGIN IMMEDIATE` is the load-bearing detail. SQLite's default deferred
transaction takes a read lock first and upgrades on write, which can fail with
SQLITE_BUSY after the read -- exactly the window this is trying to close.
IMMEDIATE takes the write lock up front, so the check and the hold are
genuinely one step.

WHY RESERVE RATHER THAN DEDUCT AFTER
------------------------------------
A 500-question batch runs for minutes. Deducting on completion means every
concurrent request during those minutes sees capacity that is already spoken
for. Reserving first is what makes the check meaningful, and it is why
`billing/entitlements.py` counts held reservations as used.

EXPIRY
------
A crashed worker must not strand a user's allowance forever. Holds carry an
expiry and are ignored once past it; `sweep_expired` tidies the rows.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .entitlements import EntitlementEngine, period_start_for, today_iso
from .plans import now_iso

DEFAULT_HOLD_MINUTES = 30


class ReservationError(RuntimeError):
    pass


class InsufficientAllowance(ReservationError):
    """The request cannot be met. Carries what WAS available, for the UI."""

    def __init__(self, message: str, available: int, requested: int):
        super().__init__(message)
        self.available = available
        self.requested = requested


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


@dataclass
class Reservation:
    id: str
    user_id: str
    question_units: int
    compute_units: int
    batch_id: str
    usage_date: str
    period_start: str
    expires_at: str
    status: str = "HELD"

    def as_dict(self) -> dict:
        return {"reservation_id": self.id, "user_id": self.user_id,
                "question_units": self.question_units,
                "compute_units": self.compute_units, "batch_id": self.batch_id,
                "usage_date": self.usage_date, "period_start": self.period_start,
                "expires_at": self.expires_at, "status": self.status}


class UsageService:
    def __init__(self, conn: sqlite3.Connection, engine: EntitlementEngine | None = None):
        self.conn = conn
        self.engine = engine or EntitlementEngine(conn)

    # ---------- compute units ----------

    def compute_units_for(self, question_type: str, count: int) -> int:
        """
        Question units are what the user sees; compute units are what Quintek
        spends. An image question costs four times an MCQ to produce and is
        still "one question" on the dashboard.
        """
        weights = self.engine.plans.compute_weights()
        return count * weights.get(question_type, 1)

    # ---------- the atomic reservation ----------

    def reserve(self, user_id: str, requested: int, *, question_type: str = "mcq",
                batch_id: str = "", allow_partial: bool = True,
                hold_minutes: int = DEFAULT_HOLD_MINUTES,
                at: datetime | None = None) -> Reservation:
        """
        Hold capacity, atomically. Raises `InsufficientAllowance` if none.

        Everything between BEGIN IMMEDIATE and COMMIT is one step from any
        other connection's point of view, which is what makes the second of
        two simultaneous requests see the first one's hold.
        """
        if requested < 1:
            raise ReservationError("a reservation must be for at least one question")

        moment = at or datetime.now(timezone.utc)
        day, period = today_iso(moment), period_start_for(moment)
        expires = (moment + timedelta(minutes=hold_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            decision = self.engine.authorize(user_id, requested, at=moment,
                                             allow_partial=allow_partial)
            if not decision.allowed or decision.granted < 1:
                raise InsufficientAllowance(decision.reason,
                                            decision.availability.available_now, requested)

            granted = decision.granted
            reservation = Reservation(
                id=new_id("res"), user_id=user_id, question_units=granted,
                compute_units=self.compute_units_for(question_type, granted),
                batch_id=batch_id or new_id("batch"), usage_date=day,
                period_start=period, expires_at=expires)

            self.conn.execute(
                "INSERT INTO reservations (id, user_id, batch_id, question_units,"
                " compute_units, status, usage_date, period_start, created_at, expires_at)"
                " VALUES (?,?,?,?,?, 'HELD', ?,?,?,?)",
                (reservation.id, user_id, reservation.batch_id, granted,
                 reservation.compute_units, day, period, now_iso(), expires))
            self.conn.commit()
            return reservation
        except Exception:
            self.conn.rollback()
            raise

    # ---------- settling ----------

    def commit(self, reservation_id: str, *, actual_units: int | None = None,
               question_type: str = "mcq", operation_id: str = "") -> dict:
        """
        Turn a hold into real usage.

        `actual_units` may be LOWER than reserved -- a batch that produced 480
        of 500 must not bill for 500. It may never be higher: a batch that
        somehow produced more than it reserved is a bug, and silently
        accepting the overage would hide it and overspend the user's
        allowance.
        """
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute("SELECT * FROM reservations WHERE id=?",
                                    (reservation_id,)).fetchone()
            if row is None:
                raise ReservationError(f"no reservation {reservation_id!r}")
            if row["status"] != "HELD":
                raise ReservationError(
                    f"reservation {reservation_id} is {row['status']}, not HELD; committing "
                    "it again would double-count the usage")

            used = row["question_units"] if actual_units is None else actual_units
            if used > row["question_units"]:
                raise ReservationError(
                    f"cannot commit {used} units against a reservation for "
                    f"{row['question_units']}: a batch that produced more than it reserved "
                    "is a bug, and accepting it would overspend the allowance")
            used = max(0, used)

            if used:
                self.conn.execute(
                    "INSERT INTO usage_ledger (id, user_id, batch_id, operation_id,"
                    " reservation_id, question_units, compute_units, question_type,"
                    " usage_date, period_start, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (new_id("use"), row["user_id"], row["batch_id"], operation_id,
                     reservation_id, used, self.compute_units_for(question_type, used),
                     question_type, row["usage_date"], row["period_start"], now_iso()))

            self.conn.execute(
                "UPDATE reservations SET status='COMMITTED', settled_at=? WHERE id=?",
                (now_iso(), reservation_id))
            self.conn.commit()
            return {"reservation_id": reservation_id, "committed_units": used,
                    "released_units": row["question_units"] - used}
        except Exception:
            self.conn.rollback()
            raise

    def release(self, reservation_id: str, *, reason: str = "") -> dict:
        """Return an unused hold. Nothing is written to the usage ledger."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute("SELECT * FROM reservations WHERE id=?",
                                    (reservation_id,)).fetchone()
            if row is None:
                raise ReservationError(f"no reservation {reservation_id!r}")
            if row["status"] != "HELD":
                self.conn.rollback()
                return {"reservation_id": reservation_id, "released": 0,
                        "note": f"already {row['status']}"}
            self.conn.execute(
                "UPDATE reservations SET status='RELEASED', settled_at=? WHERE id=?",
                (now_iso(), reservation_id))
            self.conn.commit()
            return {"reservation_id": reservation_id, "released": row["question_units"],
                    "reason": reason}
        except Exception:
            self.conn.rollback()
            raise

    def sweep_expired(self, *, at: datetime | None = None) -> int:
        """
        Expire stale holds so a crashed worker cannot strand an allowance.

        `availability()` already ignores expired holds, so this is tidying
        rather than correctness -- but a table of permanently-HELD rows makes
        every support question harder to answer.
        """
        moment = (at or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
        cursor = self.conn.execute(
            "UPDATE reservations SET status='EXPIRED', settled_at=? "
            "WHERE status='HELD' AND expires_at <= ?", (moment, moment))
        self.conn.commit()
        return cursor.rowcount

    # ---------- rollover ----------

    def apply_rollover(self, user_id: str, *, at: datetime | None = None) -> dict:
        """
        Carry unused allowance into the next period, capped by the plan.

        Run at the billing-period transition, server-side. The cap is
        `rollover_percent` of the plan's normal allowance -- Pro carries at
        most 2,500 of 5,000 -- so an inactive month cannot compound into an
        allowance the cost model never budgeted for.
        """
        entitlement = self.engine.for_user(user_id, at=at)
        previous = self.engine.availability(user_id, at=at)
        unused = previous.monthly_remaining
        cap = entitlement.plan.max_rollover
        carried = min(unused, cap)

        return {
            "user_id": user_id, "unused": unused, "cap": cap, "carried": carried,
            "forfeited": max(0, unused - cap),
            "new_allowance": entitlement.monthly_allowance + carried,
            "note": (f"{unused - cap} questions exceeded the {cap} rollover cap and were not "
                     "carried" if unused > cap else ""),
        }
