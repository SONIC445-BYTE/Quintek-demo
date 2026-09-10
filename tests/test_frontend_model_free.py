"""
The model-free screens, and the contract between the client and the routes.

Two things are checked here that neither side can check alone:

  * the node suite (`tests/frontend/model_free_surface.test.mjs`) asserts the
    client's behaviour -- that an outage is an outage, that an unmeasured value
    stays null;
  * the test below asserts the client and the SERVER agree, by deriving the
    route table from `student/api.py` and requiring every path the client can
    build to exist in it.

The second matters because the node suite validates against a hand-written
list of endpoints, and a hand-written list is exactly the thing that drifts
away from the code it describes.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from test_cross_user_access import route_inventory

ROOT = Path(__file__).resolve().parent.parent
SUITE = ROOT / "tests" / "frontend" / "model_free_surface.test.mjs"
CLIENT = ROOT / "frontend" / "quintek-student-api.js"

# `call('METHOD', '/path' ...)` -- the only way the client reaches the backend.
CALL = re.compile(r"call\(\s*'(GET|POST|PUT|DELETE)'\s*,\s*'([^']+)'")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_model_free_client_surface_behaves():
    result = subprocess.run(["node", str(SUITE)], capture_output=True, text=True,
                            cwd=ROOT, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FAIL" not in result.stdout, result.stdout


def _client_paths(keep_trailing_slash: bool = False) -> set[str]:
    """
    Every path the client can build.

    A trailing slash means the literal ends there and the client concatenates
    an id (and possibly more literal path) onto it, so it is kept: it is the
    difference between a complete path and a prefix, and the two are checked
    differently.
    """
    paths = set()
    for _method, path in CALL.findall(CLIENT.read_text(encoding="utf-8")):
        bare = path.split("?")[0]
        paths.add(bare if keep_trailing_slash else bare.rstrip("/"))
    return paths


def test_every_path_the_client_builds_is_a_route_the_server_serves():
    """
    Derived from `student/api.py`, not from a list. A client call to a path
    the server does not route is a 404 the learner sees, and nothing else in
    the suite would catch it -- the node tests use a fake backend that agrees
    with any URL.
    """
    routes, _unparsed = route_inventory()

    def matches(shape, segments) -> bool:
        return (len(shape) == len(segments)
                and all(want is None or want == got
                        for want, got in zip(shape, segments)))

    def served(path: str, partial: bool) -> bool:
        """
        `partial` is a path the client concatenates onto, e.g. '/sources/',
        where the id and any further literal segments are appended at runtime.
        Those are checked as a PREFIX -- '/sources/' is served because
        ('sources', <id>, 'progress') starts with 'sources'. Substituting a
        placeholder into every trailing slot would not work: that route has a
        literal in its LAST slot, not an id.
        """
        segments = [s for s in path.strip("/").split("/") if s]
        if partial:
            return any(len(shape) > len(segments)
                       and all(want is None or want == got
                               for want, got in zip(shape, segments))
                       for shape in routes)
        return any(matches(shape, segments) for shape in routes)

    unserved = [
        path for path in sorted(_client_paths(keep_trailing_slash=True))
        if not served(path, partial=path.endswith("/"))
    ]

    assert not unserved, (
        "the client builds paths student/api.py does not route:\n  "
        + "\n  ".join(unserved))


def test_the_client_reaches_no_generation_endpoint_from_the_model_free_calls():
    """
    The phase's boundary, asserted. Generation-dependent screens stay gated;
    these calls must not quietly depend on one.
    """
    source = CLIENT.read_text(encoding="utf-8")
    start = source.index("THE MODEL-FREE SURFACE")
    end = source.index("What this deployment is actually running")
    block = source[start:end]

    for method, path in CALL.findall(block):
        assert not (method == "POST" and path.endswith("/questions")), (
            f"{method} {path} generates questions, which needs a model call")
        assert "/sources" not in path, (
            f"{method} {path} ingests a source, which needs a model call")
