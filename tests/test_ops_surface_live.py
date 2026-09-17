"""
The operations surface, over HTTP, against a running server.

WHY THIS FILE EXISTS
--------------------
`tests/test_operations.py` tests `student/operations.py` thoroughly: incidents
are recorded, faults are grouped by type rather than by message, the ceiling
refuses, backups restore. Every one of those is a call into a Python function.

`/ops/incidents`, `/ops/alerts` and `/ops/spend` had **no test at all**. The
functions were proven; the three routes that are the only way an operator
reaches them were not. That is the same shape as the defect ADR-026 records
for the report queue -- work that terminates in a table nobody can open --
and the same shape as ADR-027, where everything around the artefact was tested
and the artefact itself was never run.

So this file starts the real server on a socket and drives the routes the way
an operator would: over HTTP, with a bearer token, against a database with
real rows in it.

WHAT "REAL" MEANS HERE, AND WHAT IT DOES NOT
--------------------------------------------
Real: a socket, a running `student/server.py`, an admin token, incidents and
spend rows written by the production code paths, a ceiling that actually
refuses a call.

Not real: no provider is contacted and no money is spent. The ceiling is
exercised by charging against it, which is what `charge()` does on the live
path too -- the outbound call is the thing on the other side of the ceiling,
not the thing being tested.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from student.db import Database
from student import operations as ops


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _request(origin, method, path, token=None, body=None):
    """Returns (status, payload). A non-2xx is a value, not an exception --
    this file asserts on 404s as much as on 200s."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{origin}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            return res.status, json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read() or b"{}"
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw.decode(errors="replace")}


@pytest.fixture
def live(tmp_path):
    """A running server, an admin, a learner, and a handle on the same
    database the server is using."""
    db_path = tmp_path / "ops.db"
    db = Database(db_path)

    from student.api import StudentAPI
    api = StudentAPI(db)
    learner = api.handle("POST", "/auth/register", {},
                         {"email": "learner@example.com",
                          "password": "correct-horse"}, None)[1]["token"]
    operator = api.handle("POST", "/auth/register", {},
                          {"email": "ops@example.com",
                           "password": "correct-horse"}, None)[1]["token"]
    db.execute("UPDATE users SET role = 'admin', name = 'Dr Ops' WHERE email = ?",
               ("ops@example.com",))
    learner_id = db.query_one("SELECT id FROM users WHERE email = ?",
                              ("learner@example.com",))["id"]

    port = _free_port()
    origin = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [sys.executable, "-m", "benchmark.cli", "serve-student",
         "--host", "127.0.0.1", "--port", str(port), "--db", str(db_path), "--no-ai"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    deadline = time.time() + 25
    up = False
    while time.time() < deadline:
        try:
            status, _ = _request(origin, "GET", "/health")
            if status == 200:
                up = True
                break
        except Exception:
            pass
        time.sleep(0.2)

    if not up:
        proc.kill()
        out, err = proc.communicate(timeout=5)
        # A skip would hide this. If the server cannot start, nothing below
        # was checked and the run must say so.
        pytest.fail(f"the server did not start, so NOTHING was verified:\n"
                    f"{err.decode(errors='replace')[:2000]}")

    yield {"origin": origin, "db": db, "learner": learner, "operator": operator,
           "learner_id": learner_id, "request": lambda *a, **k: _request(origin, *a, **k)}

    proc.kill()
    proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# Incidents and alerting
# ---------------------------------------------------------------------------

def test_incidents_recorded_by_the_engine_are_readable_over_http(live):
    """A failure written by `operations.record` reaches an operator's screen."""
    for i in range(3):
        ops.record(live["db"], operation=ops.INGESTION,
                   error=ValueError(f"chunk {i} could not be read"),
                   user_id=live["learner_id"], context={"source": f"src-{i}"})

    status, body = live["request"]("GET", "/ops/incidents", live["operator"])
    assert status == 200, body
    assert body["total"] == 3, body

    faults = {(f["operation"], f["error_type"]): f for f in body["by_fault"]}
    assert (ops.INGESTION, "ValueError") in faults, body
    fault = faults[(ops.INGESTION, "ValueError")]

    # GROUPED BY FAULT, NOT BY MESSAGE. Three different messages -- each
    # interpolating a different chunk number -- are one fault, not three.
    # Grouping on the message would report three separate problems and hide
    # that one thing is failing repeatedly.
    assert fault["count"] == 3, (
        "three instances of one fault were reported as separate faults; the "
        "route is grouping on the message")
    assert "chunk 2" in fault["latest"], "the latest message should be the newest"


def test_alerts_fire_only_once_a_fault_repeats(live):
    """One failure is noise. The same failure five times is a state."""
    status, body = live["request"]("GET", "/ops/alerts", live["operator"])
    assert status == 200 and body["alerts"] == [], "a quiet system must alert on nothing"

    for _ in range(ops.DEFAULT_ALERT_THRESHOLD - 1):
        ops.record(live["db"], operation=ops.GENERATION, error=TimeoutError("provider timed out"))
    status, body = live["request"]("GET", "/ops/alerts", live["operator"])
    assert body["alerts"] == [], (
        f"an alert fired at {ops.DEFAULT_ALERT_THRESHOLD - 1} occurrences, below "
        f"the threshold of {ops.DEFAULT_ALERT_THRESHOLD}")

    ops.record(live["db"], operation=ops.GENERATION, error=TimeoutError("provider timed out"))
    status, body = live["request"]("GET", "/ops/alerts", live["operator"])
    assert status == 200
    assert len(body["alerts"]) == 1, body
    assert body["alerts"][0]["error_type"] == "TimeoutError"
    assert body["alerts"][0]["count"] == ops.DEFAULT_ALERT_THRESHOLD


def test_a_distinct_fault_does_not_borrow_another_faults_count(live):
    """Four of one fault and four of another is not an alert, however it is
    added up. The threshold is per fault."""
    for _ in range(4):
        ops.record(live["db"], operation=ops.GENERATION, error=TimeoutError("timed out"))
        ops.record(live["db"], operation=ops.GENERATION, error=ConnectionError("refused"))
    status, body = live["request"]("GET", "/ops/alerts", live["operator"])
    assert body["alerts"] == [], (
        "eight incidents across two faults fired an alert; the counts are being pooled")


# ---------------------------------------------------------------------------
# The spend ceiling
# ---------------------------------------------------------------------------

def test_the_ceiling_refuses_and_the_refusal_is_not_logged_as_spend(live):
    """The ceiling is checked BEFORE the row is written.

    A refused call is not spend. Logging it as spend would make the next
    period's ceiling arrive early -- an account that hit the wall once would
    then hit it sooner every period after.
    """
    db, uid = live["db"], live["learner_id"]
    budget = ops.spend_guard(db, user_id=uid, max_calls=3, operation=ops.GENERATION)

    for i in range(3):
        ops.charge(db, user_id=uid, operation=ops.GENERATION, budget=budget,
                   note=f"call {i}")

    with pytest.raises(ops.SpendCeilingReached) as exc:
        ops.charge(db, user_id=uid, operation=ops.GENERATION, budget=budget,
                   note="over the line")
    assert "ceiling of 3" in str(exc.value)

    status, body = live["request"]("GET", "/ops/spend", live["operator"])
    assert status == 200, body
    assert body["by_operation"][ops.GENERATION] == 3, (
        f"the refused call was written to the spend log: {body}")


def test_the_ceiling_is_reloaded_from_history_not_from_memory(live):
    """A fresh process must not hand a spent account a fresh allowance.

    `spend_guard` reads what this account has already spent in the period out
    of `spend_log`. If it did not, restarting the server -- or simply building
    a second guard -- would reset every ceiling, which is the whole failure the
    ceiling exists to prevent for a runaway loop.
    """
    db, uid = live["db"], live["learner_id"]
    first = ops.spend_guard(db, user_id=uid, max_calls=2, operation=ops.INGESTION)
    ops.charge(db, user_id=uid, operation=ops.INGESTION, budget=first, note="one")
    ops.charge(db, user_id=uid, operation=ops.INGESTION, budget=first, note="two")

    second = ops.spend_guard(db, user_id=uid, max_calls=2, operation=ops.INGESTION)
    with pytest.raises(ops.SpendCeilingReached):
        ops.charge(db, user_id=uid, operation=ops.INGESTION, budget=second, note="three")


def test_the_ceiling_is_per_account(live):
    """One learner's runaway loop must not spend another learner's allowance."""
    db = live["db"]
    other = db.query_one("SELECT id FROM users WHERE email = ?",
                         ("ops@example.com",))["id"]
    mine = ops.spend_guard(db, user_id=live["learner_id"], max_calls=1,
                           operation=ops.GENERATION)
    ops.charge(db, user_id=live["learner_id"], operation=ops.GENERATION, budget=mine)

    theirs = ops.spend_guard(db, user_id=other, max_calls=1, operation=ops.GENERATION)
    ops.charge(db, user_id=other, operation=ops.GENERATION, budget=theirs)  # must not raise


# ---------------------------------------------------------------------------
# Who can see any of it
# ---------------------------------------------------------------------------

@pytest.mark.xfail(strict=True, reason=(
    "OPEN DISCLOSURE ROUTE, found 2026-09-17, NOT YET FIXED. Held for the "
    "owner under the standing stop condition: a new disclosure route is "
    "reported before it is fixed. The status codes were harmonised to 404 and "
    "the BODIES were not, so an authenticated learner can enumerate the whole "
    "operator surface by comparing error strings. `strict=True` means this "
    "flips to a failure the moment the fix lands, so it cannot be forgotten. "
    "See ADR-028."))
@pytest.mark.parametrize("path", ["/ops/incidents", "/ops/alerts", "/ops/spend"])
def test_an_ops_route_is_indistinguishable_from_one_that_does_not_exist(live, path):
    """The property is INDISTINGUISHABILITY, not a particular status code.

    Item P of the manual test plan records the intent as "404, not 403", and
    `_require_admin` does raise 404. But an anonymous caller never reaches
    `_require_admin`: `_user()` rejects a missing token first, with 401. So the
    literal claim "these routes answer 404" is wrong for an anonymous caller,
    and an earlier version of this test asserted it and failed.

    That failure was worth chasing rather than editing away, because the
    question underneath it is a real one: can someone who cannot use these
    routes still learn that they EXIST? A route answering 401 while a typo
    answers 404 would be exactly that -- an anonymous prober could enumerate
    the operator surface without holding a single credential.

    It does not. Both answer 401 to an anonymous caller and both answer 404 to
    a learner, so each ops path is compared against a control path that
    certainly does not exist and must be byte-identical to it. Asserting the
    comparison rather than the code means this keeps holding if the auth layer
    ever changes which code it uses, and starts failing the moment the two
    diverge.
    """
    ops.record(live["db"], operation=ops.INGESTION, error=ValueError("something"))
    control = "/definitely-not-a-route/" + path.strip("/").replace("/", "-")

    for label, token in (("anonymous", None), ("a learner", live["learner"])):
        got_status, got_body = live["request"]("GET", path, token)
        ctl_status, ctl_body = live["request"]("GET", control, token)
        assert (got_status, got_body) == (ctl_status, ctl_body), (
            f"to {label}, {path} answered {got_status} {got_body} while a "
            f"nonexistent path answered {ctl_status} {ctl_body}. The operator "
            "surface is enumerable by anyone who can send a request.")

    admin_status, _ = live["request"]("GET", path, live["operator"])
    assert admin_status == 200, (
        f"{path} answered {admin_status} to an admin, so the comparisons above "
        "may be passing because the route is simply broken")


# ---------------------------------------------------------------------------
# The engine actually writes incidents
# ---------------------------------------------------------------------------

def test_a_failed_ingestion_writes_an_incident_an_operator_can_read(tmp_path):
    """The routes above are only worth having if something populates them.

    `student/operations.py` was built, tested and never called: every ingestion
    failure was recorded on the SOURCE row, where only the learner who uploaded
    it would see it, so `/ops/incidents` was permanently empty in production and
    `/ops/alerts` could not fire however badly ingestion was failing.

    This drives a real failure through `IngestionEngine.process_source` and then
    reads the incident back, because "the function exists" is what was true
    before and it was not enough.
    """
    from student.ingestion import IngestionEngine
    from student.db import Database as DB

    db = DB(tmp_path / "ing.db")
    from student.api import StudentAPI
    api = StudentAPI(db)
    token = api.handle("POST", "/auth/register", {},
                       {"email": "up@example.com", "password": "correct-horse"},
                       None)[1]["token"]
    uid = db.query_one("SELECT id FROM users WHERE email=?", ("up@example.com",))["id"]
    nid = api.handle("POST", "/notebooks", {}, {"title": "N", "subject": "Med"}, token)[1]["id"]

    sid = new_id_local = "src_broken"
    db.execute("INSERT INTO sources (id,notebook_id,kind,filename,status,uploaded_at)"
               " VALUES (?,?,?,?,?,?)",
               (sid, nid, "text", "empty.txt", "uploaded", "2026-09-17T00:00:00Z"))

    engine = IngestionEngine(db, storage_dir=tmp_path / "uploads")
    # Empty text: `chunk_pages` yields nothing and the engine raises
    # ExtractionUnavailable, which is an ordinary, handled failure -- exactly
    # the kind that was previously invisible to an operator.
    engine.process_source(sid, raw_text="")

    assert db.query_one("SELECT status FROM sources WHERE id=?", (sid,))["status"] == "failed"

    report = ops.since(db, hours=1)
    assert report["total"] >= 1, (
        "the ingestion failed and no incident was written; /ops/incidents is "
        "still a table nothing populates")
    faults = {(f["operation"], f["error_type"]) for f in report["by_fault"]}
    assert any(op == ops.INGESTION for op, _ in faults), report

    row = db.query_one("SELECT user_id, context_json FROM incidents LIMIT 1")
    assert row["user_id"] == uid, "the incident does not name the affected account"
    context = json.loads(row["context_json"])
    assert context["source_id"] == sid
    assert context["filename"] == "empty.txt"
    # The FILENAME, never the contents. An incident is read by an operator who
    # is not this learner.
    assert "text" not in context or context.get("text") is None
