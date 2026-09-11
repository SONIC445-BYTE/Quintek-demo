"""
Evidence that is written but unreadable is most of the way to evidence that
was never written.

TWO HOLES, THE SAME SHAPE
-------------------------
Phase A made the per-item outage record durable so a run could be diagnosed
from its artifact alone. Two things then kept that promise from paying out.

The first: `RateLimiter` counts every wait it performs and the seconds it
spent, `tools_validator_eval` built `limiter.as_dict()` to print a line at
startup, and nothing ever wrote it down. So when the first real Groq run took
30.9 minutes and died on its wall-clock ceiling, the split between "time spent
pacing" and "time spent in 429 backoff" -- the number that decides what to
change -- could only be INFERRED from the wall clock and the outage counts.

The second: nothing read `outage_detail` back. The records were in the file
and diagnosis still meant hand-poking JSON.

These tests assert both: the pacing stats reach the artifact, and the reader
prints what is actually in it -- including refusing to call an artifact clean
when it merely has no detail to show.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from benchmark.providers.pacing import RateLimiter
from validator import outage, pipeline, runs
from validator.outage import (MODE_RATE_LIMITED, MODE_TRANSPORT, MODE_WALL_CLOCK,
                              LayerUnavailable)
from tools_validator_eval import main


def groq_shaped_outages():
    """
    The first real Groq run: 33 lost items, 24 to the wall-clock ceiling and
    9 to our own pace. Not one of them was the host.
    """
    records = []
    for n in range(24):
        exc = LayerUnavailable(
            "the wall-clock ceiling was reached", mode=MODE_WALL_CLOCK,
            item_id=f"dev-{n:03d}", purpose="key", attempts=1,
            provider_error="WallClockExceeded: the wall-clock ceiling of 30 minute(s)")
        exc.layer = "grounding"
        records.append(exc.as_dict())
    for n in range(9):
        exc = LayerUnavailable(
            "the host refused our pace", mode=MODE_RATE_LIMITED,
            item_id=f"dev-1{n:02d}", purpose="explanation", attempts=3,
            provider_error="RateLimited: groq (api.groq.com) HTTP 429 rate limited")
        exc.layer = "grounding"
        records.append(exc.as_dict())
    return records


def write_run(tmp_path, *, outages, pacing=None, decided=62, expected=95,
              config="v0.2.0[ABD]"):
    # `runs.record` names the file `{timestamp}_{kind}_{config}.json`, so two
    # artifacts written in the same second under the same config label are one
    # artifact. Tests that need two must differ in the label, as the real
    # experiment arms do.
    run = runs.Run(
        at=runs.now(), kind="development", corpus="corpus/validator_dev",
        corpus_hash="h", validator_version=pipeline.VALIDATOR_VERSION,
        config=config, providers=[], counts={},
        outages=len(outages), outage_detail=list(outages),
        outage_summary=outage.summarise(outages),
        pacing=dict(pacing or {}), items_expected=expected, items_decided=decided,
        completeness="INCOMPLETE")
    return pathlib.Path(runs.record(run, runs_dir=tmp_path))


class TestPacingIsPersisted:

    def test_the_limiter_stats_reach_the_artifact(self, tmp_path):
        limiter = RateLimiter(30, safety_factor=0.5,
                              clock=lambda: 0.0, sleep=lambda _s: None)
        limiter.acquire()
        limiter.acquire()          # the second one waits: the interval is 4s
        path = write_run(tmp_path, outages=[], pacing=limiter.as_dict())

        raw = json.loads(path.read_text())
        assert raw["pacing"]["requests_per_minute"] == 30
        assert raw["pacing"]["safety_factor"] == 0.5
        assert raw["pacing"]["effective_rpm"] == 15.0
        assert raw["pacing"]["min_interval_seconds"] == 4.0
        assert raw["pacing"]["waits"] == 1, (
            "the wait count is the whole point: without it, pacing time and 429 "
            "backoff time cannot be told apart after the run.")
        assert raw["pacing"]["waited_seconds"] == 4.0

    def test_it_survives_the_round_trip_back_into_a_Run(self, tmp_path):
        write_run(tmp_path, outages=[], pacing={"effective_rpm": 15.0, "waits": 7})
        [reloaded] = runs.load_all(tmp_path)
        assert reloaded.pacing["waits"] == 7

    def test_no_limiter_is_recorded_as_no_limiter_not_as_absent(self, tmp_path):
        """An empty dict is a fact -- this run was not paced -- and must round-trip."""
        path = write_run(tmp_path, outages=[])
        assert json.loads(path.read_text())["pacing"] == {}
        [reloaded] = runs.load_all(tmp_path)
        assert reloaded.pacing == {}

    def test_the_record_function_threads_it_through(self):
        """
        `_record` is what the experiments command actually calls. A field the
        dataclass has and the caller never passes is a field that is always
        empty in real artifacts.
        """
        import inspect
        from tools_validator_eval import _record
        assert "pacing_stats" in inspect.signature(_record).parameters
        source = pathlib.Path("tools_validator_eval.py").read_text()
        assert "pacing_stats=(limiter.as_dict() if limiter is not None else {})" in source, (
            "run_experiments must hand the live limiter to _record; a default of {} "
            "would record 'not paced' for every paced run.")


class TestTheOutageReader:

    def test_it_reports_the_run_as_self_inflicted(self, tmp_path, capsys):
        write_run(tmp_path, outages=groq_shaped_outages(),
                  pacing=RateLimiter(30, safety_factor=0.8).as_dict())
        assert main(["--runs-dir", str(tmp_path), "outages"]) == 0
        out = capsys.readouterr().out

        assert "OUTAGES  33" in out
        assert "the run's own doing        33" in out
        assert "attributable to the host   0" in out, (
            "every one of these was ours. A reader that leaves a host to blame here "
            "is the defect this command exists to close.")
        assert "grounding/wall_clock" in out
        assert "grounding/transport" not in out

    def test_it_prints_the_pacing_that_produced_the_run(self, tmp_path, capsys):
        write_run(tmp_path, outages=groq_shaped_outages(),
                  pacing=RateLimiter(30, safety_factor=0.5).as_dict())
        main(["--runs-dir", str(tmp_path), "outages"])
        out = capsys.readouterr().out
        assert "15.0 effective rpm" in out
        assert "4.0s apart" in out

    def test_it_says_so_when_nothing_paced_the_run(self, tmp_path, capsys):
        write_run(tmp_path, outages=groq_shaped_outages())
        main(["--runs-dir", str(tmp_path), "outages"])
        assert "NO LIMITER CONFIGURED" in capsys.readouterr().out

    @pytest.mark.parametrize("flag, value, kept", [
        ("--mode", "wall_clock", 24),
        ("--mode", "rate_limited", 9),
        ("--layer", "grounding", 33),
    ])
    def test_filters(self, tmp_path, capsys, flag, value, kept):
        write_run(tmp_path, outages=groq_shaped_outages())
        main(["--runs-dir", str(tmp_path), "outages", flag, value])
        assert f"OUTAGES  {kept}" in capsys.readouterr().out

    def test_a_filter_that_matches_nothing_says_so(self, tmp_path, capsys):
        write_run(tmp_path, outages=groq_shaped_outages())
        main(["--runs-dir", str(tmp_path), "outages", "--mode", "unparseable"])
        assert "no outages match that filter" in capsys.readouterr().out

    def test_json_output_carries_the_records(self, tmp_path, capsys):
        write_run(tmp_path, outages=groq_shaped_outages())
        main(["--runs-dir", str(tmp_path), "outages", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["summary"]["stopped_by_run_ceiling"] == 24
        assert len(payload["outages"]) == 33
        assert payload["outages"][0]["provider_error"].startswith("WallClockExceeded")

    def test_replies_are_shown_only_when_asked(self, tmp_path, capsys):
        exc = LayerUnavailable("no JSON in that", mode="unparseable", item_id="dev-1",
                               purpose="key", attempts=1,
                               raw_reply="I had a look and it seems fine to me.")
        exc.layer = "grounding"
        write_run(tmp_path, outages=[exc.as_dict()])

        main(["--runs-dir", str(tmp_path), "outages"])
        assert "seems fine to me" not in capsys.readouterr().out
        main(["--runs-dir", str(tmp_path), "outages", "--show-replies"])
        assert "seems fine to me" in capsys.readouterr().out

    def test_an_artifact_with_a_count_but_no_detail_is_not_called_clean(
            self, tmp_path, capsys):
        """
        D018's artifact: `outages: 14`, no detail. The honest answer is that
        the cause is unrecoverable, NOT that every item reached a verdict.
        """
        run = runs.Run(
            at=runs.now(), kind="development", corpus="x", corpus_hash="h",
            validator_version=pipeline.VALIDATOR_VERSION, config="v0.2.0[ABCD]",
            providers=[], counts={}, outages=14, outage_detail=[],
            items_expected=80, items_decided=69, completeness="INCOMPLETE")
        runs.record(run, runs_dir=tmp_path)

        assert main(["--runs-dir", str(tmp_path), "outages"]) == 1
        out = capsys.readouterr().out
        assert "NO per-item detail" in out
        assert "every item reached a verdict" not in out

    def test_a_genuinely_clean_run_is_called_clean(self, tmp_path, capsys):
        run = runs.Run(
            at=runs.now(), kind="development", corpus="x", corpus_hash="h",
            validator_version=pipeline.VALIDATOR_VERSION, config="v0.2.0[ABD]",
            providers=[], counts={}, outages=0, outage_detail=[],
            items_expected=95, items_decided=95, completeness="COMPLETE")
        runs.record(run, runs_dir=tmp_path)
        assert main(["--runs-dir", str(tmp_path), "outages"]) == 0
        assert "every item reached a verdict" in capsys.readouterr().out

    def test_a_named_run_can_be_read_instead_of_the_newest(self, tmp_path, capsys):
        older = write_run(tmp_path, outages=groq_shaped_outages(), config="v0.2.0[ABD]")
        write_run(tmp_path, outages=[], config="v0.2.0[C]")   # a second, clean arm
        main(["--runs-dir", str(tmp_path), "outages", "--run", str(older)])
        assert "OUTAGES  33" in capsys.readouterr().out

    def test_a_missing_run_is_refused_not_guessed_at(self, tmp_path, capsys):
        assert main(["--runs-dir", str(tmp_path), "outages",
                     "--run", str(tmp_path / "nope.json")]) == 2
