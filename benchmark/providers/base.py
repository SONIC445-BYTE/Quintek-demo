"""
Provider abstraction.

The runner must not know which model it is evaluating. Everything a scorecard
needs to reproduce a result travels in GenerationResponse.

No silent failure: a provider error is recorded as an error, never as an empty
answer that scores as wrong. Those are different events and conflating them
biases every accuracy figure downward in a way that looks like model weakness.

Retry/timeout: `timeout_seconds` is passed through to `_call` on every
attempt so a concrete provider can hand it to its own HTTP client (the
correct place to enforce a network timeout -- a wrapper-level alarm/thread
timeout cannot safely cancel an in-flight request in a generic way). The base
class owns the retry loop and the accounting: how many attempts were made,
and whether the final attempt still failed, both travel in the response so a
report never hides how many tries a number cost.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol


#: Where a chat reply's text can live, in priority order. `content` is the
#: OpenAI-compatible field; `reasoning_content` is what several NIM reasoning
#: models fill INSTEAD, leaving `content` null.
#:
#: Reading only `content` is not a cosmetic bug. `extract_json_object` regexes
#: whatever it is handed, so a null content raised
#: "TypeError: expected string or bytes-like object, got 'NoneType'" and the
#: item was recorded as a backend outage. Phase 0 on 2026-09-02 lost items to
#: exactly this against a reasoning candidate.
#:
#: This lives here, once, because the same bug was fixed in
#: `benchmark/capability_probe.py` days earlier and not here -- two call sites
#: with two answers to "where is the text" is how one of them stays wrong.
TEXT_FIELDS = ("content", "reasoning_content", "text")


def content_of(message: dict) -> str:
    """
    The text of one chat message, from wherever the host put it.

    Returns "" rather than None so a caller that regexes the result cannot
    crash on an empty reply -- an empty reply is a parse failure, which is a
    different and much better outcome than an exception.
    """
    parts = []
    for field in TEXT_FIELDS:
        value = (message or {}).get(field)
        if isinstance(value, list):          # some hosts return content parts
            parts.append("".join(p.get("text", "") for p in value
                                 if isinstance(p, dict)))
        elif isinstance(value, str):
            parts.append(value)
    return "".join(parts)


@dataclass
class GenerationRequest:
    item_id: str
    prompt: str
    system: str = ""
    max_tokens: int = 1024
    temperature: float = 0.0
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RetryPolicy:
    """
    Frozen, because `BaseProvider.retry_policy` is a class attribute and
    therefore ONE object shared by every provider that has not been given its
    own. While this was mutable, `provider.retry_policy.max_retries = 0`
    silently retuned every other provider in the process -- including ones
    already constructed -- and the manifest went on recording the mutated
    value for all of them, so the record looked consistent while the run was
    not.

    That matters most where it is hardest to notice: an experiment set freezes
    `max_retries`, and the spend forecast multiplies planned calls by
    `1 + max_retries`. A mid-run mutation would leave the frozen number and the
    actual behaviour disagreeing with nothing to show for it.

    To change a policy, assign a new one -- `provider.retry_policy =
    RetryPolicy(max_retries=0, timeout_seconds=5.0)` -- which creates an
    instance attribute and cannot leak.
    """

    max_retries: int = 2
    timeout_seconds: float = 30.0
    backoff_base_seconds: float = 0.0


@dataclass
class GenerationResponse:
    item_id: str
    raw_output: str
    parsed: dict | None
    provider: str
    model: str
    model_version: str
    latency_ms: float
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: str | None = None
    attempts: int = 1
    request_metadata: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None

    def as_dict(self) -> dict:
        return {
            "item_id": self.item_id, "raw_output": self.raw_output, "parsed": self.parsed,
            "provider": self.provider, "model": self.model, "model_version": self.model_version,
            "latency_ms": round(self.latency_ms, 2), "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens, "error": self.error, "attempts": self.attempts,
            "request_metadata": self.request_metadata,
        }


class ModelProvider(Protocol):
    name: str
    model: str
    model_version: str
    model_family: str

    def generate(self, request: GenerationRequest) -> GenerationResponse: ...


class BaseProvider:
    name = "base"
    model = "unset"
    model_version = "unset"
    model_family = "unset"
    # Whether this provider talks to a model. Test doubles and oracles set it
    # False, and the evaluation record refuses to count their runs as
    # measurements. Defaulting to True is deliberate: a new real adapter is
    # counted without anyone remembering to opt in, and a new fake has to say
    # so, which is the direction that fails safe.
    is_model = True
    is_oracle = False
    retry_policy: RetryPolicy = RetryPolicy()

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        """
        Retries up to `retry_policy.max_retries` additional times on any
        exception from `_call` (including a concrete provider raising
        TimeoutError once its own client-level timeout fires). The response
        records every attempt count; a value that only succeeded on retry is
        not the same evidence as one that succeeded first try, and a report
        that discards that distinction hides instability.
        """
        start = time.perf_counter()
        attempt = 0
        raw, parsed, tin, tout, err = "", None, None, None, None
        while True:
            attempt += 1
            try:
                raw, parsed, tin, tout = self._call(request, self.retry_policy.timeout_seconds)
                err = None
                break
            except Exception as exc:
                raw, parsed, tin, tout = "", None, None, None
                err = f"{type(exc).__name__}: {exc}"
                if attempt > self.retry_policy.max_retries:
                    break
                if self.retry_policy.backoff_base_seconds:
                    time.sleep(self.retry_policy.backoff_base_seconds * attempt)
        return GenerationResponse(
            item_id=request.item_id, raw_output=raw, parsed=parsed,
            provider=self.name, model=self.model, model_version=self.model_version,
            latency_ms=(time.perf_counter() - start) * 1000,
            input_tokens=tin, output_tokens=tout, error=err, attempts=attempt,
            request_metadata={"temperature": request.temperature,
                              "max_tokens": request.max_tokens},
        )

    def _call(self, request: GenerationRequest, timeout_seconds: float):
        """
        Concrete providers implement this and pass `timeout_seconds` to their
        own client (e.g. `requests.post(..., timeout=timeout_seconds)`),
        raising on timeout or transport failure so the retry loop above can
        act on it.
        """
        raise NotImplementedError

    def manifest(self) -> dict:
        return {"provider": self.name, "model_id": self.model,
                "model_version": self.model_version, "model_family": self.model_family,
                "retry_policy": {"max_retries": self.retry_policy.max_retries,
                                 "timeout_seconds": self.retry_policy.timeout_seconds}}
