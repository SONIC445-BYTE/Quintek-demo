"""
Which database this process talks to, and how it opens it.

ONE VARIABLE DECIDES
--------------------
`QUINTEK_DATABASE_URL` unset  -> SQLite, exactly as before, no install step.
`QUINTEK_DATABASE_URL` set    -> PostgreSQL, pooled.

That is the whole switch, and it is deliberately the whole switch. It is what
makes the rollback real: a deployment that goes wrong on Postgres is reverted
by clearing one environment variable, not by reverting code. It is also what
keeps `python -m pytest` working on a laptop with nothing installed, which is
the property the repository was built around and which a Postgres-only port
would have quietly destroyed.

SCHEMAS, NOT DATABASES
----------------------
The learner engine, billing and the inference ledger are three separate SQLite
FILES today, and nothing joins across them -- identity crosses the boundary in
Python, as a resolved `user_id`, never as SQL. On Postgres they become three
SCHEMAS in one database, which preserves that separation, costs one connection
string instead of three, and keeps them inside one backup and one PITR window.

The schemas are named and are NOT `public`. That is a security requirement,
not tidiness: Supabase exposes the `public` schema over PostgREST using the
anon key, and the anon key is not a secret -- it ships inside clients. Tables
outside `public`, with RLS on and no policies, are not reachable that way.
Quintek itself connects as a Postgres role over TLS and is unaffected by RLS.
"""

from __future__ import annotations

import os
from pathlib import Path

#: The one variable. Absent means SQLite.
URL_ENV = "QUINTEK_DATABASE_URL"

#: Non-`public` on purpose -- see the module docstring.
STUDENT_SCHEMA = "quintek_student"
BILLING_SCHEMA = "quintek_billing"
INFERENCE_SCHEMA = "quintek_inference"


def database_url() -> str | None:
    """The configured Postgres URL, or None for SQLite."""
    url = (os.environ.get(URL_ENV) or "").strip()
    return url or None


def is_postgres() -> bool:
    return database_url() is not None


def require_tls(url: str) -> str:
    """
    Ensure the connection asks for TLS.

    Postgres will happily connect in the clear if the server allows it, and a
    database password plus every learner's data would cross the network
    unencrypted. `sslmode` is only added when the caller has not already
    chosen one, so an explicit `sslmode=verify-full` is never downgraded --
    but an absent one is never left to the driver's default.
    """
    if "sslmode=" in url:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}sslmode=require"


def connect(*, schema: str, sqlite_path: str | Path, sqlite_factory,
            min_size: int = 1, max_size: int = 8):
    """
    Open a connection to whichever backend is configured.

    `sqlite_factory` is passed in rather than imported so this module does not
    have to know how each caller wants its SQLite connection set up -- the
    PRAGMAs differ slightly between the learner and billing databases, and
    that difference is theirs to own.
    """
    url = database_url()
    if url is None:
        return sqlite_factory(sqlite_path)
    from .postgres import Pool
    return Pool.shared(require_tls(url), schema,
                       min_size=min_size, max_size=max_size).connect()


def close_pools() -> None:
    """Release every pooled connection. For test teardown and shutdown."""
    if not is_postgres():
        return
    from .postgres import Pool
    Pool.close_all()
