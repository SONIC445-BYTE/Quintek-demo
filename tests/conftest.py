"""
Shared test fixtures.

Two independent concerns live here, merged from the two branches that each
grew a `conftest.py` of their own. They share no symbols and touch nothing in
common; both are session-scoped and autouse-compatible.

  1. THE BACKEND SWITCH -- the same test run against SQLite or Postgres.
  2. EXECUTION-LOG ISOLATION -- the suite must never write to the real
     `executions.jsonl`.

--- 1. the backend switch -------------------------------------------------

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

--- 2. execution-log isolation --------------------------------------------

Test-wide isolation for anything the suite would otherwise write to a shared,
real file.

WHY THIS EXISTS
---------------
`student/ai.py` appends an execution record per model call, defaulting to
`executions.jsonl` at the repository root. That is the same file
`EvalAPI._latency_for` takes a median latency from, and that median is served
to a learner through `/ai/eval`.

Nothing separated the two. Running the suite appended records naming a real
provider -- `openrouter`, which is genuinely registered -- from a scripted test
double, every one with a latency of exactly 12.0 ms. 229 of them accumulated,
42 during the very session that found them. Anything reading that file for
provider health was reading test fixtures as measurements.

The redirect below is the actual fix: the suite cannot reach the real log
because it is never told where it is. `test_execution_log_isolation.py` then
proves the redirect is in force rather than assuming it.
"""

from __future__ import annotations
import os
import pytest

# --- 3. failure capture -----------------------------------------------------
# Every failing test, with its traceback, appended to a durable log, plus one
# line per run. See tests/failure_capture.py for why: an intermittent failure
# whose name was lost with the terminal cannot be diagnosed.
from failure_capture import (  # noqa: E402,F401  -- pytest finds hooks by name
    pytest_runtest_logreport, pytest_sessionfinish, pytest_sessionstart)
import uuid

# ---------------------------------------------------------------------------
# 1. The backend switch
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# 2. Execution-log isolation
# ---------------------------------------------------------------------------
# The path the suite must never touch, resolved once at import.
REAL_EXECUTION_LOG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "executions.jsonl")


@pytest.fixture(scope="session", autouse=True)
def _isolate_execution_log(tmp_path_factory):
    """
    Point every execution-log write at a throwaway file, and label the records.

    Session-scoped and autouse: a per-test opt-in is a per-test opportunity to
    forget, and forgetting is what produced 229 fabricated latency figures in
    the shared log.

    Both halves matter. The redirect stops the suite reaching the real file.
    The origin label means that if some future path writes to a real log
    anyway, the record still cannot be mistaken for a measurement.
    """
    real = os.path.exists(REAL_EXECUTION_LOG)
    before = os.path.getsize(REAL_EXECUTION_LOG) if real else None

    sandbox = tmp_path_factory.mktemp("execution-log") / "executions.jsonl"
    previous = {
        "QUINTEK_EXECUTION_LOG": os.environ.get("QUINTEK_EXECUTION_LOG"),
        "QUINTEK_EXECUTION_ORIGIN": os.environ.get("QUINTEK_EXECUTION_ORIGIN"),
    }
    os.environ["QUINTEK_EXECUTION_LOG"] = str(sandbox)
    os.environ["QUINTEK_EXECUTION_ORIGIN"] = "test-harness"
    try:
        yield sandbox
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        # Checked at session teardown rather than in a test, so it covers the
        # WHOLE run regardless of ordering. A test asserting this could only
        # ever see writes that happened before it ran.
        if real:
            after = os.path.getsize(REAL_EXECUTION_LOG)
            if after != before:
                raise AssertionError(
                    f"the real execution log at {REAL_EXECUTION_LOG} grew from {before} "
                    f"to {after} bytes during this test run. Something wrote to the "
                    "shared log despite the redirect -- find it before trusting any "
                    "provider-health figure computed from that file.")
