"""
Applying `schema.sql` to whichever backend is open, and reading back what a
table's columns are.

Both operations look trivial and both are where a portability bug hides.

INITIALISATION
--------------
The schema files are idempotent by construction -- `CREATE TABLE IF NOT
EXISTS`, `CREATE INDEX IF NOT EXISTS` -- and Postgres understands both. What
Postgres does NOT have is `CREATE TRIGGER IF NOT EXISTS`, so re-running the
schema against an existing database would fail on the second startup. The
triggers are therefore dropped and recreated, which is safe because a trigger
carries no data.

IDEMPOTENT IS NOT THE SAME AS CONCURRENCY-SAFE, and the difference is not
theoretical: `billing/db.py` runs this on every `connect()`, and with a
connection pool that means several threads at once. Two concurrent
`CREATE OR REPLACE FUNCTION` statements deadlock against each other on
`pg_proc` -- observed, not predicted, while running the reservation
concurrency tests. So initialisation is guarded twice:

  * a process-local record of what has already been built, so the DDL runs
    once per process rather than once per connection; and
  * a Postgres advisory lock around the DDL itself, so two PROCESSES
    deploying or starting at the same moment serialise instead of racing.
    Two Render instances booting together is the ordinary case, not the
    exotic one.

COLUMN INTROSPECTION
--------------------
`PRAGMA table_info(x)` is SQLite's answer and `information_schema.columns` is
Postgres's. The additive-migration lists in `student/db.py` and `billing/db.py`
depend on this returning an EMPTY set for a table that does not exist -- that
is how they skip a table `schema.sql` has not created yet -- so both branches
preserve that, rather than raising.
"""

from __future__ import annotations

import threading
from pathlib import Path

from .dialect import schema_to_postgres, trigger_tables

#: What this process has already built, so the DDL is not re-run per
#: connection. Keyed by (backend identity, schema file).
_BUILT: set[tuple[str, str]] = set()
_GUARD = threading.Lock()

#: Arbitrary but fixed: the advisory-lock namespace for schema DDL. Any other
#: use of pg_advisory_lock in this codebase is keyed by a string through
#: hashtext(), so it cannot collide with this integer pair.
_DDL_LOCK_NAMESPACE = 0x5175696E      # 'Quin'


def _is_postgres(conn) -> bool:
    return bool(getattr(conn, "is_postgres", False))


def _identity(conn) -> str:
    """
    What distinguishes one database from another, for the built-once cache.

    For SQLite that is the FILE the connection actually has open, asked of the
    connection itself rather than remembered by the caller -- two `Database`
    objects on one path must share a cache entry, and an in-memory database
    (empty path) must never collide with a file.
    """
    if _is_postgres(conn):
        return f"pg:{conn.schema}"
    try:
        rows = list(conn.execute("PRAGMA database_list"))
        main = next((r for r in rows if r["name"] == "main"), None)
        return f"sqlite:{main['file'] if main else ''}"
    except Exception:
        return f"sqlite:{id(conn)}"


def initialise(conn, schema_path: str | Path, *, force: bool = False) -> None:
    """
    Create everything `schema.sql` declares, idempotently, on either backend.

    Runs at most once per (database, schema file) per process unless `force`.
    Callers may invoke this on every connection -- `billing/db.py` does -- and
    it stays cheap.
    """
    key = (_identity(conn), str(schema_path))
    if not force:
        with _GUARD:
            if key in _BUILT:
                return

    source = Path(schema_path).read_text(encoding="utf-8")
    if not _is_postgres(conn):
        conn.executescript(source)
        conn.commit()
    else:
        _initialise_postgres(conn, source)

    with _GUARD:
        _BUILT.add(key)


def _harden_postgres(conn) -> None:
    """
    Close the Supabase exposure path on every table this schema owns.

    THE THREAT, precisely. Supabase runs PostgREST over the database and
    exposes the `public` schema through it, authenticated with the project's
    anon key. That key is NOT a secret -- it is designed to ship inside client
    applications. So a table sitting in `public` with RLS disabled is readable
    by anyone who has the project URL and that key, entirely outside Quintek's
    own authorization. For the `users` table that means email addresses and
    password hashes.

    Two independent fences, because the failure mode is total:

      1. These tables are not in `public`. They live in `quintek_student`,
         `quintek_billing` and `quintek_inference`, which PostgREST does not
         expose unless somebody adds them to its exposed-schema list.
      2. RLS is ENABLED with NO POLICIES on every table. Under PostgREST's
         anon role that denies everything; a future change that exposes the
         schema by accident therefore leaks nothing.

    Quintek itself is unaffected: it connects as a PostgreSQL role that owns
    these tables, and a table owner bypasses RLS by default. That is the
    reason this is safe to switch on unconditionally -- it removes an
    unintended reader without removing the intended one. `FORCE ROW LEVEL
    SECURITY` is deliberately NOT used, because it would apply RLS to the
    owner too and, with no policies defined, lock the application out of its
    own data.
    """
    for row in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"):
        table = row["tablename"]
        conn.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    conn.commit()


def _initialise_postgres(conn, source: str) -> None:
    """
    The DDL, serialised against any other process doing the same thing.

    A session-scoped advisory lock rather than a transaction-scoped one,
    because the DDL is run in autocommit and there is no surrounding
    transaction to hang it on. The `finally` is therefore load-bearing: a
    leaked session lock would wedge every later deployment.
    """
    lock_id = abs(hash(conn.schema)) % (2 ** 31)
    conn.execute("SELECT pg_advisory_lock(%s, %s)" % (_DDL_LOCK_NAMESPACE, lock_id))
    try:
        for name, table in trigger_tables(source):
            conn.execute(f'DROP TRIGGER IF EXISTS {name} ON "{table}"')
        conn.executescript(schema_to_postgres(source))
        conn.commit()
        _harden_postgres(conn)
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s, %s)"
                     % (_DDL_LOCK_NAMESPACE, lock_id))


def forget_all() -> None:
    """Drop the built-once cache. For tests that create fresh schemas."""
    with _GUARD:
        _BUILT.clear()


def columns_of(conn, table: str) -> set[str]:
    """
    The column names of `table`, or an empty set if it does not exist.

    The empty-set-for-missing-table behaviour is load-bearing: it is what lets
    an additive migration list name a table that this database has not created.
    """
    if not _is_postgres(conn):
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_schema = current_schema() AND table_name = ?", (table,))
    return {r["column_name"] for r in rows}


def table_names(conn) -> set[str]:
    """Every table in the active database or schema. For tests and diagnostics."""
    if not _is_postgres(conn):
        return {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
    return {r["table_name"] for r in conn.execute(
        "SELECT table_name FROM information_schema.tables"
        " WHERE table_schema = current_schema()")}
