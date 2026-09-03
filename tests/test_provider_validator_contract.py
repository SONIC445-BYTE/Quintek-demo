"""
The contract between a provider adapter and the validator that reads it.

WHY THIS FILE EXISTS
--------------------
On 2026-09-03 Phase 0 produced specificity 0%, sensitivity 100% and a
discrimination rate of 0% -- a validator that flagged all 68 decided items.
The cause was not the model and not the checks. `NVIDIAProvider._call`
returned the whole HTTP body as `raw_output`, and every validator layer does
`extract_json(response.raw_output)` to recover the model's JSON. An HTTP
envelope is itself a balanced JSON object, so the validator parsed
`{"id": "chatcmpl-...", "choices": [...]}` and read THAT as the answer.
`supported` is absent from an envelope, so every item was flagged
`not_answerable_from_passage`. 91 of 94 grounding calls in that run.

`validator/scripted.py` had always returned the model's reply, so the contract
was never ambiguous -- only this one adapter disagreed. 1274 tests were green
because every one of them drove a scripted provider: nothing compared the two
implementations against the consumer they share.

So these tests are written against the SEAM rather than either side of it, and
they are parameterised over the adapters so a new one cannot quietly join with
its own idea of what `raw_output` means.
"""

from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import patch

import pytest

from benchmark.providers.base import GenerationRequest
from benchmark.providers.nvidia import NVIDIAProvider
from validator.grounding import extract_json
from validator.scripted import ReplayProvider

#: What a grounding reply looks like. The field the validator needs is
#: `supported`; its absence is what turned into a flag on every item.
MODEL_REPLY = {
    "passage_addresses_question": True,
    "supported": ["D"],
    "evidence": {"D": "a span copied word for word"},
    "reasoning": "one sentence",
}


class _FakeHTTPResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _envelope(content, reasoning_content=None) -> bytes:
    message = {"role": "assistant", "content": content}
    if reasoning_content is not None:
        message["reasoning_content"] = reasoning_content
    return json.dumps({
        "id": "chatcmpl-contract",
        "choices": [{"message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }).encode("utf-8")


def _nvidia_reply(content, reasoning_content=None, api_key="nvapi-test-key",
                  monkeypatch=None):
    monkeypatch.setenv("NVIDIA_API_KEY", api_key)
    provider = NVIDIAProvider("deepseek-ai/deepseek-v4-flash-0731")
    with patch("benchmark.providers.nvidia.urllib.request.urlopen",
               return_value=_FakeHTTPResponse(_envelope(content, reasoning_content))):
        return provider.generate(GenerationRequest(item_id="vd-clean-001:key",
                                                   prompt="p", system="s"))


# ---------------------------------------------------------------------------
# The seam
# ---------------------------------------------------------------------------

def test_the_adapter_returns_the_models_reply_not_the_http_envelope(monkeypatch):
    """
    THE regression. raw_output is the model's text; the transport that carried
    it is not part of the answer.
    """
    response = _nvidia_reply(json.dumps(MODEL_REPLY), monkeypatch=monkeypatch)
    assert response.ok
    assert "chatcmpl-contract" not in response.raw_output
    assert "choices" not in response.raw_output
    assert json.loads(response.raw_output) == MODEL_REPLY


def test_the_validator_recovers_the_answer_from_what_the_adapter_returned(monkeypatch):
    """
    The consumer's own function, against the producer's real output. This is
    the assertion whose absence cost a whole Phase 0 run.
    """
    response = _nvidia_reply(json.dumps(MODEL_REPLY), monkeypatch=monkeypatch)
    parsed = extract_json(response.raw_output)
    assert parsed is not None
    assert parsed.get("supported") == ["D"], (
        "the validator did not get the model's object; if this is an HTTP "
        "envelope, every item will be flagged not_answerable_from_passage")
    assert "choices" not in parsed


def test_both_adapters_present_the_same_thing_to_the_validator(monkeypatch):
    """
    Scripted and real must be indistinguishable at this seam, or a suite that
    passes on one proves nothing about the other.
    """
    live = _nvidia_reply(json.dumps(MODEL_REPLY), monkeypatch=monkeypatch)
    scripted = ReplayProvider({"vd-clean-001:key": MODEL_REPLY}).generate(
        GenerationRequest(item_id="vd-clean-001:key", prompt="p", system="s"))
    assert extract_json(live.raw_output) == extract_json(scripted.raw_output)


def test_a_reasoning_model_that_fills_both_fields_still_yields_its_answer(monkeypatch):
    """
    `reasoning_content` is the thinking, `content` is the reply. Joining them
    appended prose to the JSON and the greedy extractor spanned into it; 93 of
    340 replies in Phase 0's journal failed to parse for this reason alone.
    """
    response = _nvidia_reply(
        json.dumps(MODEL_REPLY),
        reasoning_content='We must decide. The passage says {something} here.',
        monkeypatch=monkeypatch)
    assert json.loads(response.raw_output) == MODEL_REPLY
    assert extract_json(response.raw_output).get("supported") == ["D"]


def test_a_reasoning_model_that_fills_only_reasoning_content_still_works(monkeypatch):
    """The case `content_of` was introduced for, which must keep working."""
    response = _nvidia_reply(None, reasoning_content=json.dumps(MODEL_REPLY),
                             monkeypatch=monkeypatch)
    assert extract_json(response.raw_output).get("supported") == ["D"]


def test_an_empty_reply_is_unparseable_rather_than_an_envelope(monkeypatch):
    """
    A model that says nothing must produce a parse failure -- an outage -- not
    a silently-parsed envelope that scores as a finding about the item.
    """
    response = _nvidia_reply("", reasoning_content="", monkeypatch=monkeypatch)
    assert extract_json(response.raw_output) is None


# ---------------------------------------------------------------------------
# A reply that was cut off is an outage, not a finding
# ---------------------------------------------------------------------------

def _truncated_envelope(content, reasoning_content) -> bytes:
    message = {"role": "assistant", "content": content,
               "reasoning_content": reasoning_content}
    return json.dumps({
        "id": "chatcmpl-truncated",
        "choices": [{"message": message, "finish_reason": "length"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4096},
    }).encode("utf-8")


def _truncated_reply(content, reasoning_content, monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-key")
    provider = NVIDIAProvider("deepseek-ai/deepseek-v4-flash-0731")
    with patch("benchmark.providers.nvidia.urllib.request.urlopen",
               return_value=_FakeHTTPResponse(
                   _truncated_envelope(content, reasoning_content))):
        return provider.generate(GenerationRequest(item_id="vd-clean-014:key",
                                                   prompt="p", system="s"))


def test_a_reply_cut_off_mid_thought_is_unparseable_not_a_fragment(monkeypatch):
    """
    The model was still reasoning when it hit the cap, so it never gave an
    answer. `extract_json` takes the FIRST balanced object out of whatever it
    is handed, and reasoning prose quotes JSON: without this guard a snippet
    like {"A": "..."} inside the thinking was read as the reply, `supported`
    was absent, and the item was flagged not_answerable_from_passage on the
    strength of a fragment of the model's own deliberation.
    """
    response = _truncated_reply(
        None,
        'We need to answer. The passage says "x". Evidence would be {"A": "some span"} '
        'but let me reconsider',
        monkeypatch)
    assert extract_json(response.raw_output) is None, (
        "a fragment was scavenged out of an unfinished reply")


def test_a_truncated_reply_that_did_emit_its_answer_is_still_used(monkeypatch):
    """
    Truncation after a complete answer is not a reason to discard the answer.
    The guard keys on there being no content at all, not on finish_reason
    alone.
    """
    response = _truncated_reply(json.dumps(MODEL_REPLY), "thinking that ran long",
                                monkeypatch)
    assert extract_json(response.raw_output).get("supported") == ["D"]


def test_the_validator_layers_ask_for_room_to_think():
    """
    1024 was a dataclass default, not a validator decision, and it truncated
    18% of a reasoning candidate's replies -- enough to push the clean arm
    below the item floor its own gate requires.
    """
    from validator.grounding import MAX_REPLY_TOKENS
    assert MAX_REPLY_TOKENS >= 2048
    seen = []

    class Recorder(ReplayProvider):
        def generate(self, request):
            seen.append((request.metadata.get("layer"), request.max_tokens))
            return super().generate(request)

    from validator import conformance, grounding, judge
    item = {"id": "i", "stem": "s", "options": ["a", "b"], "correct_index": 0,
            "source_passage": "a passage", "concept": "c", "difficulty": "easy",
            "generated_by_family": "deepseek"}

    class Recorded(Recorder):
        model_family = "ising"        # independent of the item's author

    for run in (grounding.check, conformance.check, judge.check):
        try:
            run(item, Recorded({}, default={}))
        except Exception:
            pass                      # the reply is irrelevant; the request is not

    # Named explicitly: a layer that raises before issuing its request would
    # otherwise pass this test by never appearing in it, which is the exact
    # shape of the gap that let the envelope defect through.
    assert {layer for layer, _ in seen} == {"grounding", "conformance", "judge"}, seen
    assert all(t == MAX_REPLY_TOKENS for _, t in seen), seen
