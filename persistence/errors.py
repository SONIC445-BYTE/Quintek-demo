"""
The errors that mean the same thing on both backends.

A duplicate row is the only database error this codebase catches by type
(`billing/subscriptions.py`, where a replayed gateway webhook must be ignored
rather than raised). SQLite spells it `sqlite3.IntegrityError`; psycopg spells
it `UniqueViolation`. Catching only the first would have turned a harmless
gateway retry into a 500 the moment the service moved to Postgres.

An immutability trigger's refusal is deliberately NOT here. On SQLite
`RAISE(ABORT, ...)` surfaces as `IntegrityError`; on Postgres `RAISE
EXCEPTION` surfaces as SQLSTATE P0001, which is not an integrity violation and
must not be conflated with one. Nothing in the application catches it -- the
write is supposed to fail -- and the tests match on the message, which both
backends preserve exactly.
"""

from __future__ import annotations

import sqlite3


def integrity_errors() -> tuple[type[BaseException], ...]:
    """
    Exception types meaning "a UNIQUE constraint rejected this row".

    Built on demand rather than at import so that a SQLite-only environment
    with no psycopg installed still works -- which is the whole point of
    keeping SQLite as the default backend.
    """
    types: list[type[BaseException]] = [sqlite3.IntegrityError]
    try:
        from psycopg import errors as pg_errors
    except ImportError:
        return tuple(types)
    types.append(pg_errors.UniqueViolation)
    return tuple(types)
