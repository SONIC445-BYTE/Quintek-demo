"""
The transparency screen over real HTTP, on the learner backend.

`test_student_transparency.py` exercises the service and the router. This
file checks the thing a phone actually does: one origin, one bearer token,
and the whole screen -- including the `/ai/eval` bundle the design files
consume -- served without the admin console's server being involved.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from benchmark.analytics import RunArchive
from student.ai import AIEngine
from student.api import StudentAPI
from student.db import Database
from student.server import make_handler
from tests.test_student_transparency import PASSING, write_run


@pytest.fixture()
def live(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    write_run(runs, "r1", "cand-a", PASSING, timestamp="2026-01-01T00:00:00Z")
    write_run(runs, "r2", "cand-a", PASSING, timestamp="2026-04-01T00:00:00Z")

    db = Database(tmp_path / "student.db")
    engine = AIEngine(db)
    engine.archive = RunArchive(runs)
    engine.promote("QUESTION_GENERATION", "cand-a", "r2", outcome="PASS",
                   activated_by="admin@example.test")

    api = StudentAPI(db, ai=engine)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(api))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    uid = db.create_user("learner@example.test", "a-long-enough-password")
    token = db.issue_token(uid)

    def call(path):
        request = urllib.request.Request(base + path)
        request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    try:
        yield call
    finally:
        server.shutdown()
        server.server_close()


def test_the_whole_screen_is_served_from_one_origin(live):
    for path in ("/ai/benchmark", "/ai/benchmark/categories", "/ai/benchmark/powering",
                 "/ai/benchmark/ranking?category=medical_qa", "/ai/models/cand-a",
                 "/ai/models/cand-a/history", "/ai/eval"):
        status, _ = live(path)
        assert status == 200, path


def test_the_overview_arrives_complete_in_one_call(live):
    status, body = live("/ai/benchmark")
    assert status == 200
    assert body["title"] == "Quintek AI Benchmark"
    assert body["categories"] and body["how_it_works"]
    assert body["powering"]["tasks"]
    assert body["ranking"]["entries"]


def test_a_promotion_is_visible_to_the_learner(live):
    _, body = live("/ai/benchmark/powering")
    task = next(t for t in body["tasks"] if t["task_type"] == "QUESTION_GENERATION")
    assert task["source"] == "promoted"
    assert task["evidence_backed"] is True
    assert task["run_id"] == "r2"


def test_the_learner_is_told_which_tasks_have_no_model(live):
    _, body = live("/ai/benchmark/powering")
    assert body["all_evidence_backed"] is False
    assert "no model at all" in body["unresolved_note"]
    unresolved = [t for t in body["tasks"] if t["source"] == "unresolved"]
    assert unresolved and all(t["name"] is None for t in unresolved)


def test_the_profile_carries_history_from_two_runs(live):
    _, body = live("/ai/models/cand-a")
    assert len(body["history"]) == 2
    assert body["currently_serving"][0]["task_type"] == "QUESTION_GENERATION"


def test_the_eval_bundle_the_design_files_consume_is_served_here_too(live):
    status, body = live("/ai/eval")
    assert status == 200
    # The shape quintek-eval-api.js reads.
    for key in ("state", "candidates", "routing", "tracks", "overview",
                "history", "trackDetail", "overallByCandidate"):
        assert key in body, key
    assert body["state"] != "error"


def test_every_route_needs_a_token(tmp_path):
    db = Database(tmp_path / "s.db")
    api = StudentAPI(db)
    for path in ("/ai/benchmark", "/ai/eval", "/ai/models/cand-a"):
        assert api.handle("GET", path, {}, {}, None)[0] == 401, path
