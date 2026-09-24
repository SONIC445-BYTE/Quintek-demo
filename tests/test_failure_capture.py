"""
The failure log (tests/failure_capture.py) records what a failure needs to be
diagnosed after the terminal is gone: which test, which phase, the full
traceback, the captured output, when, and against which backend and commit --
plus a line per run, so "did not reproduce in N runs" has an N.

Driven through a real pytest subprocess on a throwaway test file, because the
hooks only mean anything inside pytest's own reporting.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

PROBE = '''
import pytest

def test_passes():
    assert True

def test_fails():
    print("captured stdout MARKER-OUT")
    assert 1 + 1 == 3, "MARKER-ASSERTION"

@pytest.fixture
def broken():
    raise RuntimeError("MARKER-SETUP")

def test_errors_in_setup(broken):
    pass

def test_skips():
    pytest.skip("not today")

@pytest.mark.xfail(strict=True)
def test_expected_failure():
    assert False
'''


def run_probe(tmp_path, log):
    (tmp_path / "test_probe.py").write_text(PROBE)
    env = dict(os.environ, **{"QUINTEK_TEST_FAILURE_LOG": str(log),
                              "PYTHONPATH": str(HERE)})
    env.pop("QUINTEK_TEST_POSTGRES_URL", None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "failure_capture",
         "--rootdir", str(tmp_path), str(tmp_path / "test_probe.py")],
        cwd=tmp_path, env=env, capture_output=True, text=True)


def records(log):
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_a_failure_is_written_with_everything_needed_to_diagnose_it(tmp_path):
    log = tmp_path / "failures.jsonl"
    result = run_probe(tmp_path, log)
    assert result.returncode == 1, result.stdout
    failures = [r for r in records(log) if r["kind"] == "failure"]
    by = {(r["nodeid"].split("::")[-1], r["when"]): r for r in failures}
    assert set(by) == {("test_fails", "call"), ("test_errors_in_setup", "setup")}

    fail = by[("test_fails", "call")]
    assert "MARKER-ASSERTION" in fail["traceback"]
    assert "test_probe.py" in fail["traceback"], "not a full traceback"
    assert any("MARKER-OUT" in s["text"] for s in fail["sections"]), "stdout was not kept"
    assert fail["backend"] == "sqlite" and fail["at"].endswith("Z")
    assert "MARKER-SETUP" in by[("test_errors_in_setup", "setup")]["traceback"]


def test_every_run_leaves_a_summary_line_with_its_counts(tmp_path):
    log = tmp_path / "failures.jsonl"
    run_probe(tmp_path, log)
    run_probe(tmp_path, log)                      # appended, not overwritten
    runs = [r for r in records(log) if r["kind"] == "run"]
    assert len(runs) == 2 and runs[0]["run_id"] != runs[1]["run_id"]
    assert runs[0]["counts"] == {"passed": 1, "failed": 1, "error": 1, "skipped": 1,
                                 "xfailed": 1}
    assert runs[0]["exitstatus"] == 1
    failures = [r for r in records(log) if r["kind"] == "failure"]
    assert {f["run_id"] for f in failures} == {runs[0]["run_id"], runs[1]["run_id"]}


def test_the_default_log_is_gitignored():
    ignored = subprocess.run(["git", "check-ignore", ".test-failures/failures.jsonl"],
                             cwd=HERE.parent, capture_output=True, text=True)
    assert ignored.returncode == 0, "the failure log could be committed"
