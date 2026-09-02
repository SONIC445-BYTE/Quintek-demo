"""
Empirical capability probing, and the three-way distinction it exists for.

    DECLARED   the provider's catalogue said so
    OBSERVED   a probe sent a request and inspected the reply
    UNKNOWN    nobody has said or shown anything -- and that is not False

Every request goes through a fake transport. The interesting cases are a model
that answers wrongly, a model that cannot answer at all, and the difference
between those two, and none of them can be summoned from a real endpoint.
"""

from __future__ import annotations

import json

import pytest

from benchmark.capability_probe import (LONG_CONTEXT, PREREQUISITE, PROBES,
                                        PROBE_VERSION, REASONING, STRUCTURED_OUTPUT,
                                        TEXT_OUTPUT, TOOL_CALLING, VISION,
                                        _first_json_object, forecast, run_probes)
from benchmark.discovery import (Availability, DynamicModelRegistry, Lifecycle,
                                 Observation, Provenance)
from benchmark.provider_catalogue import HttpResult, SOURCES, Transport

T0 = "2026-08-01T00:00:00Z"
T1 = "2026-08-02T00:00:00Z"
ENV = {"NVIDIA_API_KEY": "x"}

NVIDIA_410 = ('{"status":410,"detail":"The model has reached its end of life on '
              '2026-08-26T09:00:00Z and is no longer available."}')


def chat(text=None, *, tool_calls=None):
    message = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return json.dumps({"choices": [{"message": message}]})


class ScriptedTransport(Transport):
    """
    Replies chosen by what the probe asked, not by the model id, so a test
    cannot accidentally pass because the fake recognised a famous name.
    """

    def __init__(self, replies, *, status=200):
        self.replies = replies          # match-substring -> body (or Exception)
        self.status = status
        self.sent = []

    def get(self, url, *, headers, timeout):
        raise AssertionError("a capability probe must not call the catalogue endpoint")

    def post_json(self, url, *, headers, payload, timeout):
        self.sent.append(payload)
        content = payload["messages"][0]["content"]
        haystack = json.dumps(content) + json.dumps(payload.get("tools", ""))
        # An ambiguous match is an error, not a coin toss. Two probe prompts
        # legitimately share the phrase "JSON object", and a dict-order match
        # sent the reasoning probe the structured-output reply. The registry
        # then correctly recorded the result as inconclusive -- so the code was
        # right and the fixture was lying, which is the worse of the two: a
        # fixture that can misroute a reply can make a broken probe look fine.
        matches = [n for n in self.replies if n in haystack]
        if len(matches) > 1:
            raise AssertionError(
                f"ambiguous scripted reply: {sorted(matches)} all match the same "
                f"request. Pick needles that identify exactly one probe.")
        if matches:
            reply = self.replies[matches[0]]
            if isinstance(reply, BaseException):
                return HttpResult(status=None, error=reply, latency_ms=60_000.0)
            if isinstance(reply, tuple):
                code, body = reply
                return HttpResult(status=code, body=body, latency_ms=20.0,
                                  error=None if code < 300 else f"HTTP {code}: {body}")
            return HttpResult(status=200, body=reply, latency_ms=20.0)
        return HttpResult(status=self.status, body=chat("unrecognised"),
                          latency_ms=20.0,
                          error=None if self.status < 300 else f"HTTP {self.status}")


def source():
    return SOURCES["nvidia"]


def seeded(tmp_path, *model_ids, available=True):
    reg = DynamicModelRegistry(tmp_path / "registry.json")
    reg.reconcile("nvidia", [Observation(provider="nvidia", model_id=m)
                             for m in model_ids], at=T0)
    if available:
        for m in model_ids:
            reg.record_probe(f"nvidia:{m}", http_status=200, at=T0)
    return reg


# ---------------------------------------------------------------------------
# The three-way distinction
# ---------------------------------------------------------------------------

def test_declared_observed_and_unknown_are_three_different_things(tmp_path):
    reg = DynamicModelRegistry(tmp_path / "r.json")
    reg.reconcile("openrouter", [Observation(
        provider="openrouter", model_id="m",
        capabilities={"structured_output": True})], at=T0)
    reg.record_probe("openrouter:m", http_status=200, at=T0)

    declared = reg.get("openrouter:m").capability("structured_output")
    assert declared.value is True
    assert declared.source == Provenance.DECLARED
    assert declared.observed is False

    unknown = reg.get("openrouter:m").capability("vision")
    assert unknown.value is None
    assert unknown.source == Provenance.UNKNOWN
    assert unknown.known is False

    transport = ScriptedTransport({"ready": chat("ready"),
                                   "colour": chat("Red.")})
    run = run_probes(source(), "m", [VISION], transport=transport, env=ENV)
    reg.record_capability_probe("openrouter:m", run.claims(), at=T1,
                                probe_version=PROBE_VERSION)
    observed = reg.get("openrouter:m").capability("vision")
    assert observed.value is True
    assert observed.source == Provenance.OBSERVED
    assert observed.at == T1
    assert observed.probe_version == PROBE_VERSION
    assert "red" in observed.evidence.lower()


def test_a_catalogue_may_not_overwrite_an_observation(tmp_path):
    """
    The catalogue is what the provider says; the probe is what happened. When
    they disagree the probe is the one that sent a request.
    """
    reg = DynamicModelRegistry(tmp_path / "r.json")
    reg.reconcile("openrouter", [Observation(provider="openrouter", model_id="m",
                                             capabilities={"vision": True})], at=T0)
    reg.record_probe("openrouter:m", http_status=200, at=T0)
    transport = ScriptedTransport({"ready": chat("ready"),
                                   "colour": chat("I cannot see images.")})
    run = run_probes(source(), "m", [VISION], transport=transport, env=ENV)
    reg.record_capability_probe("openrouter:m", run.claims(), at=T1)
    assert reg.get("openrouter:m").capability("vision").value is False

    # A later catalogue pass still claims vision. The observation stands.
    reg.reconcile("openrouter", [Observation(provider="openrouter", model_id="m",
                                             capabilities={"vision": True})], at=T1)
    claim = reg.get("openrouter:m").capability("vision")
    assert claim.value is False
    assert claim.source == Provenance.OBSERVED


def test_an_unreachable_probe_leaves_the_claim_unknown_not_false(tmp_path):
    """
    The single most important rule here. A model that 410s during a probe has
    not been shown to lack a capability; recording False would disqualify it
    permanently for an outage.
    """
    reg = seeded(tmp_path, "m")
    transport = ScriptedTransport({"ready": (410, NVIDIA_410)})
    run = run_probes(source(), "m", [STRUCTURED_OUTPUT, REASONING],
                     transport=transport, env=ENV)
    assert run.outcomes[TEXT_OUTPUT].value is None
    assert "410" in run.outcomes[TEXT_OUTPUT].evidence or \
        "RETIRED" in run.outcomes[TEXT_OUTPUT].evidence

    reg.record_capability_probe("nvidia:m", run.claims(), at=T1)
    record = reg.get("nvidia:m")
    assert record.capability(STRUCTURED_OUTPUT).value is None
    assert record.capability(REASONING).value is None
    assert record.capability_probes_inconclusive >= 1
    # And nothing was recorded as an observation.
    assert record.probed_capabilities == []


def test_a_conclusive_negative_is_recorded_as_false(tmp_path):
    reg = seeded(tmp_path, "m")
    transport = ScriptedTransport({"ready": chat("ready"),
                                   "JSON object": chat("Sure! The answer is B.")})
    run = run_probes(source(), "m", [STRUCTURED_OUTPUT], transport=transport, env=ENV)
    reg.record_capability_probe("nvidia:m", run.claims(), at=T1)
    claim = reg.get("nvidia:m").capability(STRUCTURED_OUTPUT)
    assert claim.value is False
    assert claim.source == Provenance.OBSERVED
    assert "no JSON object" in claim.evidence


# ---------------------------------------------------------------------------
# Each probe
# ---------------------------------------------------------------------------

def test_text_output_separates_a_chat_model_from_an_embedder():
    transport = ScriptedTransport({"ready": chat("ready")})
    assert run_probes(source(), "m", [TEXT_OUTPUT], transport=transport,
                      env=ENV).outcomes[TEXT_OUTPUT].value is True

    # An embedding response to a chat-completions POST IS the answer: this
    # endpoint does not serve chat. Leaving it UNKNOWN would re-probe an
    # embedder forever.
    transport = ScriptedTransport({"ready": json.dumps({"data": [{"embedding": [0.1]}]})})
    run = run_probes(source(), "m", [TEXT_OUTPUT], transport=transport, env=ENV)
    assert run.outcomes[TEXT_OUTPUT].value is False
    assert "embedding" in run.outcomes[TEXT_OUTPUT].evidence


def test_structured_output_accepts_json_inside_a_code_fence():
    transport = ScriptedTransport({
        "ready": chat("ready"),
        "JSON object": chat('```json\n{"answer": "B"}\n```')})
    run = run_probes(source(), "m", [STRUCTURED_OUTPUT], transport=transport, env=ENV)
    assert run.outcomes[STRUCTURED_OUTPUT].value is True


def test_structured_output_rejects_json_without_the_requested_key():
    transport = ScriptedTransport({"ready": chat("ready"),
                                   "JSON object": chat('{"result": "B"}')})
    run = run_probes(source(), "m", [STRUCTURED_OUTPUT], transport=transport, env=ENV)
    assert run.outcomes[STRUCTURED_OUTPUT].value is False


def test_reasoning_is_verified_against_a_known_answer():
    transport = ScriptedTransport({"ready": chat("ready"), "beds": chat('{"beds": 10}')})
    assert run_probes(source(), "m", [REASONING], transport=transport,
                      env=ENV).outcomes[REASONING].value is True

    transport = ScriptedTransport({"ready": chat("ready"), "beds": chat('{"beds": 12}')})
    run = run_probes(source(), "m", [REASONING], transport=transport, env=ENV)
    assert run.outcomes[REASONING].value is False
    assert "expected 10" in run.outcomes[REASONING].evidence


def test_reasoning_is_inconclusive_when_the_reply_is_not_the_asked_for_shape():
    """
    A model that ignored the format has not been shown to reason badly. The
    two failures need different fixes, so they get different values.
    """
    transport = ScriptedTransport({"ready": chat("ready"),
                                   "beds": chat("There are ten usable beds.")})
    run = run_probes(source(), "m", [REASONING], transport=transport, env=ENV)
    assert run.outcomes[REASONING].value is None
    assert "says nothing about reasoning" in run.outcomes[REASONING].evidence


def test_tool_calling_needs_an_actual_tool_call():
    calls = [{"id": "1", "type": "function",
              "function": {"name": "get_weather", "arguments": '{"city":"Kolkata"}'}}]
    transport = ScriptedTransport({"ready": chat("ready"),
                                   "get_weather": chat(None, tool_calls=calls)})
    assert run_probes(source(), "m", [TOOL_CALLING], transport=transport,
                      env=ENV).outcomes[TOOL_CALLING].value is True

    transport = ScriptedTransport({"ready": chat("ready"),
                                   "get_weather": chat("It is warm in Kolkata.")})
    assert run_probes(source(), "m", [TOOL_CALLING], transport=transport,
                      env=ENV).outcomes[TOOL_CALLING].value is False


def test_vision_sends_a_real_image_and_believes_only_the_answer():
    transport = ScriptedTransport({"ready": chat("ready"), "colour": chat("Red")})
    run = run_probes(source(), "nvidia/llama-3.2-11b-vision-instruct", [VISION],
                     transport=transport, env=ENV)
    assert run.outcomes[VISION].value is True
    sent = [p for p in transport.sent if "colour" in json.dumps(p["messages"])][0]
    parts = sent["messages"][0]["content"]
    assert any(part.get("type") == "image_url" for part in parts)


def test_a_name_that_says_vision_is_not_evidence_of_vision():
    """The id says vision; the model does not identify the image. False wins."""
    transport = ScriptedTransport({"ready": chat("ready"),
                                   "colour": chat("I am a text-only model.")})
    run = run_probes(source(), "nvidia/llama-3.2-11b-vision-instruct", [VISION],
                     transport=transport, env=ENV)
    assert run.outcomes[VISION].value is False


def test_long_context_is_opt_in():
    transport = ScriptedTransport({"ready": chat("ready"), "access code": chat("QX-4417")})
    run = run_probes(source(), "m", [LONG_CONTEXT], transport=transport, env=ENV)
    assert LONG_CONTEXT not in run.outcomes
    assert run.calls == 1                      # only the prerequisite

    run = run_probes(source(), "m", [LONG_CONTEXT], transport=transport, env=ENV,
                     include_opt_in=True)
    assert run.outcomes[LONG_CONTEXT].value is True


# ---------------------------------------------------------------------------
# Budget discipline
# ---------------------------------------------------------------------------

def test_a_failed_prerequisite_stops_the_pass_instead_of_paying_for_the_rest():
    transport = ScriptedTransport({"ready": json.dumps({"data": [{"embedding": [1]}]})})
    run = run_probes(source(), "embedder",
                     [STRUCTURED_OUTPUT, REASONING, TOOL_CALLING, VISION],
                     transport=transport, env=ENV)
    assert run.calls == 1
    assert run.stopped_early
    assert set(run.inconclusive) >= {STRUCTURED_OUTPUT, REASONING, TOOL_CALLING, VISION}


def test_the_funnel_does_not_probe_what_it_already_knows(tmp_path):
    reg = seeded(tmp_path, "unknown-caps", "known-good", "known-bad", "unreachable")
    reg.record_probe("nvidia:unreachable", error="504 gateway timeout",
                     http_status=504, at=T0)
    transport = ScriptedTransport({"ready": chat("ready"),
                                   "JSON object": chat('{"answer":"B"}')})
    for key, model in (("nvidia:known-good", "known-good"),):
        run = run_probes(source(), model, [STRUCTURED_OUTPUT], transport=transport, env=ENV)
        reg.record_capability_probe(key, run.claims(), at=T0)
    transport = ScriptedTransport({"ready": chat("ready"), "JSON object": chat("nope")})
    run = run_probes(source(), "known-bad", [STRUCTURED_OUTPUT], transport=transport, env=ENV)
    reg.record_capability_probe("nvidia:known-bad", run.claims(), at=T0)

    due = [r.model_id for r in reg.due_for_capability_probe([STRUCTURED_OUTPUT])]
    assert due == ["unknown-caps"], due


def test_a_router_is_never_capability_probed(tmp_path):
    reg = DynamicModelRegistry(tmp_path / "r.json")
    reg.reconcile("openrouter", [Observation(provider="openrouter",
                                             model_id="openrouter/free",
                                             entry_kind="ROUTER")], at=T0)
    reg.record_probe("openrouter:openrouter/free", http_status=200, at=T0)
    assert reg.due_for_capability_probe([STRUCTURED_OUTPUT]) == []


def test_the_forecast_counts_calls_exactly_and_labels_tokens_as_estimates():
    plan = forecast([1, 2], [STRUCTURED_OUTPUT, REASONING])
    assert plan["probes_per_model"] == 3           # + the prerequisite
    assert plan["calls"] == 6
    assert "estimates" in plan["note"]
    assert forecast([1], [LONG_CONTEXT])["calls"] == 1        # opt-in excluded


def test_requiring_a_capability_with_no_probe_is_an_error_not_a_silent_pass():
    """
    Otherwise a typo in a role's requirements disqualifies every model in the
    registry and reads as "no model can do this".
    """
    with pytest.raises(ValueError, match="no probe defines"):
        run_probes(source(), "m", ["telepathy"],
                   transport=ScriptedTransport({}), env=ENV)


def test_a_missing_credential_yields_inconclusive_not_negative():
    run = run_probes(source(), "m", [STRUCTURED_OUTPUT],
                     transport=ScriptedTransport({}), env={})
    assert run.calls == 0
    assert run.outcomes == {}
    assert STRUCTURED_OUTPUT in run.inconclusive


# ---------------------------------------------------------------------------
# The lifecycle this feeds
# ---------------------------------------------------------------------------

def test_the_lifecycle_runs_unverified_to_production_eligible(tmp_path):
    requirements = (STRUCTURED_OUTPUT, REASONING)
    reg = DynamicModelRegistry(tmp_path / "r.json")
    reg.reconcile("nvidia", [Observation(provider="nvidia", model_id="m")], at=T0)
    record = reg.get("nvidia:m")
    assert record.lifecycle(requirements=requirements) == Lifecycle.UNVERIFIED

    reg.record_probe("nvidia:m", http_status=200, at=T0)
    assert record.lifecycle(requirements=requirements) == Lifecycle.PROBED

    transport = ScriptedTransport({"ready": chat("ready"),
                                   "Use the letter B": chat('{"answer":"B"}'),
                                   "3 bays": chat('{"beds": 10}')})
    run = run_probes(source(), "m", list(requirements), transport=transport, env=ENV)
    reg.record_capability_probe("nvidia:m", run.claims(), at=T1)
    assert record.lifecycle(requirements=requirements) == Lifecycle.QUALIFIED
    assert record.lifecycle(requirements=requirements,
                            evaluated=True) == Lifecycle.PRODUCTION_ELIGIBLE


def test_a_model_that_fails_a_required_capability_is_disqualified(tmp_path):
    reg = seeded(tmp_path, "m")
    transport = ScriptedTransport({"ready": chat("ready"), "JSON object": chat("prose")})
    run = run_probes(source(), "m", [STRUCTURED_OUTPUT], transport=transport, env=ENV)
    reg.record_capability_probe("nvidia:m", run.claims(), at=T1)
    assert reg.get("nvidia:m").lifecycle(
        requirements=(STRUCTURED_OUTPUT,)) == Lifecycle.DISQUALIFIED


def test_an_outage_moves_a_qualified_model_to_temporarily_unavailable_and_back(tmp_path):
    requirements = (STRUCTURED_OUTPUT,)
    reg = seeded(tmp_path, "m")
    transport = ScriptedTransport({"ready": chat("ready"),
                                   "JSON object": chat('{"answer":"B"}')})
    run = run_probes(source(), "m", list(requirements), transport=transport, env=ENV)
    reg.record_capability_probe("nvidia:m", run.claims(), at=T1)
    record = reg.get("nvidia:m")
    assert record.lifecycle(requirements=requirements) == Lifecycle.QUALIFIED

    reg.record_probe("nvidia:m", error="504 gateway timeout", http_status=504, at=T1)
    assert record.lifecycle(requirements=requirements) == Lifecycle.TEMPORARILY_UNAVAILABLE

    reg.record_probe("nvidia:m", http_status=200, at=T1)
    # Recovered, and its qualification survived the outage rather than needing
    # to be re-earned -- but it is QUALIFIED, not PRODUCTION_ELIGIBLE: the
    # evaluation evidence is a separate gate and recovery does not grant it.
    assert record.lifecycle(requirements=requirements) == Lifecycle.QUALIFIED


def test_a_retired_model_is_retired_whatever_it_once_qualified_for(tmp_path):
    reg = seeded(tmp_path, "m")
    transport = ScriptedTransport({"ready": chat("ready"),
                                   "JSON object": chat('{"answer":"B"}')})
    run = run_probes(source(), "m", [STRUCTURED_OUTPUT], transport=transport, env=ENV)
    reg.record_capability_probe("nvidia:m", run.claims(), at=T1)
    reg.record_probe("nvidia:m", error=NVIDIA_410, http_status=410, at=T1)
    record = reg.get("nvidia:m")
    assert record.lifecycle(requirements=(STRUCTURED_OUTPUT,)) == Lifecycle.RETIRED
    # The observation is still on the record; it just cannot be selected.
    assert record.capability(STRUCTURED_OUTPUT).value is True
    assert reg.eligible(required_capabilities=(STRUCTURED_OUTPUT,))[0] == []


def test_require_observed_rejects_a_merely_declared_capability(tmp_path):
    reg = DynamicModelRegistry(tmp_path / "r.json")
    reg.reconcile("openrouter", [Observation(
        provider="openrouter", model_id="m",
        capabilities={"structured_output": True})], at=T0)
    reg.record_probe("openrouter:m", http_status=200, at=T0)

    kept, _ = reg.eligible(required_capabilities=(STRUCTURED_OUTPUT,))
    assert [r.key for r in kept] == ["openrouter:m"]

    kept, dropped = reg.eligible(required_capabilities=(STRUCTURED_OUTPUT,),
                                 require_observed=True)
    assert kept == []
    assert "declared, not observed" in dropped[0]["reasons"][0]


def test_json_scanning_survives_braces_inside_strings():
    assert _first_json_object('{"answer": "}{"}') == {"answer": "}{"}
    assert _first_json_object('prose {not json} then {"answer": "B"}') == {"answer": "B"}


# ---------------------------------------------------------------------------
# Defects found by the 2026-08-28 live run (capability-probe/1.0.0)
# ---------------------------------------------------------------------------

def test_text_in_reasoning_content_is_still_text():
    """
    DEFECT. 1.0.0 read only `message.content`. Several NIM reasoning models
    put their reply in `reasoning_content` and leave `content` empty, so the
    probe recorded "cannot emit text" for a model that emitted plenty. Four
    models were marked text_output=False on the live run on that basis.
    """
    body = json.dumps({"choices": [{"finish_reason": "stop", "message": {
        "role": "assistant", "content": None,
        "reasoning_content": "Let me think. ready"}}]})
    transport = ScriptedTransport({"ready": body})
    run = run_probes(source(), "m", [TEXT_OUTPUT], transport=transport, env=ENV)
    assert run.outcomes[TEXT_OUTPUT].value is True


def test_a_reply_truncated_at_max_tokens_establishes_nothing():
    """
    DEFECT. An empty reply is evidence of incapability only if the model had
    room to answer and did not. Cut off at our own `max_tokens`, it says
    nothing -- and False there is a positive claim of incapability resting on
    a budget decision of ours, which is the exact error the tri-state exists
    to prevent, made one level up.
    """
    body = json.dumps({"choices": [{"finish_reason": "length",
                                    "message": {"content": ""}}]})
    transport = ScriptedTransport({"ready": body})
    run = run_probes(source(), "m", [TEXT_OUTPUT], transport=transport, env=ENV)
    outcome = run.outcomes[TEXT_OUTPUT]
    assert outcome.value is None
    assert "cut off" in outcome.evidence


def test_an_empty_reply_with_room_to_spare_is_still_a_no():
    """The other side of it: `stop` with nothing emitted is a real answer."""
    body = json.dumps({"choices": [{"finish_reason": "stop",
                                    "message": {"content": ""}}]})
    transport = ScriptedTransport({"ready": body})
    run = run_probes(source(), "m", [TEXT_OUTPUT], transport=transport, env=ENV)
    assert run.outcomes[TEXT_OUTPUT].value is False


def test_an_unreadable_reply_shape_establishes_nothing():
    transport = ScriptedTransport({"ready": json.dumps({"unexpected": "shape"})})
    run = run_probes(source(), "m", [TEXT_OUTPUT], transport=transport, env=ENV)
    assert run.outcomes[TEXT_OUTPUT].value is None


def test_an_inconclusive_probe_records_the_status_and_body_it_saw():
    """
    DEFECT. 1.0.0 recorded only "probe could not run (MODEL_RETIRED)". A
    terminal-sounding classification nobody can audit afterwards is worse than
    no classification -- five models on the live run could not be checked.
    """
    transport = ScriptedTransport({"ready": (410, NVIDIA_410)})
    run = run_probes(source(), "m", [TEXT_OUTPUT], transport=transport, env=ENV)
    outcome = run.outcomes[TEXT_OUTPUT]
    assert outcome.value is None
    assert outcome.http_status == 410
    assert "MODEL_RETIRED" in outcome.evidence
    assert "410" in outcome.evidence
    assert "end of life" in outcome.evidence          # the provider's own words


def test_a_capability_pass_carries_back_what_it_learned_about_availability():
    """
    DEFECT. A capability pass that gets a 410 has discovered a retirement.
    1.0.0 threw it away, so five models stayed AVAILABLE in the registry after
    the provider refused them.
    """
    transport = ScriptedTransport({"ready": (410, NVIDIA_410)})
    run = run_probes(source(), "m", [STRUCTURED_OUTPUT], transport=transport, env=ENV)
    assert run.availability["http_status"] == 410
    assert run.availability["provider_status"] == "MODEL_RETIRED"

    good = ScriptedTransport({"ready": chat("ready"),
                              "Use the letter B": chat('{"answer":"B"}')})
    run = run_probes(source(), "m", [STRUCTURED_OUTPUT], transport=good, env=ENV)
    assert run.availability["provider_status"] == "AVAILABLE"


def test_a_withdrawn_claim_returns_to_unknown_and_is_not_deleted(tmp_path):
    """
    A claim a later reading showed was unsound is not evidence of anything, so
    the honest state is the one before it was made -- not the opposite value,
    and not a deleted row.
    """
    reg = seeded(tmp_path, "m")
    transport = ScriptedTransport({"ready": chat("ready"),
                                   "Use the letter B": chat("prose, no json")})
    run = run_probes(source(), "m", [STRUCTURED_OUTPUT], transport=transport, env=ENV)
    reg.record_capability_probe("nvidia:m", run.claims(), at=T1,
                                probe_version="capability-probe/1.0.0")
    assert reg.get("nvidia:m").capability(STRUCTURED_OUTPUT).value is False

    reg.withdraw_capability_claim("nvidia:m", STRUCTURED_OUTPUT,
                                  reason="verifier was wrong in 1.0.0", at=T1)
    claim = reg.get("nvidia:m").capability(STRUCTURED_OUTPUT)
    assert claim.value is None
    assert claim.source == Provenance.UNKNOWN
    assert "withdrawn" in claim.evidence
    # The record, and the fact that a claim was withdrawn, both survive.
    assert reg.get("nvidia:m") is not None
    assert any(e["kind"] == "CAPABILITY_CLAIM_WITHDRAWN"
               for e in reg.get("nvidia:m").history)
    # And it becomes due for a probe again, rather than being stuck.
    assert reg.get("nvidia:m") in reg.due_for_capability_probe([STRUCTURED_OUTPUT])


def test_withdrawing_an_unknown_claim_is_a_no_op(tmp_path):
    reg = seeded(tmp_path, "m")
    before = len(reg.get("nvidia:m").history)
    reg.withdraw_capability_claim("nvidia:m", VISION, reason="nothing to withdraw")
    assert len(reg.get("nvidia:m").history) == before
