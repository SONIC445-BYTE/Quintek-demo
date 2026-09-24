"""
Every test failure, written somewhere that outlives the terminal.

WHY THIS EXISTS
---------------
On 2026-09-24 one full PostgreSQL run of the suite reported `1 failed` and
four later runs were clean. The summary line was all that was kept, so which
test failed, and why, was lost -- and an intermittent failure that cannot be
identified cannot be fixed or even honestly called fixed.

WHAT IT WRITES
--------------
One JSON line per failing phase (setup, call or teardown) and one summary
line per run, appended to `QUINTEK_TEST_FAILURE_LOG` (default
`.test-failures/failures.jsonl` at the repository root, which is gitignored):

  {"kind": "failure", "run_id", "at", "nodeid", "when", "duration",
   "traceback", "sections", "backend", "commit", "pid"}
  {"kind": "run", "run_id", "started", "finished", "exitstatus", "counts",
   "backend", "commit"}

The run lines matter as much as the failure lines: "failed once in N runs"
needs N, and a hunt that only records failures cannot say how many clean runs
stand behind "did not reproduce".

Each line is flushed and fsynced on its own, so a run that is killed part-way
still leaves every failure it had already seen.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path

ENV = "QUINTEK_TEST_FAILURE_LOG"
ROOT = Path(__file__).resolve().parent.parent
DEFAULT = ROOT / ".test-failures" / "failures.jsonl"

_RUN = {"run_id": uuid.uuid4().hex[:12], "started": None, "counts": {}}


def _path() -> Path:
    return Path(os.environ.get(ENV) or DEFAULT)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return ""


def _backend() -> str:
    return "postgres" if (os.environ.get("QUINTEK_TEST_POSTGRES_URL") or "").strip() else "sqlite"


def _write(record: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def pytest_sessionstart(session):
    _RUN["started"] = _now()
    _RUN["commit"] = _commit()


def pytest_runtest_logreport(report):
    outcome = report.outcome
    # One count per test: its call phase, or the setup/teardown phase that
    # stopped it (a setup skip or error never reaches call).
    if report.when == "call" or outcome != "passed":
        key = "xfailed" if hasattr(report, "wasxfail") and outcome == "skipped" else outcome
        if report.when != "call" and outcome == "failed":
            key = "error"
        _RUN["counts"][key] = _RUN["counts"].get(key, 0) + 1
    if outcome != "failed":
        return
    _write({
        "kind": "failure", "run_id": _RUN["run_id"], "at": _now(),
        "nodeid": report.nodeid, "when": report.when,
        "duration": round(getattr(report, "duration", 0.0), 3),
        "traceback": report.longreprtext,
        "sections": [{"name": n, "text": t[-20000:]} for n, t in report.sections],
        "backend": _backend(), "commit": _RUN.get("commit", ""), "pid": os.getpid(),
    })


def pytest_sessionfinish(session, exitstatus):
    _write({
        "kind": "run", "run_id": _RUN["run_id"], "started": _RUN["started"],
        "finished": _now(), "exitstatus": int(exitstatus), "counts": _RUN["counts"],
        "backend": _backend(), "commit": _RUN.get("commit", ""),
    })
