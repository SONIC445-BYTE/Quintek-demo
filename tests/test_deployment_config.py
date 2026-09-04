"""
The deployment blueprint has to say what the code actually requires.

A `render.yaml` that drifts from the application is not caught by any other
test: the code passes, the suite is green, and the service fails on the
platform where nobody is watching a test runner. These assertions are cheap
and they pin the parts that would be silently wrong.
"""

from __future__ import annotations

from pathlib import Path

import yaml

BLUEPRINT = Path("render.yaml")


def _service() -> dict:
    return yaml.safe_load(BLUEPRINT.read_text(encoding="utf-8"))["services"][0]


def _env() -> dict[str, dict]:
    return {entry["key"]: entry for entry in _service()["envVars"]}


def test_no_secret_value_is_committed():
    """
    Every credential is `sync: false` -- entered in the dashboard, never here.

    This is the test that fails if somebody pastes a working connection string
    into the blueprint to make a deploy go through.
    """
    for key, entry in _env().items():
        if "value" not in entry:
            continue
        value = str(entry["value"])
        assert "://" not in value, f"{key} looks like a URL with credentials"
        assert "@" not in value, f"{key} looks like it embeds a userinfo section"
        assert len(value) < 40, f"{key} carries a suspiciously long committed value"


def test_the_database_url_is_a_dashboard_secret():
    entry = _env()["QUINTEK_DATABASE_URL"]
    assert entry.get("sync") is False
    assert "value" not in entry, "the connection string must never be committed"


def test_production_mode_is_switched_on():
    """Without this the startup checks in student/production.py do nothing."""
    assert _env()["QUINTEK_ENV"]["value"] == "production"


def test_cors_origin_is_set_explicitly():
    """
    Production refuses to boot without it. The blueprint must therefore
    provide it, and `*` is the correct value while the Android WebView is a
    client -- its Origin is the literal string "null" and cannot be
    allowlisted.
    """
    assert _env()["QUINTEK_CORS_ORIGIN"].get("value") == "*"


def test_no_ephemeral_sqlite_paths_remain():
    """
    The old blueprint set QUINTEK_DB_PATH and QUINTEK_BILLING_DB to /tmp.

    Leaving them would be harmless in code but actively misleading in the
    file an operator reads to understand where the data lives.
    """
    env = _env()
    assert "QUINTEK_DB_PATH" not in env
    assert "QUINTEK_BILLING_DB" not in env


def test_the_development_override_is_not_configured():
    """
    The one variable that could put an unqualified model in front of learners.

    Absent here, and refused at startup by student/production.py if set.
    """
    assert "QUINTEK_DEV_CANDIDATE" not in _env()


def test_supabase_client_keys_are_not_requested():
    """
    The architecture connects as a PostgreSQL role over TLS, never through
    PostgREST. Asking for an anon or service-role key would mean somebody had
    changed that without saying so.
    """
    keys = " ".join(_env()).upper()
    for forbidden in ("ANON", "SERVICE_ROLE", "JWT_SECRET", "SUPABASE_KEY"):
        assert forbidden not in keys


def test_the_service_binds_for_the_platform():
    """Render routes in from outside, and assigns the port itself."""
    start = _service()["startCommand"]
    assert "--host 0.0.0.0" in start
    assert "--port $PORT" in start
    assert "8500" not in start, "a hardcoded port would ignore the platform's assignment"


def test_health_is_the_configured_check():
    assert _service()["healthCheckPath"] == "/health"


def test_the_console_is_mounted_so_one_origin_answers_both_screens():
    assert "--with-console" in _service()["startCommand"]


def test_requirements_pin_the_pooled_driver():
    """
    `[pool]` is not optional. Without it there is no bounded pool, and
    ThreadingHTTPServer would open one PostgreSQL backend per request.
    """
    text = Path("requirements.txt").read_text(encoding="utf-8")
    assert "psycopg" in text
    assert "pool" in text
