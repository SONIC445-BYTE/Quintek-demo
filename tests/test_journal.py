"""
The resume, and the four ways it could quietly become a different experiment.

WHY THIS FILE IS ADVERSARIAL RATHER THAN HAPPY-PATH
---------------------------------------------------
A journal that replays replies is a convenience. A journal that replays the
successes and re-asks the failures is scientific fraud with good intentions:
it turns an outage rate into whatever the operator has patience for. The same
goes for serving one arm's answer to another arm, and for letting each restart
reissue the full frozen budget.

So most of what follows tests the refusals, not the caching.
"""

from __future__ import annotations

import json

import pytest

from benchmark.journal import (Journal, JournalMismatch, JournalledProvider,
                               JOURNAL_VERSION, key_for)
from benchmark.providers.base import GenerationRequest, GenerationResponse

FREEZE = "c2955816b918bb5d"
OTHER_FREEZE = "3bca900de60ecafe"


class CountingProvider:
    """Records how many times it was actually asked."""

    name = "counting"
    model = "test-model"
    model_version = "1.0"
    model_family = "test"
    is_model = True
    is_oracle = False

    def __init__(self, *, error: str | None = None):
        self.calls = 0
        self.error = error

    def generate(self, request):
        self.calls += 1
        return GenerationResponse(
            item_id=request.item_id, raw_output=f"reply-{self.calls}",
            parsed={"n": self.calls}, provider=self.name, model=self.model,
            model_version=self.model_version, latency_ms=12.5,
            input_tokens=7, output_tokens=9, error=self.error, attempts=1)


def req(item_id="item-1", prompt="is this grounded?"):
    return GenerationRequest(item_id=item_id, prompt=prompt, system="sys",
                             max_tokens=1024, temperature=0.0)


def wrap(inner, book, *, arm="ABD", role="grounding", budget=None, clock=None):
    return JournalledProvider(inner, book, arm=arm, role=role,
                              budget=budget, clock=clock)


# ---------------------------------------------------------------------------
# It remembers
# ---------------------------------------------------------------------------

def test_a_recorded_reply_is_replayed_without_asking_again(tmp_path):
    path = tmp_path / "j.jsonl"
    inner = CountingProvider()
    first = wrap(inner, Journal.open(path, FREEZE)).generate(req())
    assert inner.calls == 1

    # A fresh process, same journal.
    second = wrap(inner, Journal.open(path, FREEZE)).generate(req())
    assert inner.calls == 1, "the model was asked a question already on disk"
    assert second.raw_output == first.raw_output
    assert second.parsed == first.parsed
    assert second.attempts == first.attempts


def test_the_reply_survives_a_kill_because_it_is_fsynced_per_call(tmp_path):
    """
    Written line by line, not at the end. The whole point is the run that never
    reaches its end.
    """
    path = tmp_path / "j.jsonl"
    book = Journal.open(path, FREEZE)
    provider = wrap(CountingProvider(), book)
    for n in range(3):
        provider.generate(req(item_id=f"item-{n}"))
        # readable from disk immediately, mid-run
        assert len(path.read_text().strip().splitlines()) == n + 1


# ---------------------------------------------------------------------------
# It does not selectively retry
# ---------------------------------------------------------------------------

def test_a_recorded_outage_replays_as_that_outage_and_is_never_re_asked(tmp_path):
    """
    THE integrity test of this module.

    Re-asking only the items that failed, and keeping the answer if it comes
    back, is how a 71-outage arm becomes a clean one without anybody deciding
    to cheat. A recorded failure is an observation about this run.
    """
    path = tmp_path / "j.jsonl"
    sick = CountingProvider(error="HTTP 500: internal server error")
    recorded = wrap(sick, Journal.open(path, FREEZE)).generate(req())
    assert recorded.error and not recorded.ok
    assert sick.calls == 1

    # Resume. The provider is now healthy -- and must not be consulted.
    healed = CountingProvider()
    replayed = wrap(healed, Journal.open(path, FREEZE)).generate(req())
    assert healed.calls == 0, "a recorded outage was re-asked; that is selective retry"
    assert replayed.error == recorded.error
    assert replayed.ok is False


# ---------------------------------------------------------------------------
# It does not blur the arms together
# ---------------------------------------------------------------------------

def test_each_arm_pays_for_its_own_calls(tmp_path):
    """
    A byte-identical request in another arm is a miss. Sharing would strip
    between-arm sampling variation out of ABCD - ABD, which is a quieter
    experiment than the frozen one.
    """
    path = tmp_path / "j.jsonl"
    inner = CountingProvider()
    wrap(inner, Journal.open(path, FREEZE), arm="ABD").generate(req())
    wrap(inner, Journal.open(path, FREEZE), arm="ABCD").generate(req())
    assert inner.calls == 2

    # ...and each arm still replays its own.
    wrap(inner, Journal.open(path, FREEZE), arm="ABD").generate(req())
    assert inner.calls == 2


def test_a_different_role_on_the_same_model_is_a_different_question(tmp_path):
    path = tmp_path / "j.jsonl"
    inner = CountingProvider()
    wrap(inner, Journal.open(path, FREEZE), role="grounding").generate(req())
    wrap(inner, Journal.open(path, FREEZE), role="conformance").generate(req())
    assert inner.calls == 2


def test_a_drifted_prompt_misses_and_is_paid_for(tmp_path):
    """
    Every input that could change the reply is in the key, so a cached answer
    can never be served to a question nobody asked.
    """
    path = tmp_path / "j.jsonl"
    inner = CountingProvider()
    wrap(inner, Journal.open(path, FREEZE)).generate(req(prompt="original"))
    wrap(inner, Journal.open(path, FREEZE)).generate(req(prompt="reworded"))
    assert inner.calls == 2


def test_the_key_covers_every_field_that_could_change_the_reply():
    base = dict(arm="ABD", role="grounding", model="m", request=req())
    baseline = key_for(**base)
    assert key_for(**{**base, "arm": "ABCD"}) != baseline
    assert key_for(**{**base, "role": "judge"}) != baseline
    assert key_for(**{**base, "model": "other"}) != baseline
    assert key_for(**{**base, "request": req(item_id="item-2")}) != baseline
    assert key_for(**{**base, "request": req(prompt="other")}) != baseline
    other = req()
    other.temperature = 0.7
    assert key_for(**{**base, "request": other}) != baseline
    other2 = req()
    other2.system = "different system prompt"
    assert key_for(**{**base, "request": other2}) != baseline


# ---------------------------------------------------------------------------
# It refuses to splice two experiments together
# ---------------------------------------------------------------------------

def test_a_journal_from_another_freeze_is_refused(tmp_path):
    path = tmp_path / "j.jsonl"
    wrap(CountingProvider(), Journal.open(path, OTHER_FREEZE)).generate(req())
    with pytest.raises(JournalMismatch, match="different experiment|two different"):
        Journal.open(path, FREEZE)


def test_a_journal_from_another_version_is_refused_rather_than_reinterpreted(tmp_path):
    path = tmp_path / "j.jsonl"
    path.write_text(json.dumps({"version": "journal/0.0.1", "freeze": FREEZE,
                                "key": "k", "response": {}}) + "\n")
    with pytest.raises(JournalMismatch, match="written by"):
        Journal.open(path, FREEZE)


def test_a_line_truncated_by_the_kill_it_survives_costs_one_call_not_the_run(tmp_path):
    """
    The last line is the only one that can be partial, because each is fsynced
    whole. Dropping it re-asks one question; refusing the file would re-ask
    every question.
    """
    path = tmp_path / "j.jsonl"
    book = Journal.open(path, FREEZE)
    provider = wrap(CountingProvider(), book)
    provider.generate(req(item_id="a"))
    provider.generate(req(item_id="b"))
    with path.open("a") as handle:
        handle.write('{"version": "journal/1.0.0", "freeze": "c29558')  # cut mid-write

    recovered = Journal.open(path, FREEZE)
    assert len(recovered.replies) == 2


# ---------------------------------------------------------------------------
# The ceilings bound the set, not the invocation
# ---------------------------------------------------------------------------

def test_spend_and_elapsed_are_carried_across_a_resume(tmp_path):
    """
    Without this, a 2400-attempt ceiling becomes 2400 attempts per crash.
    """
    path = tmp_path / "j.jsonl"

    class Spend:
        spent = {"candidate": 41, "judge": 7}

    class Clock:
        elapsed_minutes = 118.0

    wrap(CountingProvider(), Journal.open(path, FREEZE),
         budget=Spend(), clock=Clock()).generate(req())

    resumed = Journal.open(path, FREEZE)
    assert dict(resumed.spent) == {"candidate": 41, "judge": 7}
    assert resumed.elapsed_seconds == pytest.approx(118.0 * 60.0)


def test_a_replay_is_not_charged_against_the_budget(tmp_path):
    """
    A replayed reply makes no outbound attempt. Charging it would walk the
    frozen ceiling downward with every restart.
    """
    path = tmp_path / "j.jsonl"
    charged = []

    class Metered(CountingProvider):
        def generate(self, request):
            charged.append(request.item_id)
            return super().generate(request)

    inner = Metered()
    wrap(inner, Journal.open(path, FREEZE)).generate(req())
    assert charged == ["item-1"]
    wrap(inner, Journal.open(path, FREEZE)).generate(req())
    assert charged == ["item-1"], "a replay reached the metered provider"


# ---------------------------------------------------------------------------
# It stays invisible to everything that describes a run
# ---------------------------------------------------------------------------

def test_the_wrapper_is_indistinguishable_from_what_it_wraps(tmp_path):
    """
    The manifest, the freeze and the ablation report all read these attributes
    off the provider. A wrapper that hid them would change the record.
    """
    inner = CountingProvider()
    wrapped = wrap(inner, Journal.open(tmp_path / "j.jsonl", FREEZE))
    for attribute in ("name", "model", "model_version", "model_family",
                      "is_model", "is_oracle"):
        assert getattr(wrapped, attribute) == getattr(inner, attribute)


def test_the_journal_records_what_the_reply_cost_rather_than_smoothing_it(tmp_path):
    """
    `attempts` and `latency_ms` travel with the reply, so a resumed arm still
    reports that an item took three tries.
    """
    path = tmp_path / "j.jsonl"

    class Expensive(CountingProvider):
        def generate(self, request):
            response = super().generate(request)
            response.attempts = 3
            response.latency_ms = 4321.0
            return response

    wrap(Expensive(), Journal.open(path, FREEZE)).generate(req())
    replayed = wrap(Expensive(), Journal.open(path, FREEZE)).generate(req())
    assert replayed.attempts == 3
    assert replayed.latency_ms == 4321.0


def test_every_written_line_carries_the_freeze_it_belongs_to(tmp_path):
    path = tmp_path / "j.jsonl"
    provider = wrap(CountingProvider(), Journal.open(path, FREEZE))
    provider.generate(req(item_id="a"))
    provider.generate(req(item_id="b"))
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [r["freeze"] for r in rows] == [FREEZE, FREEZE]
    assert {r["version"] for r in rows} == {JOURNAL_VERSION}


# ---------------------------------------------------------------------------
# A request that never reached the provider is not evidence about the provider
# ---------------------------------------------------------------------------

REFUSED = "TimeoutError: NVIDIA NIM request failed: [Errno 111] Connection refused"


class Unreachable(CountingProvider):
    """The network is gone. Nothing is listening; nothing is asked."""

    def generate(self, request):
        response = super().generate(request)
        response.error = REFUSED
        response.raw_output, response.parsed = "", None
        response.latency_ms = 100.0
        return response


def test_a_request_that_never_reached_the_provider_is_not_written_down(tmp_path):
    """
    Rule 1 makes a recorded outage permanent. That is right for an observation
    of the run and catastrophic for our own network failing mid-teardown: it
    would freeze a fact about the harness as a fact about the model.
    """
    path = tmp_path / "j.jsonl"
    book = Journal.open(path, FREEZE)
    wrap(Unreachable(), book).generate(req())
    assert book.unreached == 1
    assert not path.exists() or path.read_text().strip() == ""

    # So the item is asked for the first time on the next invocation.
    healthy = CountingProvider()
    response = wrap(healthy, Journal.open(path, FREEZE)).generate(req())
    assert healthy.calls == 1
    assert response.ok


def test_rows_written_under_the_older_rule_are_skipped_not_deleted(tmp_path):
    """
    The file stays a complete account of what happened; the rule that filters
    it is the one under test. Deleting evidence to fix a rule is how the fix
    becomes unauditable.
    """
    path = tmp_path / "j.jsonl"
    rows = [
        {"version": JOURNAL_VERSION, "freeze": FREEZE, "key": "good",
         "response": {"item_id": "a", "raw_output": "yes", "parsed": {}, "provider": "p",
                      "model": "m", "model_version": "1", "latency_ms": 50.0,
                      "error": None, "attempts": 1}},
        {"version": JOURNAL_VERSION, "freeze": FREEZE, "key": "unreached",
         "response": {"item_id": "b", "raw_output": "", "parsed": None, "provider": "p",
                      "model": "m", "model_version": "1", "latency_ms": 0.1,
                      "error": REFUSED, "attempts": 3}},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    book = Journal.open(path, FREEZE)
    assert set(book.replies) == {"good"}
    assert book.skipped_unreached == 1
    assert len(path.read_text().strip().splitlines()) == 2, "the file was edited"


def test_an_ambiguous_failure_is_still_recorded(tmp_path):
    """
    The narrow part of the rule, and the part that stops it being a loophole.

    A timeout may mean the request arrived and the answer was lost coming
    back, so it counts. Only failures that PROVE the connection never came up
    are exempt. Anything else and "it was probably the network" becomes a
    universal solvent for inconvenient outages.
    """
    path = tmp_path / "j.jsonl"
    for n, error in enumerate((
            "TimeoutError: request timed out after 30s",
            "RuntimeError: NVIDIA NIM HTTP 500: internal server error",
            "RuntimeError: NVIDIA NIM HTTP 429: rate limited",
            "RuntimeError: connection reset by peer")):
        book = Journal.open(path, FREEZE)
        sick = CountingProvider(error=error)
        # A distinct arm per case: sharing one would make the second case
        # replay the first, which is correct behaviour and a useless test.
        wrap(sick, book, arm=f"arm-{n}").generate(req())
        assert book.unreached == 0, f"{error!r} was treated as never sent"
        assert book.recorded == 1, f"{error!r} was not recorded"


def test_the_unreached_failure_is_never_counted_against_the_model(tmp_path):
    from benchmark.provider_status import ProviderStatus, classify, policy_for

    assert classify(error=REFUSED) == ProviderStatus.UNREACHED
    policy = policy_for(ProviderStatus.UNREACHED)
    assert policy.counts_against_quality is False
    # The break is on our side of the socket; opening the circuit on the
    # provider would blame it for our outage.
    assert policy.open_circuit is False


def test_a_freeze_mismatch_names_both_digests_in_full(tmp_path):
    """
    Truncated digests print two different configurations as the same string,
    turning a real mismatch into a nonsense message.
    """
    path = tmp_path / "j.jsonl"
    a = "919cd25bc306" + "a" * 52
    b = "919cd25bc306" + "b" * 52
    wrap(CountingProvider(), Journal.open(path, a)).generate(req())
    with pytest.raises(JournalMismatch) as caught:
        Journal.open(path, b)
    assert a in str(caught.value) and b in str(caught.value)


# ---------------------------------------------------------------------------
# End to end: an interrupted set resumes to the same answers
# ---------------------------------------------------------------------------

import importlib  # noqa: E402

from validator import pipeline, scripted  # noqa: E402
from validator.devset import load  # noqa: E402

eval_tool = importlib.import_module("tools_validator_eval")


class Killed(BaseException):
    """Stands in for the container restart. Deliberately not an Exception, so
    the runner's outage handling cannot mistake it for a provider failure."""


class DiesPartway:
    """An oracle that stops answering after n calls, the way a kill does."""

    def __init__(self, inner, budget_of_calls, counter):
        self._inner, self._left, self._counter = inner, budget_of_calls, counter

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def generate(self, request):
        if self._counter[0] >= self._left:
            raise Killed("container restarted")
        self._counter[0] += 1
        return self._inner.generate(request)


def _verdict_rows(verdicts):
    return [v.as_dict() for v in sorted(verdicts, key=lambda v: v.item_id)]


def test_a_resumed_set_reaches_exactly_the_verdicts_an_uninterrupted_one_did(tmp_path):
    """
    The guarantee that makes resuming legitimate at all.

    Verdicts are recomputed by the real pipeline from the real replies, never
    rehydrated from a summary -- a lossily reconstructed verdict is fabricated
    evidence. So the resumed arm must be indistinguishable from the arm that
    was never interrupted, field for field.
    """
    devset = load("corpus/validator_dev")
    cases = devset.cases[:12]
    config = pipeline.Config(structural=True, grounding=True, judge=True,
                             conformance=True)

    def oracles():
        return scripted.oracle(cases)

    ground, judge, conform = oracles()
    reference, reference_outages = eval_tool.evaluate(
        cases, grounding_provider=ground, judge_provider=judge,
        conformance_provider=conform, config=config)

    path = tmp_path / "resume.jsonl"

    # First invocation: dies partway through.
    counter = [0]
    g, j, c = oracles()
    book = Journal.open(path, FREEZE)
    with pytest.raises(Killed):
        eval_tool.evaluate(
            cases,
            grounding_provider=wrap(DiesPartway(g, 9, counter), book,
                                    arm="ABCD", role="grounding"),
            judge_provider=wrap(DiesPartway(j, 9, counter), book,
                                arm="ABCD", role="judge"),
            conformance_provider=wrap(DiesPartway(c, 9, counter), book,
                                      arm="ABCD", role="conformance"),
            config=config)
    partial = Journal.open(path, FREEZE)
    assert partial.replies, "nothing survived the kill; the journal did not do its job"

    # Second invocation: same journal, fresh providers, runs to the end.
    g, j, c = oracles()
    resumed_book = Journal.open(path, FREEZE)
    verdicts, outages = eval_tool.evaluate(
        cases,
        grounding_provider=wrap(g, resumed_book, arm="ABCD", role="grounding"),
        judge_provider=wrap(j, resumed_book, arm="ABCD", role="judge"),
        conformance_provider=wrap(c, resumed_book, arm="ABCD", role="conformance"),
        config=config)

    assert _verdict_rows(verdicts) == _verdict_rows(reference)
    assert outages == reference_outages
    assert resumed_book.replayed > 0, "the resume paid for work already on disk"
