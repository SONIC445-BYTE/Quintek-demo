"""
Opening the billing database, with additive migrations.

`CREATE TABLE IF NOT EXISTS` is inert against a database that already exists,
so a column added to schema.sql after deployment never appears and the first
query touching it fails on live data. The same trap `student/db.py` fell into;
the same explicit fix.

Backend is chosen by `QUINTEK_DATABASE_URL` -- see `persistence/`. `schema.sql`
is the single source of truth for both; the PRAGMAs and the two append-only
triggers are the only parts that need translating, and `persistence.dialect`
does that rather than a second hand-maintained file that would drift.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import persistence
from persistence import schema as schema_support

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

# Only additions belong here. Dropping or retyping a column is not safe to do
# as a startup side effect and must be a considered migration.
MIGRATIONS: list[tuple[str, str, str]] = [
    ("plans", "gateway", "TEXT NOT NULL DEFAULT ''"),
    ("plans", "gateway_plan_id", "TEXT NOT NULL DEFAULT ''"),
]


def _sqlite(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL so a reader thread does not block behind a writer. Several request
    # threads read entitlements while one commits usage; without it they
    # serialise behind each other and a batch reservation stalls the UI.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def connect(path: str | Path = "billing.db"):
    """
    A billing connection, on whichever backend is configured.

    On Postgres this checks a connection OUT OF A BOUNDED POOL. The caller
    owns returning it -- `close()` puts it back rather than destroying it.
    """
    conn = persistence.connect(schema=persistence.BILLING_SCHEMA,
                               sqlite_path=path, sqlite_factory=_sqlite)
    schema_support.initialise(conn, SCHEMA_PATH)
    apply_migrations(conn)
    return conn


def apply_migrations(conn) -> list[str]:
    """
    Add columns `CREATE TABLE IF NOT EXISTS` cannot add.

    Column introspection is portable via `persistence.schema.columns_of`:
    SQLite answers with `PRAGMA table_info`, Postgres with
    `information_schema.columns`. The list itself is unchanged and still
    readable line by line, which was always the point.
    """
    applied = []
    for table, column, ddl in MIGRATIONS:
        existing = schema_support.columns_of(conn, table)
        if not existing:
            continue
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            applied.append(f"{table}.{column}")
    if applied:
        conn.commit()
    return applied
