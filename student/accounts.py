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

import secrets

from .db import UNUSABLE_PASSWORD_HASH, Database, new_id, now_iso

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


#: The placeholder severed attempts point to (ADR-029). A FIXED id that
#: `new_id` can never produce ("erased" is not hex), an email with no "@" so
#: nobody can register it, and no usable password. Its role is 'learner'
#: because `users.role` is CHECKed to learner|admin and a CHECK cannot be
#: changed in place on SQLite; it can never be logged into either way.
#: One row for every erased learner -- not one per learner -- so severed rows
#: cannot even be grouped back into a single anonymous person's history.
ERASED_USER = "usr_erased"
ERASED_NOTEBOOK = "nb_erased"


def _ensure_placeholders(db: Database) -> None:
    db.execute(
        "INSERT OR IGNORE INTO users (id, email, name, role, timezone, password_salt,"
        " password_hash, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (ERASED_USER, "erased", "", "learner", "UTC", "", UNUSABLE_PASSWORD_HASH, now_iso()))
    db.execute(
        "INSERT OR IGNORE INTO notebooks (id, owner_id, title, subject, created_at)"
        " VALUES (?,?,?,?,?)", (ERASED_NOTEBOOK, ERASED_USER, "", "", now_iso()))
    # OR IGNORE also swallows a CHECK or NOT NULL failure on SQLite -- the
    # first draft of this used a role the CHECK refuses, and nothing said so.
    # So confirm the rows are really there before anything points at them.
    if db.query_one("SELECT 1 FROM notebooks n JOIN users u ON u.id = n.owner_id"
                    " WHERE n.id = ? AND u.id = ?", (ERASED_NOTEBOOK, ERASED_USER)) is None:
        raise AccountError("the erased-accounts placeholder could not be created; "
                           "nothing has been erased")


def _sever(db: Database, user_id: str) -> dict:
    """Detach the learner's attempts, and the questions they answered, from
    the learner -- BEFORE anything is deleted, because deleting the notebook
    would cascade to those questions and then to the attempts, which the
    immutability trigger refuses (that refusal WAS ADR-029).

    Attempts: moved to ERASED_USER with the session, typed gap labels and
    upload pointers cleared. Every evidence column is untouched; the trigger
    permits exactly this and nothing else.

    Questions the learner answered: kept, because an attempt must name its
    question, but moved to ERASED_NOTEBOOK and stripped of everything derived
    from the learner's uploads -- stem, option text (the NUMBER of options is
    kept, so the recorded answer index still means something), rationale, the
    source and passage pointers, their demonstrations and the validator's
    notes, which quote the stem. What remains is structure: family,
    difficulty, the correct index, which model wrote it, its validation
    status and its concept tags.

    Reports on those questions (retained, see RETAINED_ANONYMISED) lose the
    reporter AND the words they typed, and the frozen provenance, which holds
    the stem and the passage."""
    _ensure_placeholders(db)
    kept = [r["question_id"] for r in db.query(
        "SELECT DISTINCT a.question_id FROM attempts a"
        " JOIN questions q ON q.id = a.question_id"
        " JOIN notebooks n ON n.id = q.primary_notebook_id"
        " WHERE a.user_id = ? OR n.owner_id = ?", (user_id, user_id))]
    attempts = db.execute(
        "UPDATE attempts SET user_id = ?, session_id = NULL, knowledge_gaps_json = '[]',"
        " source_refs_json = '[]' WHERE user_id = ?", (ERASED_USER, user_id)).rowcount
    for qid in kept:
        row = db.query_one("SELECT options_json FROM questions WHERE id = ?", (qid,))
        try:
            count = len(json.loads(row["options_json"] or "[]"))
        except (TypeError, ValueError):
            count = 0
        db.execute(
            "UPDATE questions SET primary_notebook_id = ?, stem = '', options_json = ?,"
            " rationale = '', source_id = NULL, chunk_id = NULL, demo_ids_json = '[]',"
            " validation_json = '{}' WHERE id = ?",
            (ERASED_NOTEBOOK, json.dumps([""] * count), qid))
    reports = db.execute(
        "UPDATE question_reports SET user_id = ?, note = '', provenance_json = '{}'"
        " WHERE user_id = ?", (ERASED_USER, user_id)).rowcount
    return {"attempts": attempts, "questions": len(kept), "question_reports": reports}


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
    # `persistence.schema.table_names` and `.columns_of`, NOT `sqlite_master`
    # and `PRAGMA table_info`.
    #
    # Those two were the original implementation and they are SQLite-only, so
    # on PostgreSQL this raised `UndefinedTable: relation "sqlite_master" does
    # not exist` before it examined a single table -- and `erase()` calls this
    # first, so **erasure failed for every account on the backend production
    # actually runs on.** A learner asking to be forgotten got a 500.
    #
    # ADR-020 catalogued four dialect incompatibilities and translated the
    # SQLite-only constructs out of `schema.sql`. This one was not in
    # `schema.sql`. It was runtime introspection in application code, so the
    # DDL translation never saw it and the audit that found the other four was
    # not looking here. It is the fifth.
    #
    # The two helpers below already existed, in `persistence/schema.py`,
    # written for exactly this and used by the migration path. This function
    # simply never called them.
    from persistence import schema as pschema

    con = db.connect()
    found = []
    for table in pschema.table_names(con):
        columns = pschema.columns_of(con, table)
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
        rows = db.query(f'SELECT * FROM "{table}" WHERE {column} = ?', (user_id,))
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
    if user_id == ERASED_USER:
        raise AccountError("the erased-accounts placeholder is not an account")
    if db.query_one("SELECT id FROM users WHERE id = ?", (user_id,)) is None:
        raise AccountError(f"no such user: {user_id}")

    severed = _sever(db, user_id)
    removed: dict[str, int] = {}
    anonymised: dict[str, int] = {}
    for table, column in tables_holding_user_data(db):
        if table in RETAINED_ANONYMISED:
            count = db.execute(
                f'UPDATE "{table}" SET {column} = \'\' WHERE {column} = ?',
                (user_id,)).rowcount
            if count:
                anonymised[table] = count
            continue
        count = db.execute(
            f'DELETE FROM "{table}" WHERE {column} = ?', (user_id,)).rowcount
        if count:
            removed[table] = count
    removed["users"] = db.execute(
        "DELETE FROM users WHERE id = ?", (user_id,)).rowcount

    # The check. Without it, "your data is deleted" is a claim nobody verified.
    # EVERY table, the retained ones included: an anonymised row still
    # carrying the id would be a row that was not anonymised.
    survivors = {}
    for table, column in tables_holding_user_data(db):
        left = db.query_one(
            f'SELECT COUNT(*) AS n FROM "{table}" WHERE {column} = ?', (user_id,))
        if left and left["n"]:
            survivors[table] = left["n"]
    if survivors:
        raise AccountError(
            f"erasure incomplete: rows for {user_id} remain in {survivors}. The "
            "account has NOT been erased and the caller must not report that it "
            "has.")
    return {"user_id": user_id, "erased_at": now_iso(), "removed": removed,
            "severed": severed, "anonymised": anonymised,
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


# ---------------------------------------------------------------------------
# The first admin, and setting a password nobody else chose
# ---------------------------------------------------------------------------
#
# There is no route that makes an admin, deliberately: an HTTP path to
# privilege is a path an attacker can find. The first admin is made OUT OF
# BAND -- at server start, from an environment variable only the operator can
# set -- and it is made WITHOUT a password. The operator then sets one with
# `python -m benchmark.cli set-password`, which prompts for it and never puts
# it on a command line, in a log, or in anything this code prints.

ADMIN = "admin"
PASSWORD_MIN = 12


def bootstrap_admin(db: Database, email: str) -> dict:
    """Create the admin account named by `email`, with no usable password.

    Idempotent. Never ELEVATES: if the address already belongs to a learner
    it is refused, because otherwise anyone who registered that address before
    the operator's deploy would be handed admin rights by it."""
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise AccountError("the bootstrap admin email is not an email address")
    row = db.query_one("SELECT id, role FROM users WHERE email = ?", (email,))
    if row is not None:
        if (row["role"] or "") == ADMIN:
            return {"user_id": row["id"], "created": False, "outcome": "already an admin"}
        raise AccountError(
            f"{email} already exists as a {row['role']} account; the bootstrap never "
            "promotes an existing account. Use another address.")
    user_id = new_id("usr")
    db.execute(
        "INSERT INTO users (id, email, name, role, timezone, password_salt,"
        " password_hash, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (user_id, email, "Operator", ADMIN, "UTC", secrets.token_hex(16),
         UNUSABLE_PASSWORD_HASH, now_iso()))
    return {"user_id": user_id, "created": True,
            "outcome": "created with no password; run set-password"}


def set_password(db: Database, email: str, password: str) -> str:
    """Give an account a new password and sign out every session it has."""
    email = (email or "").strip().lower()
    if not isinstance(password, str) or len(password) < PASSWORD_MIN:
        raise AccountError(f"the password must be at least {PASSWORD_MIN} characters")
    row = db.query_one("SELECT id FROM users WHERE email = ?", (email,))
    if row is None:
        raise AccountError(f"no account with the address {email}")
    salt = secrets.token_hex(16)
    db.execute("UPDATE users SET password_salt = ?, password_hash = ? WHERE id = ?",
               (salt, Database._hash_password(password, salt), row["id"]))
    # A password change that leaves old sessions alive is not a reset.
    db.execute("DELETE FROM sessions_auth WHERE user_id = ?", (row["id"],))
    return row["id"]
