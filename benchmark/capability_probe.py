"""
Finding out what a model can actually do, when its provider will not say.

WHY
---
NVIDIA's and Cerebras's catalogues return `{id, object, created, owned_by}`.
No context window, no structured-output flag, no modalities. Every capability
is therefore `UNKNOWN`, and `unknown is not yes`, so `shortlist --role
validation` correctly returned **zero** candidates against a provider serving
nineteen working models. Correct, and useless.

The fix is not to relax the rule. It is to go and find out.

WHAT A PROBE IS AND IS NOT
--------------------------
A probe establishes a **floor**: can this model do the thing at all. It is not
a quality measurement and must never be read as one. `structured_output=True`
means one strict-JSON request came back as parseable JSON with the requested
key. It does not mean the model is good at JSON, and nothing here ranks
anything.

The distinction that matters most is between three outcomes:

    True     the reply demonstrated it
    False    the reply was well-formed and demonstrated the opposite
    None     the probe could not run, or could not be judged

`None` is not `False`. A model that 410s, times out, or gets rate-limited
mid-probe leaves the claim untouched and is counted as inconclusive. Writing
`False` there would permanently disqualify a model for an outage, and
`DynamicModelRegistry.record_capability_probe` refuses to store it.

Names are never evidence. Nothing in this module reads a model id to decide
what it supports -- `nvidia/llama-3.2-11b-vision-instruct` gets the same image
sent to it as everything else, and is believed only if it answers.

BUDGET
------
One probe per capability per model, `max_tokens` in the low tens, and only for
the capabilities a role actually requires. `DynamicModelRegistry.due_for_capability_probe`
applies the cheap filters first, so the funnel is:

    catalogue -> AVAILABLE -> not a router -> has an unknown this role needs
              -> and is not already known to lack one -> probe

`context` is deliberately opt-in and off by default: a needle-in-a-haystack
probe at 32k costs more input tokens than every other probe in this module
combined, and "the catalogue does not state a context window" is usually
better answered by asking the provider than by paying for it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .discovery import CapabilityClaim, Provenance
from .provider_catalogue import DEFAULT_TIMEOUT, MissingCredential, Transport
from .provider_status import ProviderStatus, classify

#: Bumped when a probe's prompt or verifier changes. Stored on every claim,
#: because a claim produced by a different question is a different claim and a
#: registry that cannot tell them apart cannot be re-based after a fix.
PROBE_VERSION = "capability-probe/1.1.0"

TEXT_OUTPUT = "text_output"
STRUCTURED_OUTPUT = "structured_output"
REASONING = "reasoning"
TOOL_CALLING = "tool_calling"
VISION = "vision"
LONG_CONTEXT = "long_context"

#: An 8x8 solid red PNG, inline. Small enough to send anywhere, and its answer
#: is verifiable without a vision model of our own. A 1x1 pixel is not usable:
#: several hosts reject it as a malformed image, which reads as "no vision"
#: when it is "no image".
RED_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAYAAADED76LAAAAHElEQVQoz2P8z8Dwn4GKgIm"
    "BgYFhVMPQ0MAIAP9OBAXBBpNlAAAAAElFTkSuQmCC")


@dataclass
class ProbeOutcome:
    """One probe, its verdict, and the evidence for it."""

    capability: str
    value: bool | None
    evidence: str
    http_status: int | None = None
    latency_ms: float | None = None
    provider_status: str = ""

    @property
    def conclusive(self) -> bool:
        return self.value is not None

    def as_claim(self) -> CapabilityClaim:
        return CapabilityClaim(value=self.value,
                               source=(Provenance.OBSERVED if self.conclusive
                                       else Provenance.UNKNOWN),
                               evidence=self.evidence,
                               probe_version=PROBE_VERSION)

    def as_dict(self) -> dict:
        return {"capability": self.capability, "value": self.value,
                "evidence": self.evidence, "http_status": self.http_status,
                "latency_ms": self.latency_ms,
                "provider_status": self.provider_status}


@dataclass
class Probe:
    """
    One capability, the request that tests it, and how to read the reply.

    `payload` is built per model rather than stored, because a probe body
    names the model. `verify` receives the parsed response body and returns
    `(value, evidence)`; returning `(None, why)` is how a probe says the reply
    was unreadable rather than negative.
    """

    capability: str
    max_tokens: int = 32
    #: Roughly how many input tokens this costs, for the forecast. An estimate
    #: is honest here in a way it is not for a call count: the point is to
    #: separate "trivial" from "this one is expensive", not to bill anybody.
    approx_input_tokens: int = 40
    opt_in: bool = False

    def payload(self, model_id: str) -> dict:
        raise NotImplementedError

    def verify(self, body: dict, text: str) -> tuple[bool | None, str]:
        raise NotImplementedError


def _chat(model_id: str, content, *, max_tokens: int, **extra) -> dict:
    return {"model": model_id, "messages": [{"role": "user", "content": content}],
            "max_tokens": max_tokens, "temperature": 0.0, **extra}


class TextOutputProbe(Probe):
    """
    Does it accept text and emit text at all?

    The floor beneath every other probe. A model that fails this one is not
    text-to-text, whatever else it may be -- several entries in NVIDIA's
    catalogue are embedders, rerankers and image models, and they pass a
    plain availability probe because a 200 is a 200.
    """

    def __init__(self):
        super().__init__(TEXT_OUTPUT, max_tokens=16, approx_input_tokens=20)

    def payload(self, model_id: str) -> dict:
        return _chat(model_id, "Reply with the single word: ready", max_tokens=16)

    def verify(self, body, text):
        if text.strip():
            return True, f"returned {len(text.strip())} character(s) of text"
        # An empty reply is only evidence of incapability if the model was
        # given room to answer and chose not to. Cut off at max_tokens, it
        # says nothing -- and recording False there is a positive claim of
        # incapability built on a budget decision of ours. This is the exact
        # error the tri-state exists to prevent, made one level up: the 1.0.0
        # probe recorded False for four models on 2026-08-28 without checking.
        reason = _finish_reason(body)
        if reason == "length":
            return None, ("reply was cut off at max_tokens before any text was "
                          "emitted, so this establishes nothing")
        if not (body or {}).get("choices"):
            # No `choices` at all. If the body is a recognisable NON-CHAT
            # success shape -- an embedding or reranking response -- that IS
            # the answer: this endpoint does not serve chat completions, and
            # leaving it UNKNOWN would re-probe an embedder forever. A body
            # that fits no shape we know is genuinely unreadable, and
            # unreadable is not a finding.
            for shape, label in (("data", "an embedding/reranking response"),
                                 ("embedding", "an embedding response"),
                                 ("rankings", "a reranking response")):
                if shape in (body or {}):
                    return False, f"answered with {label}, not a chat completion"
            return None, ("reply carried no choices and matched no shape this "
                          "probe can read; nothing was established")
        return False, (f"returned no text in any of {', '.join(TEXT_FIELDS)} "
                       f"(finish_reason={reason or 'unstated'})")


class StructuredOutputProbe(Probe):
    """
    Will it produce a parseable JSON object with the key that was asked for?

    Deliberately does NOT use a provider's `response_format` parameter: some
    hosts accept the parameter and ignore it, and Quintek's own prompts ask
    for JSON in the prompt. The probe should test the path production uses.
    """

    def __init__(self):
        super().__init__(STRUCTURED_OUTPUT, max_tokens=64, approx_input_tokens=60)

    def payload(self, model_id: str) -> dict:
        return _chat(model_id,
                     'Reply with ONLY a JSON object, no prose and no code fence, '
                     'exactly of the form {"answer": "B"}. Use the letter B.',
                     max_tokens=64)

    def verify(self, body, text):
        blob = _first_json_object(text)
        if blob is None:
            return False, f"no JSON object in the reply: {text.strip()[:80]!r}"
        if "answer" not in blob:
            return False, f"JSON object lacked the requested key: {sorted(blob)[:4]}"
        return True, f'parsed {{"answer": {blob["answer"]!r}}}'


class ReasoningProbe(Probe):
    """
    A floor, not a grade.

    Two arithmetic steps with one verifiable answer. A model that gets it
    wrong has not been shown to be a bad reasoner -- it has been shown not to
    clear the floor this role needs, which is the only claim being recorded.
    The measured failure this guards against was concrete: llama-3.1-8b
    approved a question that contradicted its own source passage, which is why
    `ROLE_REQUIREMENTS["validation"]` demands reasoning at all.
    """

    def __init__(self):
        super().__init__(REASONING, max_tokens=96, approx_input_tokens=80)

    def payload(self, model_id: str) -> dict:
        return _chat(model_id,
                     "A ward has 3 bays. Each bay has 4 beds. Two beds in the ward "
                     "are out of service. Reply with ONLY a JSON object of the form "
                     '{"beds": N} giving the number of usable beds.',
                     max_tokens=96)

    def verify(self, body, text):
        blob = _first_json_object(text)
        if blob is None or "beds" not in blob:
            return None, ("reply was not the requested JSON, so this says nothing "
                          "about reasoning: " + repr(text.strip()[:80]))
        try:
            value = int(blob["beds"])
        except (TypeError, ValueError):
            return None, f"non-numeric answer {blob['beds']!r}"
        if value == 10:
            return True, "3x4-2 = 10, answered correctly"
        return False, f"answered {value}, expected 10"


class ToolCallingProbe(Probe):
    """Offer one tool and see whether the reply is a tool call."""

    def __init__(self):
        super().__init__(TOOL_CALLING, max_tokens=96, approx_input_tokens=120)

    def payload(self, model_id: str) -> dict:
        return _chat(
            model_id, "What is the weather in Kolkata? Use the tool.", max_tokens=96,
            tools=[{"type": "function", "function": {
                "name": "get_weather",
                "description": "Current weather for a city",
                "parameters": {"type": "object",
                               "properties": {"city": {"type": "string"}},
                               "required": ["city"]}}}],
            tool_choice="auto")

    def verify(self, body, text):
        choices = (body or {}).get("choices") or []
        message = (choices[0].get("message") if choices else None) or {}
        calls = message.get("tool_calls") or []
        if calls:
            name = (calls[0].get("function") or {}).get("name", "?")
            return True, f"emitted a tool_call to {name!r}"
        return False, "answered in prose with a tool available"


class VisionProbe(Probe):
    """
    Send a solid red image and ask what colour it is.

    Verifiable without trusting the model's self-description, and it fails
    honestly: a text-only model either errors (inconclusive, because the error
    may be the image encoding) or describes nothing red.
    """

    def __init__(self):
        super().__init__(VISION, max_tokens=32, approx_input_tokens=100)

    def payload(self, model_id: str) -> dict:
        return _chat(model_id, [
            {"type": "text",
             "text": "What colour fills this image? Reply with one word."},
            {"type": "image_url",
             "image_url": {"url": f"data:image/png;base64,{RED_PNG_B64}"}},
        ], max_tokens=32)

    def verify(self, body, text):
        lowered = text.lower()
        if "red" in lowered:
            return True, "identified the image as red"
        if not lowered.strip():
            return None, "empty reply, so the image was not shown to have been seen"
        return False, f"did not identify the image: {text.strip()[:60]!r}"


class LongContextProbe(Probe):
    """
    A needle at a known depth. Opt-in: this is the only probe here whose input
    is measured in thousands of tokens rather than tens.
    """

    def __init__(self, *, filler_tokens: int = 8_000, needle: str = "QX-4417"):
        super().__init__(LONG_CONTEXT, max_tokens=32,
                         approx_input_tokens=filler_tokens, opt_in=True)
        self.filler_tokens = filler_tokens
        self.needle = needle

    def payload(self, model_id: str) -> dict:
        filler = "The ward round proceeded without incident. " * (self.filler_tokens // 8)
        half = len(filler) // 2
        body = f"{filler[:half]}\nThe access code is {self.needle}.\n{filler[half:]}"
        return _chat(model_id,
                     f"{body}\n\nWhat is the access code? Reply with the code only.",
                     max_tokens=32)

    def verify(self, body, text):
        if self.needle.lower() in text.lower():
            return True, f"recalled the needle at ~{self.filler_tokens} tokens"
        return False, f"did not recall the needle at ~{self.filler_tokens} tokens"


#: Every probe this module knows how to run. A role names capabilities; this
#: maps them to requests. Nothing outside this table decides what a capability
#: means, so "what does structured_output claim" has one answer.
PROBES: dict[str, Probe] = {
    p.capability: p for p in (TextOutputProbe(), StructuredOutputProbe(),
                              ReasoningProbe(), ToolCallingProbe(), VisionProbe(),
                              LongContextProbe())
}

#: Every probe needs the model to accept text and answer, so a model that
#: fails this one cannot be shown anything else. Run first, and short-circuit.
PREREQUISITE = TEXT_OUTPUT


@dataclass
class ProbeRun:
    """What one model's probe pass established, and what it cost."""

    key: str
    outcomes: dict = field(default_factory=dict)
    calls: int = 0
    inconclusive: list = field(default_factory=list)
    stopped_early: str = ""
    #: What the probe learned about REACHABILITY, as opposed to capability.
    #: A capability pass that gets a 410 has discovered a retirement, and
    #: version 1.0.0 threw that away: five models answered with a
    #: MODEL_RETIRED-classified error on 2026-08-28 and stayed AVAILABLE in
    #: the registry, because nothing carried the finding across.
    availability: dict = field(default_factory=dict)

    def claims(self) -> dict:
        return {name: outcome.as_claim() for name, outcome in self.outcomes.items()}

    def as_dict(self) -> dict:
        return {"key": self.key, "calls": self.calls,
                "outcomes": {n: o.as_dict() for n, o in self.outcomes.items()},
                "inconclusive": list(self.inconclusive),
                "stopped_early": self.stopped_early,
                "availability": dict(self.availability)}


def run_probes(source, model_id: str, capabilities, *, transport: Transport | None = None,
               env: dict | None = None, timeout: float = DEFAULT_TIMEOUT,
               include_opt_in: bool = False) -> ProbeRun:
    """
    Probe one model for the named capabilities.

    Order is fixed: the text-output prerequisite first, and if it fails or is
    inconclusive nothing else is sent. There is no point paying for a vision
    probe against something that did not answer a one-word question, and a
    string of inconclusive results from one dead endpoint looks like a model
    with many missing capabilities rather than one unreachable model.
    """
    from .provider_catalogue import UrllibTransport

    transport = transport or UrllibTransport()
    run = ProbeRun(key=f"{source.name}:{model_id}")
    wanted = [name for name in capabilities if name in PROBES]
    unknown = sorted(set(capabilities) - set(PROBES))
    if unknown:
        raise ValueError(
            f"no probe defines {unknown}. A capability with no probe cannot be "
            f"observed, and requiring it would silently disqualify every model. "
            f"Probes exist for: {', '.join(sorted(PROBES))}")
    ordered = ([PREREQUISITE] if PREREQUISITE in wanted or wanted else []) + \
              [n for n in wanted if n != PREREQUISITE]

    try:
        headers, _used = source.headers(env)
    except MissingCredential as exc:
        run.stopped_early = str(exc)
        run.inconclusive = list(wanted)
        return run

    for name in ordered:
        probe = PROBES[name]
        if probe.opt_in and not include_opt_in:
            continue
        outcome = _run_one(source, model_id, probe, transport=transport,
                           headers=headers, timeout=timeout)
        run.calls += 1
        # First real answer of the pass decides what we learned about
        # reachability. Later probes cannot improve on it and a later failure
        # should not overwrite a successful first contact.
        if not run.availability:
            run.availability = {"http_status": outcome.http_status,
                                "provider_status": outcome.provider_status,
                                "latency_ms": outcome.latency_ms,
                                "detail": outcome.evidence}
        if name in wanted or name == PREREQUISITE:
            run.outcomes[name] = outcome
        if not outcome.conclusive:
            run.inconclusive.append(name)
        if name == PREREQUISITE and outcome.value is not True:
            run.stopped_early = (
                f"the text-output prerequisite came back {outcome.value!r} "
                f"({outcome.evidence}); nothing further was sent")
            for remaining in ordered[ordered.index(name) + 1:]:
                if remaining not in run.inconclusive:
                    run.inconclusive.append(remaining)
            break
    return run


def _run_one(source, model_id: str, probe: Probe, *, transport, headers,
             timeout: float) -> ProbeOutcome:
    response = transport.post_json(source.completions_url, headers=headers,
                                   payload=probe.payload(model_id), timeout=timeout)
    if not response.ok:
        status = classify(response.error or f"HTTP {response.status}",
                          http_status=response.status)
        # An availability failure is NOT a capability answer. This is the whole
        # reason `value` is tri-state.
        #
        # The status AND an excerpt of what the provider said are carried on
        # the outcome and into the stored claim. Version 1.0.0 recorded only
        # "probe could not run (MODEL_RETIRED)", which made it impossible to
        # audit afterwards whether the 410 was real -- and a terminal-sounding
        # classification nobody can check is worse than no classification.
        detail = (str(response.error) if response.error
                  else f"HTTP {response.status}")
        return ProbeOutcome(probe.capability, None,
                            f"probe could not run [{status}] "
                            f"HTTP {response.status}: {detail[:200]}",
                            http_status=response.status,
                            latency_ms=response.latency_ms, provider_status=status)
    try:
        body = json.loads(response.body)
    except ValueError:
        return ProbeOutcome(probe.capability, None, "reply was not JSON at all",
                            http_status=response.status,
                            latency_ms=response.latency_ms,
                            provider_status=ProviderStatus.INVALID_RESPONSE)
    text = _content_of(body)
    try:
        value, evidence = probe.verify(body, text)
    except Exception as exc:                       # a malformed reply, not a crash
        return ProbeOutcome(probe.capability, None,
                            f"verifier could not read the reply: "
                            f"{type(exc).__name__}: {exc}",
                            http_status=response.status,
                            latency_ms=response.latency_ms,
                            provider_status=ProviderStatus.INVALID_RESPONSE)
    return ProbeOutcome(probe.capability, value, evidence,
                        http_status=response.status, latency_ms=response.latency_ms,
                        provider_status=ProviderStatus.AVAILABLE)


#: Where a reply's text can live. `content` is the OpenAI-compatible field;
#: `reasoning_content` is what several NIM reasoning models fill instead, and
#: reading only the first records "emitted no text" for a model that emitted
#: plenty. Measured on the 2026-08-28 run: four models -- including
#: openai/gpt-oss-20b -- came back with an empty `content`.
TEXT_FIELDS = ("content", "reasoning_content", "text")


def _content_of(body: dict) -> str:
    choices = (body or {}).get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    parts = []
    for field in TEXT_FIELDS:
        value = message.get(field)
        if isinstance(value, list):        # some hosts return content parts
            parts.append("".join(part.get("text", "") for part in value
                                 if isinstance(part, dict)))
        elif isinstance(value, str):
            parts.append(value)
    return "".join(parts)


def _finish_reason(body: dict) -> str:
    choices = (body or {}).get("choices") or []
    return str((choices[0].get("finish_reason") if choices else "") or "")


def _first_json_object(text: str) -> dict | None:
    """
    The first balanced {...} in the reply, parsed.

    Scans for balance rather than regexing to the last brace: a model that
    emits prose after its JSON, or wraps it in a code fence, has still
    produced the object that was asked for, and failing it for the packaging
    would record a capability it demonstrably has as absent.
    """
    start = text.find("{")
    while start != -1:
        depth, in_string, escaped = 0, False, False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(text[start:index + 1])
                    except ValueError:
                        break
                    return parsed if isinstance(parsed, dict) else None
        start = text.find("{", start + 1)
    return None


def forecast(records, capabilities, *, include_opt_in: bool = False) -> dict:
    """
    What a probe pass will cost before it costs it, in the same spirit as
    `validator/forecast.py`: calls are exact, tokens are an estimate, and the
    difference is stated rather than blurred.
    """
    names = [PREREQUISITE] + [n for n in capabilities if n != PREREQUISITE]
    probes = [PROBES[n] for n in names if n in PROBES
              and (include_opt_in or not PROBES[n].opt_in)]
    per_model = len(probes)
    return {
        "models": len(records),
        "probes_per_model": per_model,
        "calls": len(records) * per_model,
        "approx_input_tokens": len(records) * sum(p.approx_input_tokens for p in probes),
        "max_output_tokens": len(records) * sum(p.max_tokens for p in probes),
        "probes": [p.capability for p in probes],
        "note": ("calls are exact; token figures are estimates and are here to "
                 "separate a trivial pass from an expensive one, not to bill"),
    }
