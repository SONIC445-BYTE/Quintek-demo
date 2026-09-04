"""
Persistence for the student engine.

Two backends, chosen by one environment variable.

`QUINTEK_DATABASE_URL` unset -> SQLite, from the standard library, for the
reason the rest of this repository is stdlib-only: the harness runs anywhere
with no install step. Set -> PostgreSQL from a bounded pool. See
`persistence/`.

A CORRECTION TO WHAT THIS DOCSTRING USED TO CLAIM
-------------------------------------------------
It said: *"Swapping it for Postgres later is a connection string and a dialect
pass, because nothing below uses a SQLite-only feature except the immutability
triggers, which have direct equivalents."*

That was wrong, and it was the sentence the migration decision leaned on. An
audit against a real PostgreSQL 16 found four incompatibilities, three of them
silent -- the DDL loaded without error and the failure waited for live data:

  1. `source_concepts` and `gap_links` put a NULLABLE column inside a
     composite PRIMARY KEY. SQLite permits it (and, worse, then accepted
     duplicate rows, so `INSERT OR IGNORE` did not deduplicate); Postgres
     silently promotes the column to NOT NULL and rejects the insert. Fixed in
     `schema.sql` with partial unique indexes.
  2. `billing/usage.py`'s `BEGIN IMMEDIATE` has no Postgres equivalent, and
     the obvious translation reopens the allowance-overspend bug it exists to
     prevent.
  3. `revision.py`'s bare-column `GROUP BY` is rejected outright.
  4. `cost_ledger`'s micro-unit columns overflow a 32-bit INTEGER at prices
     already in `configs/model_prices.json`.

The lesson worth keeping: "it looks like standard SQL" is not evidence of
portability. Each of the four now has a regression test that fails on the
unported code.

Two things this module insists on, unchanged:

  * **Foreign keys on, every connection.** SQLite disables them by default,
    per-connection, silently. A schema full of REFERENCES clauses that are
    never enforced is worse than no schema at all, because it reads as if it
    guarantees something. Postgres enforces them always.

  * **One writer at a time, WAL for readers.** The ingestion pipeline writes
    from a worker thread while HTTP requests read. WAL makes that safe without
    a lock dance, and `busy_timeout` turns the rare contention into a wait
    rather than an error. Postgres needs neither.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import persistence
from persistence import schema as schema_support

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
DEFAULT_DB = ROOT / "quintek.db"

# PBKDF2 rounds. Deliberately not a "fast" number: this protects a password
# database, and the cost is paid once per login.
_PBKDF2_ROUNDS = 240_000


def now_iso() -> str:
    """UTC, second resolution, sortable as text. Every timestamp column stores
    this format so string comparison is chronological comparison."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id(prefix: str = "") -> str:
    raw = uuid.uuid4().hex[:16]
    return f"{prefix}_{raw}" if prefix else raw


class Database:
    """A connection factory plus the handful of operations that are about the
    database itself rather than about any one domain object."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else DEFAULT_DB
        self._local = threading.local()
        self.initialise()

    # ---------- connections ----------

    def _sqlite(self, path) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def connect(self):
        """
        One connection per thread.

        SQLite objects are not safe to share across threads and the API server
        is threaded, so the cache is thread-local either way.

        On Postgres this checks one OUT OF A BOUNDED POOL, and the difference
        matters more than it looks. `ThreadingHTTPServer` starts a new thread
        per request, so a thread-local cache is empty on every request: with
        SQLite that costs a file handle, with Postgres it would be a new
        backend per request against a finite server limit. `release()` returns
        it; `student/server.py` calls that at the end of each request.
        """
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        conn = persistence.connect(schema=persistence.STUDENT_SCHEMA,
                                   sqlite_path=self.path,
                                   sqlite_factory=self._sqlite)
        self._local.conn = conn
        return conn

    def release(self) -> None:
        """
        Give this thread's connection back.

        On Postgres it returns to the pool; on SQLite it is a no-op, because
        reopening a file per request is pure cost and the handle is cheap to
        keep. Safe to call when no connection was ever opened.
        """
        conn = getattr(self._local, "conn", None)
        if conn is None or not getattr(conn, "is_postgres", False):
            return
        conn.close()
        self._local.conn = None

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def initialise(self) -> None:
        conn = self.connect()
        schema_support.initialise(conn, SCHEMA_PATH)
        self._apply_additive_migrations(conn)
        conn.commit()

    def _apply_additive_migrations(self, conn) -> None:
        """
        Add columns that `CREATE TABLE IF NOT EXISTS` cannot add.

        The schema file is idempotent for a fresh database and inert for an
        existing one -- which means a column added to schema.sql after a
        database exists never appears in it, and the first query touching that
        column fails at runtime on someone's live data. SQLite's ALTER TABLE
        can add a column with a default cheaply, so every such addition is
        listed here explicitly.

        Explicit rather than derived from parsing schema.sql: a migration list
        that a reader can check line by line is worth more than one that is
        clever. Only additions belong here. A change that drops or retypes a
        column is not safe to apply automatically and must be a considered
        migration, not a startup side effect.
        """
        migrations = [
            ("production_deployments", "deactivated_by", "TEXT NOT NULL DEFAULT ''"),
            ("sources", "byte_size", "INTEGER NOT NULL DEFAULT 0"),
        ]
        for table, column, ddl in migrations:
            existing = schema_support.columns_of(conn, table)
            if not existing:
                continue  # table absent entirely; schema.sql owns creating it
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    # ---------- helpers ----------

    def execute(self, sql: str, params: tuple = ()):
        conn = self.connect()
        cur = conn.execute(sql, params)
        conn.commit()
        return cur

    def query(self, sql: str, params: tuple = ()) -> list:
        return self.connect().execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple = ()):
        return self.connect().execute(sql, params).fetchone()

    # ---------- identity ----------

    @staticmethod
    def _hash_password(password: str, salt: str) -> str:
        return hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), _PBKDF2_ROUNDS
        ).hex()

    def create_user(self, email: str, password: str, *, name: str = "",
                    role: str = "learner", tz: str = "UTC") -> str:
        email = email.strip().lower()
        if not email or "@" not in email:
            raise ValueError("a valid email address is required")
        if len(password) < 8:
            raise ValueError("password must be at least 8 characters")
        salt = secrets.token_hex(16)
        user_id = new_id("usr")
        self.execute(
            "INSERT INTO users (id, email, name, role, timezone, password_salt,"
            " password_hash, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (user_id, email, name, role, tz, salt,
             self._hash_password(password, salt), now_iso()),
        )
        # Every user gets notification preferences immediately, so the rest of
        # the engine can read them without a "row might not exist" branch.
        self.execute(
            "INSERT INTO notification_prefs (user_id, timezone) VALUES (?,?)",
            (user_id, tz),
        )
        return user_id

    def verify_password(self, email: str, password: str) -> str | None:
        row = self.query_one("SELECT id, password_salt, password_hash FROM users WHERE email = ?",
                             (email.strip().lower(),))
        if row is None:
            # Hash anyway. Returning early on an unknown address makes login
            # timing a user-enumeration oracle.
            self._hash_password(password, "decoy-salt")
            return None
        expected = row["password_hash"]
        actual = self._hash_password(password, row["password_salt"])
        return row["id"] if secrets.compare_digest(expected, actual) else None

    def issue_token(self, user_id: str, *, ttl_hours: int = 24 * 30) -> str:
        token = secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
        self.execute(
            "INSERT INTO sessions_auth (token, user_id, created_at, expires_at) VALUES (?,?,?,?)",
            (token, user_id, now_iso(), expires.strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        return token

    def user_for_token(self, token: str | None):
        if not token:
            return None
        row = self.query_one(
            "SELECT u.* FROM sessions_auth s JOIN users u ON u.id = s.user_id"
            " WHERE s.token = ? AND s.expires_at > ?",
            (token, now_iso()),
        )
        return row

    def revoke_token(self, token: str) -> None:
        self.execute("DELETE FROM sessions_auth WHERE token = ?", (token,))
