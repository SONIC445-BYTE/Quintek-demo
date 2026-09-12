"""
The arms share observations, and the artifact says so.

THE DEFECT THIS CLOSES
----------------------
A full experiment set issues 765 calls, and 380 of them were exact repeats --
measured by building every request the three arms send and hashing it the way
`journal.key_for` does. ABD and ABCD issue byte-identical grounding (190),
conformance (95) and judge (95) calls: same item, same system prompt, same
prompt, same max_tokens, same temperature, same freeze.

The expense was the smaller half. The ablation reports the judge's
contribution as ABCD minus ABD; an arm decides 30-40 items, so ONE item
flipping is a three per cent swing, and providers are not bit-deterministic at
temperature zero. Redrawing grounding and conformance for the second arm put
sampling noise the same size as the effect inside the difference the
experiment exists to measure.

`arm` was the only thing keeping those requests apart, and the arm is never
sent to a model.

WHY DISCLOSURE IS PART OF THE FIX AND NOT A NICETY
---------------------------------------------------
Sharing makes ABD and ABCD non-independent. A reader who assumed otherwise
would compute a between-arm variance that no longer exists, so an artifact
that stayed silent would have traded a known confound for a hidden one. The
`shared_observations` block is therefore asserted here as hard as the saving.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from benchmark.journal import FORFEITED_BY_DESIGN, Journal, JournalledProvider, key_for
from benchmark.providers.base import GenerationRequest, GenerationResponse
from validator import pipeline, runs
from tools_validator_eval import main

FREEZE = "freeze-digest-for-these-tests"


class Counting:
    name, model, model_version, model_family = "double", "m", "v0", "none"
    is_model, is_oracle = False, False

    def __init__(self):
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        return GenerationResponse(
            item_id=request.item_id, raw_output='{"ok": true}', parsed=None,
            provider=self.name, model=self.model, model_version=self.model_version,
            latency_ms=0.0, attempts=1)


def request(item_id="it-1", purpose="key"):
    return GenerationRequest(item_id=f"{item_id}:{purpose}", prompt="p", system="s",
                             max_tokens=4096, temperature=0.0)


def wrap(inner, book, arm):
    return JournalledProvider(inner, book, arm=arm, role="candidate")


class TestTheSaving:

    def test_a_second_arm_does_not_re_ask(self, tmp_path):
        book = Journal.open(tmp_path / "j.jsonl", FREEZE)
        inner = Counting()
        wrap(inner, book, "ABD").generate(request())
        wrap(inner, book, "ABCD").generate(request())
        assert inner.calls == 1

    def test_the_arm_cannot_be_smuggled_back_into_the_key(self):
        import inspect
        assert "arm" not in inspect.signature(key_for).parameters

    def test_a_genuinely_different_question_still_costs(self, tmp_path):
        """The saving must not become a cache that answers questions nobody asked."""
        book = Journal.open(tmp_path / "j.jsonl", FREEZE)
        inner = Counting()
        wrap(inner, book, "ABD").generate(request(item_id="it-1"))
        wrap(inner, book, "ABCD").generate(request(item_id="it-2"))
        wrap(inner, book, "ABCD").generate(request(item_id="it-1", purpose="explanation"))
        assert inner.calls == 3

    def test_a_different_freeze_is_refused_rather_than_shared(self, tmp_path):
        """
        The property that makes cross-arm sharing sound: a reply may only be
        reused under the configuration that produced it.
        """
        from benchmark.journal import JournalMismatch
        path = tmp_path / "j.jsonl"
        wrap(Counting(), Journal.open(path, FREEZE), "ABD").generate(request())
        with pytest.raises(JournalMismatch):
            Journal.open(path, "a-different-freeze")


class TestTheDisclosure:

    def test_the_block_names_who_reused_whose_reply(self, tmp_path):
        book = Journal.open(tmp_path / "j.jsonl", FREEZE)
        inner = Counting()
        wrap(inner, book, "ABD").generate(request(item_id="a"))
        wrap(inner, book, "ABD").generate(request(item_id="b"))
        wrap(inner, book, "ABCD").generate(request(item_id="a"))
        wrap(inner, book, "ABCD").generate(request(item_id="b"))

        shared = book.shared_observations()
        assert shared["arms_share_observations"] is True
        assert shared["distinct_questions"] == 2
        assert shared["by_arm"]["ABD"]["asked"] == 2
        assert shared["by_arm"]["ABCD"]["replayed"] == 2
        assert shared["by_arm"]["ABCD"]["replayed_from"] == {"ABD": 2}

    def test_it_carries_the_forfeited_capability(self, tmp_path):
        book = Journal.open(tmp_path / "j.jsonl", FREEZE)
        wrap(Counting(), book, "ABD").generate(request())
        assert book.shared_observations()["forfeited"] == FORFEITED_BY_DESIGN

    def test_it_survives_the_round_trip_through_the_run_record(self, tmp_path):
        book = Journal.open(tmp_path / "j.jsonl", FREEZE)
        inner = Counting()
        wrap(inner, book, "ABD").generate(request())
        wrap(inner, book, "ABCD").generate(request())

        run = runs.Run(
            at=runs.now(), kind="development", corpus="c", corpus_hash="h",
            validator_version=pipeline.VALIDATOR_VERSION, config="v0.2.0[ABCD]",
            providers=[], counts={}, shared_observations=book.shared_observations())
        path = pathlib.Path(runs.record(run, runs_dir=tmp_path))

        raw = json.loads(path.read_text())
        assert raw["shared_observations"]["arms_share_observations"] is True
        assert raw["shared_observations"]["by_arm"]["ABCD"]["replayed"] == 1
        [reloaded] = runs.load_all(tmp_path)
        assert reloaded.shared_observations["distinct_questions"] == 1

    def test_the_reader_warns_that_the_arms_are_not_replications(self, tmp_path, capsys):
        book = Journal.open(tmp_path / "j.jsonl", FREEZE)
        inner = Counting()
        wrap(inner, book, "ABD").generate(request())
        wrap(inner, book, "ABCD").generate(request())
        runs.record(runs.Run(
            at=runs.now(), kind="development", corpus="c", corpus_hash="h",
            validator_version=pipeline.VALIDATOR_VERSION, config="v0.2.0[ABCD]",
            providers=[], counts={}, outages=0, items_expected=1, items_decided=1,
            completeness="COMPLETE",
            shared_observations=book.shared_observations()), runs_dir=tmp_path)

        main(["--runs-dir", str(tmp_path), "outages"])
        out = capsys.readouterr().out
        assert "arms SHARE observations" in out
        assert "NOT independent replications" in out, (
            "a reader who takes ABD and ABCD for replications computes a variance "
            "that does not exist; the artifact must say so where it is read.")

    def test_a_run_with_no_journal_records_no_sharing(self, tmp_path):
        run = runs.Run(
            at=runs.now(), kind="development", corpus="c", corpus_hash="h",
            validator_version=pipeline.VALIDATOR_VERSION, config="v0.2.0[ABD]",
            providers=[], counts={})
        raw = json.loads(pathlib.Path(runs.record(run, runs_dir=tmp_path)).read_text())
        assert raw["shared_observations"] == {}

    def test_the_experiments_command_passes_the_block(self):
        """
        A field the record has and the caller never fills is a field that is
        always empty in real artifacts -- the same defect the pacing stats had.
        """
        import inspect
        from tools_validator_eval import _record
        assert "shared_observations" in inspect.signature(_record).parameters
        source = pathlib.Path("tools_validator_eval.py").read_text()
        assert "shared_observations=(book.shared_observations()" in source
