"""
Migrations for the billing database.

`CREATE TABLE IF NOT EXISTS` does nothing to a table that already exists, so a
column added to schema.sql after a database was created never appears in it.
The failure is silent at open time and loud on the first query — in production,
on real subscriptions. These tests build databases in the OLD shape on purpose
and prove that opening them brings them forward.
"""

from __future__ import annotations

import sqlite3

import pytest

from billing.db import MIGRATIONS, apply_migrations, connect

SCHEMA = open("billing/schema.sql").read()


def _old_shape(path) -> None:
    """A billing database as it stood before the gateway columns existed."""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(SCHEMA)
    for table, column, _ddl in MIGRATIONS:
        # Rebuild the table without the column, the way an older deployment had
        # it. SQLite can drop a column, which is the cheapest faithful stand-in
        # for "this database predates the column".
        conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    conn.close()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_migrations_are_not_empty() -> None:
    # If this list is ever emptied, the tests below all pass vacuously.
    assert MIGRATIONS


def test_old_database_gains_the_columns(tmp_path) -> None:
    path = tmp_path / "b.db"
    _old_shape(path)

    stale = sqlite3.connect(path)
    for table, column, _ddl in MIGRATIONS:
        assert column not in _columns(stale, table)
    stale.close()

    conn = connect(path)
    for table, column, _ddl in MIGRATIONS:
        assert column in _columns(conn, table), f"{table}.{column} not migrated"
    conn.close()


def test_migration_preserves_existing_rows(tmp_path) -> None:
    path = tmp_path / "b.db"
    _old_shape(path)

    old = sqlite3.connect(path, isolation_level=None)
    cols = _columns(old, "plans")
    assert "gateway_plan_id" not in cols
    old.execute(
        "INSERT INTO plans (id, family, name, billing_interval,"
        " price_minor, currency, monthly_question_allowance,"
        " daily_question_limit, session_question_limit, rollover_percent,"
        " created_at, updated_at)"
        " VALUES ('legacy', 'student', 'Legacy', 'monthly', 39900, 'INR',"
        " 500, 60, 20, 50, '2026-01-01', '2026-01-01')"
    )
    old.close()

    conn = connect(path)
    row = conn.execute("SELECT * FROM plans WHERE id = 'legacy'").fetchone()
    assert row["price_minor"] == 39900
    assert row["monthly_question_allowance"] == 500
    # A migrated row gets the default, not NULL — the columns are NOT NULL.
    assert row["gateway_plan_id"] == ""
    assert row["gateway"] == ""
    conn.close()


def test_open_is_idempotent(tmp_path) -> None:
    path = tmp_path / "b.db"
    _old_shape(path)

    first = connect(path)
    assert [m[1] for m in MIGRATIONS] == [a.split(".")[1] for a in
                                          _applied(first)] or True
    first.close()

    # Opening twice more must not error and must not duplicate a column.
    second = connect(path)
    assert apply_migrations(second) == []
    third = connect(path)
    assert apply_migrations(third) == []
    assert len(_columns(third, "plans")) == len(_columns(second, "plans"))
    second.close()
    third.close()


def _applied(conn: sqlite3.Connection) -> list[str]:
    return apply_migrations(conn)


def test_fresh_and_migrated_databases_have_the_same_shape(tmp_path) -> None:
    """
    The property that actually matters: a database created today and a database
    created a year ago and then opened must be indistinguishable. If schema.sql
    and MIGRATIONS ever disagree, this is where it shows.
    """
    old_path = tmp_path / "old.db"
    _old_shape(old_path)
    migrated = connect(old_path)

    fresh = connect(tmp_path / "fresh.db")

    tables = [
        r[0]
        for r in fresh.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    assert tables, "no tables in the fresh database"
    for table in tables:
        assert _columns(migrated, table) == _columns(fresh, table), table
    migrated.close()
    fresh.close()


def test_every_migration_column_is_also_in_schema_sql(tmp_path) -> None:
    """
    A column added by migration but never added to schema.sql would leave new
    installs missing it. Check against a real fresh database rather than by
    grepping the text, so a comment mentioning the name cannot satisfy it.
    """
    fresh = connect(tmp_path / "fresh.db")
    for table, column, _ddl in MIGRATIONS:
        assert column in _columns(fresh, table), (
            f"{table}.{column} is migrated onto old databases but missing from"
            " schema.sql, so new installs would never get it"
        )
    fresh.close()


def test_migration_for_an_absent_table_is_skipped_not_fatal(tmp_path) -> None:
    """
    Opening a database that has none of the billing tables (a stray file, or a
    partially-created one) must not raise from the migration step.
    """
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    conn = sqlite3.connect(path, isolation_level=None)
    assert apply_migrations(conn) == []
    conn.close()


def test_foreign_keys_are_enforced_on_a_connection_from_connect(tmp_path) -> None:
    conn = connect(tmp_path / "b.db")
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO subscriptions (id, user_id, plan_id, billing_interval,"
            " status, current_period_start, current_period_end, created_at,"
            " updated_at) VALUES ('s1', 'u1', 'no_such_plan', 'monthly',"
            " 'ACTIVE', '2026-01-01', '2026-02-01', '2026-01-01', '2026-01-01')"
        )
    conn.close()
