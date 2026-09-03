"""
The console mounted into the learner server, and the boundary it must not blur.

WHY THIS EXISTS
---------------
The Android app has ONE backend setting, and its two shipped screens read two
different globals: `__QUINTEK_STUDENT_API__` (learner) and `__QUINTEK_API__`
(console). Measured before the mount existed: pointed at the analytics server
every learner route 404'd; pointed at the learner server the console's
`/api/*` and operator `/ai/*` routes 404'd. One origin has to answer both.

The risk in fixing that is routing by accident -- falling through on 404 and
turning a missing notebook into a lookup against the benchmark archive. So
ownership is declared, and these tests hold the line.
"""

from __future__ import annotations

import pytest

from benchmark.analytics_mount import (STUDENT_OWNED_AI_EXACT, AnalyticsMount)


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "/api", "/api/runs", "/api/gates", "/api/preflight", "/api/datasets/validate",
    "/ai/discovery", "/ai/discovery/retired", "/ai/routing/current",
    "/ai/leaderboard", "/ai/reliability", "/ai/candidates",
    "/ai/eval/state", "/ai/eval/tracks", "/ai/eval/runs",
])
def test_the_console_owns_its_own_routes(path):
    assert AnalyticsMount.owns(path) is True


@pytest.mark.parametrize("path", [
    "/ai/eval",                    # served by BOTH, deliberately; learner keeps it
    "/ai/benchmark",
    "/ai/benchmark/categories",
    "/ai/benchmark/ranking",
    "/ai/benchmark/powering",
    "/ai/models/deepseek",
    "/ai/models/deepseek/history",
])
def test_the_learner_api_keeps_the_routes_it_serves_itself(path):
    """
    `student/api.py` answers these, and re-serves `/ai/eval` on purpose so the
    phone's transparency screen is answered by the server holding its session.
    Handing them to the console would answer them without that session.
    """
    assert AnalyticsMount.owns(path) is False


@pytest.mark.parametrize("path", [
    "/notebooks", "/questions", "/progress", "/capabilities", "/demos",
    "/auth/login", "/billing/me/usage", "/billing/admin/economics", "/",
])
def test_the_console_claims_nothing_of_the_learner_surface(path):
    assert AnalyticsMount.owns(path) is False


def test_every_route_the_learner_api_answers_is_excluded():
    """
    The two lists must not drift. If `student/api.py::_ai` grows a route, this
    fails until the mount's exclusion list learns about it -- otherwise the
    console would start answering it, from a different archive.
    """
    import re
    from pathlib import Path
    source = Path("student/api.py").read_text()
    block = re.search(r"def _ai\(.*?(?=\n    def )", source, re.S).group(0)
    exact = set()
    for m in re.findall(r'seg == \[([^\]]+)\]', block):
        parts = [x.strip().strip('"') for x in m.split(",")]
        exact.add("/ai/" + "/".join(parts))
    assert exact, "could not parse the learner API's /ai routes"
    assert exact <= set(STUDENT_OWNED_AI_EXACT), (
        f"the learner API answers {exact - set(STUDENT_OWNED_AI_EXACT)}, which the "
        "console mount would now steal")


# ---------------------------------------------------------------------------
# The mount refuses to write
# ---------------------------------------------------------------------------

class _Recording:
    def __init__(self): self.calls = []
    def handle(self, path, params):
        self.calls.append((path, params))
        return 200, {"ok": True}


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_the_mount_is_read_only(method):
    """
    Promotion is the one analytics surface that writes, and it changes what the
    product serves. It is not reachable through the origin a phone points at.
    """
    api = _Recording()
    status, body = AnalyticsMount(api).handle(method, "/api/runs", {})
    assert status == 405
    assert api.calls == [], "a write reached the analytics API through the mount"


def test_a_get_reaches_the_api_with_parse_qs_shaped_params():
    """
    `AnalyticsAPI.handle` reads `params[key][0]`. The learner API's dispatcher
    flattens params to `dict[str, str]`; handing that form over would make
    every query parameter the string's first character.
    """
    api = _Recording()
    status, _ = AnalyticsMount(api).handle("GET", "/ai/eval", {"category": ["overall"]})
    assert status == 200
    assert api.calls == [("/ai/eval", {"category": ["overall"]})]


def test_a_console_that_cannot_be_built_is_absent_rather_than_fatal(tmp_path, monkeypatch):
    """
    A learner deployment with no benchmark archive must still start. The
    console routes then 404, which is honest.
    """
    monkeypatch.chdir(tmp_path)
    mount = AnalyticsMount.build(runs_root=tmp_path / "nope", root=tmp_path)
    assert mount is None or isinstance(mount, AnalyticsMount)
