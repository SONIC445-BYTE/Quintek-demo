"""
Finding out that something is broken without being told by a user.

THREE THINGS, ONE MODULE, BECAUSE THEY ANSWER THE SAME QUESTION
----------------------------------------------------------------
An outage should be discoverable from monitoring. Today the student engine has
a health endpoint that honestly reports what it cannot do, and nothing else:
no record of failures, no ceiling on what an ingestion loop can spend, and no
way to get the data back if the file is lost. Each of those ends the same way
-- somebody emails to say it is broken, or the data is simply gone.

INCIDENTS ARE APPEND-ONLY AND CARRY THEIR CONTEXT
---------------------------------------------------
`record()` writes a row per failure with the operation, the exception type,
the message and a caller-supplied context. Append-only: an incident table that
can be tidied is a table whose quiet periods cannot be trusted.

The exception TYPE is stored separately from its message because grouping by
message is grouping by whatever string the failure happened to interpolate --
a row id, a path, a timestamp -- so a hundred instances of one fault look like
a hundred faults. `since()` groups on the type.

THE SPEND CEILING IS `validator.budget`, NOT A SECOND ONE
-----------------------------------------------------------
That module already counts outbound attempts, already distinguishes the
boundary it counts at, and already turns exhaustion into an ordinary exception
the caller can handle. A second counter would be a second thing to disagree
with the first, which is the defect that produced the `arm` field in the
journal key. `spend_guard()` wraps it and adds the only thing an ingestion loop
needs that a validator run does not: a per-user ceiling, because a runaway loop
belongs to one account.

**WIRED to question generation (2026-09-24).** `QuestionGenerator.generate`
charges one GENERATION unit immediately before its model call: 50 per account
per rolling 24 hours by default, `QUINTEK_GENERATION_CALLS_PER_DAY` to change
it. 50 is a starting value chosen for the testing phase, not a researched
limit. A refused call never reaches the model and is not written as spend;
the API answers 429.

Two things are NOT ceilinged, deliberately and visibly: concept extraction
during ingestion and the validator's calls. The ceiling as decided is on
generation calls. And the check and the write are two statements, so
requests racing in parallel can each pass the check before either writes:
the ceiling can be overshot by the number of requests in flight at once.
For one tester that is at most a handful; a hard guarantee would need a
row lock or a counter row, recorded rather than built.

`record()`, by contrast, needs no such number and IS wired: see
`IngestionEngine._record_incident`.

BACKUPS ARE VERIFIED BY RESTORING
-----------------------------------
`backup()` writes a consistent copy using SQLite's own backup API, which is
safe against concurrent writers in a way that copying the file is not --
especially in WAL mode, where the file on disk is not the database. `verify()`
opens the copy and reads it. A backup nobody has restored is a hypothesis, and
this project has been burned by evidence that was written but never read back.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter
from pathlib import Path

from validator.budget import Budget, BudgetExhausted

from .db import Database, new_id, now_iso

#: How many of one fault, inside the window, before an alert fires. One
#: failure is noise; the same fault repeating is a state.
DEFAULT_ALERT_THRESHOLD = 5

#: The window, in seconds.
DEFAULT_ALERT_WINDOW = 900.0

INGESTION = "ingestion"
GENERATION = "generation"
VALIDATION = "validation"
NOTIFICATION = "notification"
OPERATIONS = (INGESTION, GENERATION, VALIDATION, NOTIFICATION)


class SpendCeilingReached(RuntimeError):
    """This account has spent its allowance for the period."""


# ---------------------------------------------------------------------------
# Incidents
# ---------------------------------------------------------------------------

def record(db: Database, *, operation: str, error: BaseException | str,
           user_id: str = "", context: dict | None = None) -> str:
    """
    Write down one failure. Never raises: a monitor that can break the thing
    it monitors is worse than no monitor.
    """
    try:
        if isinstance(error, BaseException):
            error_type, message = type(error).__name__, str(error)
        else:
            error_type, message = "str", str(error)
        incident_id = new_id("inc")
        db.execute(
            "INSERT INTO incidents (id, operation, error_type, message, user_id,"
            " context_json, created_at) VALUES (?,?,?,?,?,?,?)",
            (incident_id, operation, error_type, message[:4000], user_id,
             json.dumps(context or {}, default=str)[:8000], now_iso()))
        return incident_id
    except Exception:
        # Deliberately swallowed, and the only place in this codebase where
        # that is right. The alternative is an ingestion run that dies because
        # the incident table was busy.
        return ""


def since(db: Database, *, hours: float = 24.0) -> dict:
    """
    What has been failing, grouped by FAULT rather than by message.

    Grouping on the message would split one fault into as many groups as it
    has interpolated ids.
    """
    cutoff = _iso_hours_ago(hours)
    rows = db.query(
        "SELECT operation, error_type, message, created_at FROM incidents"
        " WHERE created_at >= ? ORDER BY created_at", (cutoff,))
    by_fault = Counter((r["operation"], r["error_type"]) for r in rows)
    return {
        "window_hours": hours,
        "since": cutoff,
        "total": len(rows),
        "by_fault": [{"operation": op, "error_type": et, "count": n,
                      "latest": next(r["message"] for r in reversed(rows)
                                     if (r["operation"], r["error_type"]) == (op, et))}
                     for (op, et), n in by_fault.most_common()],
        "by_operation": dict(Counter(r["operation"] for r in rows)),
    }


def alerts(db: Database, *, threshold: int = DEFAULT_ALERT_THRESHOLD,
           window_seconds: float = DEFAULT_ALERT_WINDOW) -> list[dict]:
    """
    Faults that have crossed the threshold inside the window.

    Returned rather than delivered. Where an alert GOES is a deployment
    decision -- email, a webhook, a log line someone greps -- and a module that
    picked one would be wrong for every deployment that chose another.
    `student/reminders.py` owns the sender seam, and no sender exists yet.
    """
    report = since(db, hours=window_seconds / 3600.0)
    return [f for f in report["by_fault"] if f["count"] >= threshold]


def _iso_hours_ago(hours: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                         time.gmtime(time.time() - hours * 3600.0))


# ---------------------------------------------------------------------------
# Spend
# ---------------------------------------------------------------------------

def spend_guard(db: Database, *, user_id: str, max_calls: int,
                operation: str = GENERATION, period_hours: float = 24.0) -> Budget:
    """
    A `validator.budget.Budget` preloaded with what this account has already
    spent in the period.

    Reusing that class rather than writing a second counter is the point: it
    already meters at the outbound boundary, already refuses to guess when a
    provider hides its requests, and already treats exhaustion as an ordinary
    exception. What it does not have is a notion of WHOSE spend this is, which
    is the only thing an ingestion loop adds -- so the per-user history is
    loaded in here and the counting stays there.
    """
    spent = db.query_one(
        "SELECT COUNT(*) AS n FROM spend_log WHERE user_id = ? AND operation = ?"
        "   AND created_at >= ?",
        (user_id, operation, _iso_hours_ago(period_hours)))
    budget = Budget(max_calls=max_calls)
    budget.spent[operation] = int(spent["n"] if spent else 0)
    return budget


def charge(db: Database, *, user_id: str, operation: str, budget: Budget,
           units: int = 1, note: str = "") -> None:
    """
    Spend one unit against the ceiling AND write it down, in that order.

    The ceiling is checked first so an exhausted account writes no row: a
    refused call is not spend, and a log that records refusals as spend makes
    the next period's ceiling arrive early.
    """
    for _ in range(max(1, units)):
        try:
            budget.spend(operation)
        except BudgetExhausted as exc:
            raise SpendCeilingReached(
                f"{user_id} has reached the {operation} ceiling of "
                f"{budget.max_calls} for this period: {exc}") from None
    db.execute(
        "INSERT INTO spend_log (id, user_id, operation, units, note, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (new_id("spn"), user_id, operation, max(1, units), note[:500], now_iso()))


def spend_summary(db: Database, *, user_id: str = "", hours: float = 24.0) -> dict:
    cutoff = _iso_hours_ago(hours)
    if user_id:
        rows = db.query(
            "SELECT operation, SUM(units) AS n FROM spend_log"
            " WHERE user_id = ? AND created_at >= ? GROUP BY operation",
            (user_id, cutoff))
    else:
        rows = db.query(
            "SELECT operation, SUM(units) AS n FROM spend_log"
            " WHERE created_at >= ? GROUP BY operation", (cutoff,))
    return {"window_hours": hours, "since": cutoff,
            "by_operation": {r["operation"]: int(r["n"] or 0) for r in rows}}


# ---------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------

def backup(db: Database, destination: str | Path) -> dict:
    """
    A consistent copy, taken with SQLite's own backup API.

    NOT a file copy. In WAL mode the `.db` file is not the database -- the
    recent writes are in the `-wal` sidecar -- so copying it while anything is
    writing produces a file that opens cleanly and is missing the last
    transactions. That is the worst kind of backup: one that looks fine.
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = db.connect()
    target = sqlite3.connect(str(destination))
    try:
        source.backup(target)
        target.commit()
    finally:
        target.close()
    try:
        destination.chmod(0o600)
    except (OSError, NotImplementedError):
        pass
    return {"path": str(destination), "bytes": destination.stat().st_size,
            "taken_at": now_iso()}


def verify(destination: str | Path, *, expect_tables: tuple[str, ...] = ()) -> dict:
    """
    Open the backup and read it.

    A backup nobody has restored is a hypothesis. `integrity_check` catches a
    corrupt file; the row counts catch the subtler failure where the file is
    valid and empty.
    """
    destination = Path(destination)
    if not destination.exists():
        return {"ok": False, "reason": f"no backup at {destination}"}
    con = None
    try:
        con = sqlite3.connect(str(destination))
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [t for t in expect_tables if t not in tables]
        counts = {t: con.execute(f"SELECT COUNT(*) FROM '{t}'").fetchone()[0]
                  for t in sorted(tables) if not t.startswith("sqlite_")}
        return {"ok": integrity == "ok" and not missing,
                "integrity": integrity, "tables": len(tables),
                "missing_tables": missing, "row_counts": counts}
    except sqlite3.DatabaseError as exc:
        # A file so damaged that even PRAGMA integrity_check cannot parse it
        # raises rather than reporting. A verifier that CRASHES on the input it
        # exists to detect is worse than none: the caller learns nothing and
        # the traceback looks like a bug in the backup tool. Caught by its own
        # test, which is the reason this branch is here.
        return {"ok": False, "integrity": f"unreadable: {exc}", "tables": 0,
                "missing_tables": list(expect_tables), "row_counts": {}}
    finally:
        if con is not None:
            con.close()


def restore(backup_path: str | Path, destination: str | Path) -> dict:
    """
    Put a backup back, refusing to overwrite something that is already there.

    A restore that clobbers a live database is how a bad afternoon becomes an
    unrecoverable one. Move the existing file aside deliberately instead.
    """
    backup_path, destination = Path(backup_path), Path(destination)
    if not backup_path.exists():
        raise FileNotFoundError(f"no backup at {backup_path}")
    if destination.exists():
        raise FileExistsError(
            f"{destination} already exists. A restore that overwrites a live database "
            "turns a recoverable problem into an unrecoverable one; move it aside "
            "first if that is really what you want.")
    checked = verify(backup_path)
    if not checked["ok"]:
        raise ValueError(f"refusing to restore an unhealthy backup: {checked}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(str(backup_path))
    target = sqlite3.connect(str(destination))
    try:
        source.backup(target)
        target.commit()
    finally:
        source.close()
        target.close()
    return {"restored_to": str(destination), "from": str(backup_path),
            "row_counts": checked["row_counts"]}
