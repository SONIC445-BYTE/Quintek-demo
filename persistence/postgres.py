"""
A Postgres connection that behaves like the `sqlite3` one the code already has.

WHY AN ADAPTER RATHER THAN A REWRITE
------------------------------------
There are roughly 215 SQL call sites and 724 `?` placeholders across
`student/`, `billing/` and `benchmark/`. Rewriting all of them by hand to
psycopg's spelling is a large mechanical diff in which a single transposed
parameter is invisible in review and shows up as wrong data. The API those
call sites actually use is tiny -- `execute`, `fetchone`, `fetchall`,
iteration, `rowcount`, `commit`, `rollback`, `close`, and row access by column
name -- so it is cheaper and far safer to implement that surface once, here,
and leave the call sites alone.

WHAT THIS DELIBERATELY DOES NOT HIDE
------------------------------------
Three differences are real and are NOT papered over, because pretending they
are the same is how the overspend bug comes back:

  * `BEGIN IMMEDIATE` has no equivalent. Postgres readers do not block, so a
    read-check-write sequence is not serialised by a transaction alone.
    `billing/usage.py` asks for a named lock explicitly; this module only
    provides the mechanism (`advisory_lock`).

  * A trigger's refusal arrives as SQLSTATE P0001, not as an integrity
    violation. `IntegrityError` below is the portable type for a UNIQUE
    conflict only.

  * Connections are POOLED and must be returned. A thread-local connection
    that is never returned is what `ThreadingHTTPServer` would otherwise
    produce -- one Postgres backend per HTTP request.

POOLING AND SUPABASE
--------------------
Supabase's transaction-mode pooler multiplexes one server backend across many
client transactions, so a server-side prepared statement created by one
transaction is not there for the next. psycopg prepares automatically after a
few executions, so `prepare_threshold=None` is set unconditionally. It costs a
little planning time on a repeated query and removes a whole class of
"prepared statement does not exist" failures that only appear under load.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

import psycopg
import psycopg_pool
from psycopg import errors as pg_errors
from psycopg.rows import dict_row

from .dialect import translate_statement

#: Raised by a UNIQUE conflict on either backend. `billing/subscriptions.py`
#: relies on this being the thing a duplicate webhook raises.
IntegrityError = pg_errors.UniqueViolation

#: Bounded, and bounded small on purpose. Supabase's pooler and Postgres both
#: have finite backends, and a starter Render instance serving a stdlib
#: threaded server does not need more. Raise it from configuration, not by
#: editing this constant.
DEFAULT_MIN_POOL = 1
DEFAULT_MAX_POOL = 8


class Row(dict):
    """
    A `dict` that also answers `row["col"]`, `dict(row)` and `.keys()`.

    `sqlite3.Row` supports positional access too. Nothing in this repository
    uses positional access on a query result -- checked -- so it is left out
    rather than emulated badly.
    """


class Cursor:
    """The slice of `sqlite3.Cursor` this codebase uses."""

    def __init__(self, cursor: psycopg.Cursor):
        self._cursor = cursor

    def fetchone(self) -> Row | None:
        row = self._cursor.fetchone()
        return None if row is None else Row(row)

    def fetchall(self) -> list[Row]:
        return [Row(r) for r in self._cursor.fetchall()]

    def __iter__(self) -> Iterator[Row]:
        for row in self._cursor:
            yield Row(row)

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount


class Connection:
    """
    `sqlite3.Connection`'s interface over a pooled psycopg connection.

    Not thread-safe, deliberately: it wraps one pooled connection, and sharing
    one across threads is the bug the pool exists to prevent. Each thread
    checks out its own.
    """

    def __init__(self, raw: psycopg.Connection, *, schema: str, on_close=None):
        self._raw = raw
        self._schema = schema
        self._on_close = on_close
        self._closed = False

    # ---------- the sqlite3 surface ----------

    def execute(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> Cursor:
        cursor = self._raw.cursor(row_factory=dict_row)
        # Always pass a parameter sequence, even an empty one: psycopg only
        # unescapes `%%` when it parses, and it only parses when given params.
        cursor.execute(translate_statement(sql), tuple(params))
        return Cursor(cursor)

    def executemany(self, sql: str, seq: Sequence[Sequence[Any]]) -> None:
        rows = list(seq)
        if not rows:
            return
        with self._raw.cursor() as cursor:
            cursor.executemany(translate_statement(sql), [tuple(r) for r in rows])

    def executescript(self, script: str) -> None:
        """
        Multiple statements in one go, as `schema.sql` needs.

        No parameters are involved, so the script is sent verbatim rather than
        through the placeholder rewrite -- doubling `%` here would corrupt the
        DDL, and there is nothing to substitute into it.
        """
        with self._raw.cursor() as cursor:
            cursor.execute(script)

    def commit(self) -> None:
        """Commit an explicitly opened transaction. A no-op otherwise, which
        is what the many `execute(); commit()` pairs in this codebase expect."""
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._on_close is not None:
            self._on_close(self._raw)

    # ---------- what SQLite does not have ----------

    def advisory_lock(self, key: str) -> None:
        """
        Serialise a read-check-write against everyone else using this key,
        until the current transaction ends.

        This is the replacement for `BEGIN IMMEDIATE`, and it is strictly
        better than what it replaces: SQLite's write lock is database-wide, so
        two different users' reservations queued behind each other. This one
        is per key, so they do not.

        `pg_advisory_xact_lock` releases on COMMIT or ROLLBACK with no
        `finally` to forget, which matters because the alternative -- a
        session-scoped lock leaked by an exception -- would wedge a pooled
        connection permanently.
        """
        self._raw.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (key,))

    @property
    def raw(self) -> psycopg.Connection:
        return self._raw

    @property
    def is_postgres(self) -> bool:
        return True

    @property
    def schema(self) -> str:
        return self._schema


class Pool:
    """
    One bounded pool per (url, schema), shared by every thread in the process.

    Process-wide rather than per-`Database`, because several objects
    (`Database`, `BillingMount`, `CostRecorder`) legitimately point at the
    same database and must not each open their own set of backends.
    """

    _instances: dict[tuple[str, str], "Pool"] = {}
    _guard = threading.Lock()

    def __init__(self, url: str, schema: str, *, min_size: int, max_size: int):
        self.url = url
        self.schema = schema
        # Create the schema ONCE, before the pool opens, on a throwaway
        # connection. Doing it in `configure` raced: several pool connections
        # opened together and `CREATE SCHEMA IF NOT EXISTS` is not atomic
        # against a concurrent create -- it fails on the unique index over
        # pg_namespace rather than being ignored.
        with psycopg.connect(url, autocommit=True) as bootstrap:
            bootstrap.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')

        self._pool = psycopg_pool.ConnectionPool(
            conninfo=url,
            min_size=min_size,
            max_size=max_size,
            open=True,
            timeout=30.0,
            kwargs={
                # See the module docstring: required for transaction-mode
                # pooling, harmless otherwise.
                "prepare_threshold": None,
                # AUTOCOMMIT, matching `billing/db.py`'s SQLite connection
                # (`isolation_level=None`) and `student/db.py`'s
                # execute-then-commit contract. This is a parity decision, not
                # a laxity one, and it was measured:
                #
                #   * explicit BEGIN / COMMIT / ROLLBACK still work, which is
                #     what billing/usage.py's reservation depends on;
                #   * a standalone failed statement does NOT poison the
                #     session, exactly as on SQLite -- without this, one
                #     refused write (an immutability trigger firing, a
                #     duplicate webhook) left the connection unusable for
                #     every later request that borrowed it from the pool;
                #   * a failure INSIDE an explicit transaction still aborts
                #     that transaction, which is what a transaction means and
                #     is not papered over.
                "autocommit": True,
                "options": f"-c search_path={schema},public",
            },
        )

    @classmethod
    def shared(cls, url: str, schema: str, *, min_size: int = DEFAULT_MIN_POOL,
               max_size: int = DEFAULT_MAX_POOL) -> "Pool":
        key = (url, schema)
        with cls._guard:
            pool = cls._instances.get(key)
            if pool is None:
                pool = cls(url, schema, min_size=min_size, max_size=max_size)
                cls._instances[key] = pool
            return pool

    def connect(self) -> Connection:
        raw = self._pool.getconn()
        return Connection(raw, schema=self.schema, on_close=self._put_back)

    def _put_back(self, raw: psycopg.Connection) -> None:
        try:
            raw.rollback()   # never hand back a connection mid-transaction
        except Exception:
            pass
        self._pool.putconn(raw)

    def close(self) -> None:
        self._pool.close()
        with self._guard:
            type(self)._instances.pop((self.url, self.schema), None)

    @classmethod
    def close_all(cls) -> None:
        with cls._guard:
            pools = list(cls._instances.values())
            cls._instances.clear()
        for pool in pools:
            pool._pool.close()
