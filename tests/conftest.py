"""
Shared fixtures, and the switch that lets the same test run on both backends.

WHY THIS EXISTS
---------------
Before the Postgres port there was no `conftest.py` at all: every test built
its own database inline, so there was no seam at which a second backend could
be introduced. That absence is precisely why four dialect incompatibilities
survived to be found by an audit rather than by the suite.

HOW TO USE IT
-------------
`postgres_url` is the whole contract. Set `QUINTEK_TEST_POSTGRES_URL` and the
Postgres tests run; leave it unset and they SKIP, loudly enough to be visible
in the summary. They are never silently reported as passing -- a skipped
compatibility test and a passing one must not look the same, because the
difference is whether the thing was actually checked.

Running the suite with no Postgres available is still the default, and still
requires no install. That is deliberate: it is the property the repository was
built around.
"""

from __future__ import annotations

import os
import uuid

import pytest

#: Set this to a libpq URL to enable the Postgres half of the suite.
TEST_URL_ENV = "QUINTEK_TEST_POSTGRES_URL"

SKIP_REASON = (
    f"{TEST_URL_ENV} is not set, so the PostgreSQL compatibility tests did not "
    "run. They are SKIPPED, not passed."
)


def postgres_url_or_none() -> str | None:
    return (os.environ.get(TEST_URL_ENV) or "").strip() or None


@pytest.fixture
def postgres_url() -> str:
    """A live Postgres URL, or skip. Never a fake one."""
    url = postgres_url_or_none()
    if url is None:
        pytest.skip(SKIP_REASON)
    return url


@pytest.fixture
def pg_schema(postgres_url, monkeypatch):
    """
    Point the application at Postgres, in a schema of this test's own.

    A unique schema per test is what makes these runnable in any order and
    repeatably: there is no shared state to clean up, and a failure leaves its
    evidence behind instead of poisoning the next test.
    """
    import persistence
    from persistence.postgres import Pool

    suffix = uuid.uuid4().hex[:12]
    schemas = {
        "student": f"t_student_{suffix}",
        "billing": f"t_billing_{suffix}",
        "inference": f"t_inference_{suffix}",
    }
    monkeypatch.setenv(persistence.URL_ENV, postgres_url)
    monkeypatch.setattr(persistence, "STUDENT_SCHEMA", schemas["student"])
    monkeypatch.setattr(persistence, "BILLING_SCHEMA", schemas["billing"])
    monkeypatch.setattr(persistence, "INFERENCE_SCHEMA", schemas["inference"])

    yield schemas

    Pool.close_all()
    import psycopg
    with psycopg.connect(postgres_url, autocommit=True) as conn:
        for schema in schemas.values():
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


@pytest.fixture(params=["sqlite", "postgres"])
def any_backend(request, tmp_path, monkeypatch):
    """
    Run one test body against BOTH backends.

    Yields a factory: call it with a logical name ("student" or "billing") and
    get a connection or Database configured for the backend under test.
    """
    import persistence

    if request.param == "sqlite":
        monkeypatch.delenv(persistence.URL_ENV, raising=False)
        yield _Backend("sqlite", tmp_path)
        return

    url = postgres_url_or_none()
    if url is None:
        pytest.skip(SKIP_REASON)

    import psycopg
    from persistence.postgres import Pool

    suffix = uuid.uuid4().hex[:12]
    student = f"t_student_{suffix}"
    billing = f"t_billing_{suffix}"
    monkeypatch.setenv(persistence.URL_ENV, url)
    monkeypatch.setattr(persistence, "STUDENT_SCHEMA", student)
    monkeypatch.setattr(persistence, "BILLING_SCHEMA", billing)
    try:
        yield _Backend("postgres", tmp_path)
    finally:
        Pool.close_all()
        with psycopg.connect(url, autocommit=True) as conn:
            for schema in (student, billing):
                conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


class _Backend:
    """What a dual-backend test is handed."""

    def __init__(self, name: str, tmp_path):
        self.name = name
        self.tmp_path = tmp_path

    @property
    def is_postgres(self) -> bool:
        return self.name == "postgres"

    def student(self):
        from student.db import Database
        return Database(self.tmp_path / "quintek.db")

    def billing(self):
        from billing.db import connect
        return connect(self.tmp_path / "billing.db")

    def __repr__(self) -> str:
        return f"<backend {self.name}>"
