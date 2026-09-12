"""
Turning an account off, and erasing it when asked.

TWO DIFFERENT THINGS, DELIBERATELY NOT ONE
--------------------------------------------
SUSPENSION is reversible and keeps the data. It is what you do when something
is going wrong and you need it to stop NOW -- a runaway loop, an account
behaving badly, a payment dispute. Undoing it must restore the learner
exactly, so nothing is deleted and the record says who suspended them and why.

ERASURE is irreversible and keeps nothing. It is what you do when a person
asks you to delete their data. Conflating the two gives you either a
suspension you cannot undo or a deletion that leaves the data behind, and the
second is the one that turns into a broken promise.

WHY ERASURE ENUMERATES TABLES INSTEAD OF TRUSTING CASCADE
-----------------------------------------------------------
`PRAGMA foreign_keys = ON` is set and most user-owned rows do cascade. Relying
on that would still be wrong here, for two reasons.

The rows are reachable by TWO different columns -- `user_id` on eleven tables
and `owner_id` on two -- and a table added later with neither, or with a
nullable reference, would silently survive. And a cascade is invisible: it
cannot report what it removed, so "your data is deleted" would be a claim
nobody could check.

So `erase()` walks a list, counts what it removed, and then VERIFIES that
nothing remains, raising if it does. `tables_holding_user_data()` derives that
list from the live schema rather than hard-coding it, so a new table with a
`user_id` is covered the day it appears -- the alternative is the
hand-maintained list that has been the gap twice on this project already.

WHAT ERASURE DELIBERATELY DOES NOT TOUCH
------------------------------------------
`incidents` and `spend_log` are operational records, and both are retained
with the user id CLEARED rather than deleted. An incident is evidence that the
system failed; erasing it because the affected account left would mean a fault
could be made to disappear by deleting the person who hit it. The identifying
column goes, the fault stays. `question_reports` is treated the same way: the
report was about a QUESTION, and a wrong question does not stop being wrong
because the person who found it deleted their account.
"""

from __future__ import annotations

import json
from pathlib import Path

from .db import Database, now_iso

ACTIVE = "active"
SUSPENDED = "suspended"

#: Tables whose user-scoped rows are ANONYMISED rather than removed, with why.
#: Listed explicitly: a silent exception to "your data is deleted" would be a
#: broken promise, so each one has to be defensible in writing.
RETAINED_ANONYMISED = {
    "incidents": ("an incident is evidence the system failed. Deleting it because "
                  "the affected account left would let a fault be erased by "
                  "erasing the person who hit it."),
    "spend_log": ("spend is a financial record. The identifying column goes; the "
                  "amounts stay."),
    "question_reports": ("the report was about a QUESTION, and a wrong question "
                         "does not stop being wrong because the person who found "
                         "it deleted their account."),
}


class AccountError(RuntimeError):
    """The account operation cannot be performed, and the message says why."""


def tables_holding_user_data(db: Database) -> list[tuple[str, str]]:
    """
    `(table, column)` for every table that references a user, from the LIVE
    schema.

    Derived, not listed. Twice on this project a hand-maintained inventory has
    been the gap rather than the code it inventoried -- the route table that
    missed `/attempts`, and the body-field predicate that missed
    `storage_key`. A table added tomorrow with a `user_id` is covered by this
    the day it appears.
    """
    con = db.connect()
    found = []
    for (table,) in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"):
        columns = {row[1] for row in con.execute(f"PRAGMA table_info('{table}')")}
        for column in ("user_id", "owner_id"):
            if column in columns:
                found.append((table, column))
    return sorted(found)


def suspend(db: Database, user_id: str, *, by: str, reason: str) -> dict:
    """
    Stop this account working, reversibly.

    `by` and `reason` are required. A suspension with nobody attached is
    indistinguishable from a bug, and the person suspended is entitled to be
    told why by someone who can be asked about it.
    """
    if not (by or "").strip():
        raise AccountError("a suspension needs the name of whoever made it")
    if not (reason or "").strip():
        raise AccountError("a suspension needs a reason the learner could be told")
    if db.query_one("SELECT id FROM users WHERE id = ?", (user_id,)) is None:
        raise AccountError(f"no such user: {user_id}")

    db.execute(
        "UPDATE users SET status = ?, status_reason = ?, status_changed_by = ?,"
        " status_changed_at = ? WHERE id = ?",
        (SUSPENDED, reason.strip()[:1000], by.strip(), now_iso(), user_id))
    # Every live session, not just the current one. A suspension that leaves a
    # valid token in someone's pocket has not stopped anything.
    revoked = db.execute(
        "DELETE FROM sessions_auth WHERE user_id = ?", (user_id,)).rowcount
    return {"user_id": user_id, "status": SUSPENDED, "reason": reason,
            "by": by, "sessions_revoked": revoked}


def reinstate(db: Database, user_id: str, *, by: str) -> dict:
    if not (by or "").strip():
        raise AccountError("a reinstatement needs the name of whoever made it")
    db.execute(
        "UPDATE users SET status = ?, status_reason = '', status_changed_by = ?,"
        " status_changed_at = ? WHERE id = ?",
        (ACTIVE, by.strip(), now_iso(), user_id))
    return {"user_id": user_id, "status": ACTIVE, "by": by}


def status_of(db: Database, user_id: str) -> str:
    row = db.query_one("SELECT status FROM users WHERE id = ?", (user_id,))
    return (row["status"] if row else ACTIVE) or ACTIVE


def export(db: Database, user_id: str) -> dict:
    """
    Everything held about this person, as data they can keep.

    Offered before erasure rather than after, for the obvious reason. The
    password hash and salt are excluded: they are not useful to the learner
    and handing them out is a gift to whoever reads the export.
    """
    user = db.query_one(
        "SELECT id, email, name, role, timezone, created_at FROM users WHERE id = ?",
        (user_id,))
    if user is None:
        raise AccountError(f"no such user: {user_id}")
    out = {"exported_at": now_iso(), "user": dict(user), "tables": {}}
    for table, column in tables_holding_user_data(db):
        if table == "sessions_auth":
            continue          # live credentials, not personal data
        rows = db.query(f"SELECT * FROM '{table}' WHERE {column} = ?", (user_id,))
        if rows:
            out["tables"][table] = [dict(r) for r in rows]
    return out


def erase(db: Database, user_id: str, *, confirm: str) -> dict:
    """
    Delete this person's data, then CHECK that it is gone.

    `confirm` must be the user id. Not ceremony: this is the one irreversible
    operation in the codebase, and an accidental call with the wrong variable
    is exactly how it goes wrong.

    The verification pass at the end is the part that makes the promise
    checkable. A cascade cannot report what it removed, so a deletion that
    relied on one could only be believed.
    """
    if confirm != user_id:
        raise AccountError(
            "erasure must be confirmed with the user id being erased. This is the "
            "only irreversible operation here and it takes no defaults.")
    if db.query_one("SELECT id FROM users WHERE id = ?", (user_id,)) is None:
        raise AccountError(f"no such user: {user_id}")

    removed: dict[str, int] = {}
    anonymised: dict[str, int] = {}
    for table, column in tables_holding_user_data(db):
        if table in RETAINED_ANONYMISED:
            count = db.execute(
                f"UPDATE '{table}' SET {column} = '' WHERE {column} = ?",
                (user_id,)).rowcount
            if count:
                anonymised[table] = count
            continue
        count = db.execute(
            f"DELETE FROM '{table}' WHERE {column} = ?", (user_id,)).rowcount
        if count:
            removed[table] = count
    removed["users"] = db.execute(
        "DELETE FROM users WHERE id = ?", (user_id,)).rowcount

    # The check. Without it, "your data is deleted" is a claim nobody verified.
    survivors = {}
    for table, column in tables_holding_user_data(db):
        if table in RETAINED_ANONYMISED:
            continue
        left = db.query_one(
            f"SELECT COUNT(*) AS n FROM '{table}' WHERE {column} = ?", (user_id,))
        if left and left["n"]:
            survivors[table] = left["n"]
    if survivors:
        raise AccountError(
            f"erasure incomplete: rows for {user_id} remain in {survivors}. The "
            "account has NOT been erased and the caller must not report that it "
            "has.")
    return {"user_id": user_id, "erased_at": now_iso(), "removed": removed,
            "anonymised": anonymised,
            "anonymised_because": {t: RETAINED_ANONYMISED[t] for t in anonymised}}


def erase_uploads(storage_dir: str | Path, storage_keys: list[str]) -> int:
    """
    Delete the files behind a learner's sources.

    Separate from `erase()` because it touches the filesystem and the database
    transaction cannot cover it. Call it BEFORE erase, while the storage keys
    are still readable -- afterwards there is nothing left to look them up
    from, and the files would be orphaned on disk while the record said they
    were gone.
    """
    directory = Path(storage_dir)
    gone = 0
    for key in storage_keys:
        target = directory / key
        try:
            if target.resolve().parent != directory.resolve():
                continue      # never delete outside the storage directory
            target.unlink(missing_ok=True)
            gone += 1
        except OSError:
            continue
    return gone
