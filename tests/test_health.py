"""
The health endpoint a deployment platform polls.

It has to answer three questions from outside, without a session: is the
process up, can it reach its database, and can it generate. The third is the
one that matters here -- "no qualified model" is a correct, healthy state, not
an error, and a health check that conflated them would either mark a correctly
refusing server as broken or hide a genuinely broken one.
"""

from __future__ import annotations

import pytest

from student.api import StudentAPI
from student.db import Database


@pytest.fixture
def api(tmp_path):
    return StudentAPI(Database(tmp_path / "h.db"))


def test_health_needs_no_session(api):
    status, body = api.handle("GET", "/health", {}, None, None)
    assert status == 200
    assert body["status"] == "ok"
    assert body["database"] is True


def test_health_reports_generation_unavailable_without_ai(api):
    _s, body = api.handle("GET", "/health", {}, None, None)
    assert body["ai_configured"] is False
    assert body["generation"] == "unavailable"


def test_no_qualified_model_is_healthy_not_broken(tmp_path):
    """
    The state Quintek is actually in. Refusing to generate is correct
    behaviour; a platform must not restart the server over it.
    """
    class NoModel:
        def resolve(self, task):
            raise RuntimeError("NoEligibleModel: nothing is promoted")

    api = StudentAPI(Database(tmp_path / "h.db"))
    api.ai = NoModel()
    status, body = api.handle("GET", "/health", {}, None, None)
    assert status == 200, "a correctly refusing server was reported unhealthy"
    assert body["generation"] == "no_qualified_model"
    assert body["status"] == "ok"


def test_an_unreachable_database_is_503(tmp_path):
    """The one condition that genuinely makes the server useless."""
    class Broken:
        def query_one(self, *a, **k):
            raise OSError("database is locked")

    api = StudentAPI(Database(tmp_path / "h.db"))
    api.db = Broken()
    status, body = api.handle("GET", "/health", {}, None, None)
    assert status == 503
    assert body["database"] is False
    assert body["status"] == "degraded"
    assert "database_error" in body


def test_health_leaks_no_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-shouldnotappear-0123456789")
    api = StudentAPI(Database(tmp_path / "h.db"))
    _s, body = api.handle("GET", "/health", {}, None, None)
    assert "nvapi-" not in str(body)
