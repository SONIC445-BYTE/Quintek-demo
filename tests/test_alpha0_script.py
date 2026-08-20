"""
Smoke tests for the Alpha-0 acceptance script.

The script's job is to be run against a real model, which cannot happen in a
test. What CAN be tested is that it runs to completion, refuses the things it
should refuse, and reports honestly when handed a test double -- because an
acceptance script that crashes, or that flatters a scripted provider, is worse
than no acceptance script.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools_alpha0.py"


def run(args, cwd=None):
    return subprocess.run([sys.executable, str(SCRIPT)] + args, capture_output=True,
                          text=True, cwd=cwd or ROOT, timeout=180)


def test_it_refuses_when_the_validator_is_the_generator(tmp_path):
    """An approval from the model that wrote the question means nothing."""
    result = run(["--provider", "scripted", "--out", str(tmp_path)])
    assert result.returncode == 2
    assert "must differ from the generator" in result.stdout


def test_it_runs_to_completion_with_distinct_scripted_candidates(tmp_path):
    result = run(["--provider", "scripted", "--generator", "scripted/gen",
                  "--validator", "scripted/val", "--out", str(tmp_path)])
    assert "QUINTEK ALPHA-0 ACCEPTANCE" in result.stdout, result.stdout + result.stderr
    # Not all criteria pass on a test double, and that is the point.
    assert result.returncode == 1


def test_it_does_not_credit_a_scripted_provider_as_a_real_model(tmp_path):
    result = run(["--provider", "scripted", "--generator", "scripted/gen",
                  "--validator", "scripted/val", "--out", str(tmp_path)])
    reports = list(Path(tmp_path).glob("*/alpha0_report.json"))
    assert reports, result.stdout
    report = json.loads(reports[0].read_text())

    by_name = {c["criterion"]: c for c in report["criteria"]}
    assert by_name["Real model used"]["passed"] is False
    assert "not a real model" in by_name["Real model used"]["detail"]


def test_the_development_override_criterion_cannot_pass_without_a_benchmark_run(tmp_path):
    """
    Left failing rather than redefined: the promotion gate refusing to promote
    an unbenchmarked model is the gate working.
    """
    run(["--provider", "scripted", "--generator", "scripted/gen",
         "--validator", "scripted/val", "--out", str(tmp_path)])
    report = json.loads(next(Path(tmp_path).glob("*/alpha0_report.json")).read_text())
    criterion = next(c for c in report["criteria"] if c["criterion"] == "No development_override")
    assert criterion["passed"] is False
    assert "no candidate has a passing benchmark run" in criterion["detail"]


def test_it_refuses_a_provider_it_cannot_build(tmp_path, monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--provider", "nvidia",
         "--generator", "meta/llama-3.1-70b-instruct",
         "--validator", "meta/llama-3.1-8b-instruct", "--out", str(tmp_path)],
        capture_output=True, text=True, cwd=ROOT, timeout=120,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    assert result.returncode == 2
    assert "NVIDIA_API_KEY" in result.stdout


def test_every_report_records_which_provider_produced_it(tmp_path):
    run(["--provider", "scripted", "--generator", "scripted/gen",
         "--validator", "scripted/val", "--out", str(tmp_path)])
    report = json.loads(next(Path(tmp_path).glob("*/alpha0_report.json")).read_text())
    assert report["generator"]["provider"] == "scripted"
    assert report["generator"]["is_real_model"] is False
    assert report["validator"]["model_id"] == "scripted/val"
