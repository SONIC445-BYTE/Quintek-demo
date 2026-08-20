"""
Opening the billing database, with additive migrations.

`CREATE TABLE IF NOT EXISTS` is inert against a database that already exists,
so a column added to schema.sql after deployment never appears and the first
query touching it fails on live data. The same trap `student/db.py` fell into;
the same explicit fix.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

# Only additions belong here. Dropping or retyping a column is not safe to do
# as a startup side effect and must be a considered migration.
MIGRATIONS: list[tuple[str, str, str]] = [
    ("plans", "gateway", "TEXT NOT NULL DEFAULT ''"),
    ("plans", "gateway_plan_id", "TEXT NOT NULL DEFAULT ''"),
]


def connect(path: str | Path = "billing.db") -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    apply_migrations(conn)
    return conn


def apply_migrations(conn: sqlite3.Connection) -> list[str]:
    applied = []
    for table, column, ddl in MIGRATIONS:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            applied.append(f"{table}.{column}")
    return applied
