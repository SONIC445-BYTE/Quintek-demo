"""
NVIDIA NIM provider adapter.

NVIDIA's hosted NIM API is OpenAI-compatible (POST /v1/chat/completions),
which keeps this adapter small: it only has to build the request body and
unpack the response into the (raw, parsed, input_tokens, output_tokens)
tuple `BaseProvider.generate` expects. Retry/timeout/attempt-accounting all
come from `BaseProvider` for free -- this module's only job is the one HTTP
call.

No new dependency: built on `urllib.request` (stdlib), matching every other
network-facing piece of this harness (see analytics_api.py's module
docstring for why that's a deliberate choice, not an oversight).

The API key is read from an environment variable ONLY, at call time, never
accepted as a constructor literal and never written to disk by this module.
A key committed to source control is the single most avoidable security
failure a provider adapter can have.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import replace
import urllib.error
import urllib.request

from .base import BaseProvider, GenerationRequest, content_of, RateLimited

NIM_CHAT_COMPLETIONS_URL = "https://integrate.api.nvidia.com/v1/chat/completions"

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


def extract_json_object(text: str) -> dict | None:
    """
    Default response parser: find the first {...} block in the model's
    reply and parse it. Benchmark prompts ask candidates to answer in JSON
    (matching scorers/deterministic.py's expectations, e.g. {"answer": "B"});
    a reply that doesn't contain one is correctly treated as unparseable
    (classify_case's "invalid" bucket in analytics.py), not as a wrong
    answer -- those are different failure modes.
    """
    match = _JSON_OBJECT.search(text)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _infer_family(model_id: str) -> str:
    """'meta/llama-3.1-70b-instruct' -> 'llama'; 'openai/gpt-oss-120b' -> 'gpt-oss';
    used only for judge-independence family comparison (integrity.py), so it
    only needs to be consistent, not canonical."""
    base = model_id.split("/")[-1].lower()
    for family in ("llama", "gpt-oss", "gemma", "mixtral", "mistral", "nemotron", "phi", "qwen"):
        if family in base:
            return family
    return base.split("-")[0]


#: Default per-attempt timeout for NIM inference, in seconds.
#:
#: `RetryPolicy`'s own default is 30s, which suits a fast dedicated endpoint
#: and is far too tight for this one. Measured against
#: integrate.api.nvidia.com on 2026-08-19: `GET /v1/models` returned in 0.6s,
#: while an 8-token `POST /v1/chat/completions` on
#: meta/llama-3.3-70b-instruct took **72.9s** wall clock. At 30s every attempt
#: timed out, and a batch reported 8/8 failures at ~91s each (3 attempts x
#: 30s) -- a result that looks like a broken model but was a broken timeout.
#:
#: This is a shared, free-tier, queue-behind-other-tenants endpoint; latency
#: is dominated by queueing, not by token generation. Override per instance
#: (`provider.retry_policy = replace(provider.retry_policy, timeout_seconds=...)`)
#: or globally with
#: NVIDIA_TIMEOUT_SECONDS for a dedicated deployment where 30s is realistic.
NIM_DEFAULT_TIMEOUT_SECONDS = 180.0

#: Sent on every request. Not cosmetic: see the header block in `_call`.
USER_AGENT = "Quintek-Validator/0.2 (+https://github.com/SONIC445-BYTE/Quintek-demo)"



def _retry_after(headers) -> float | None:
    """
    The host's own wait, in seconds, or None.

    `Retry-After` is either a count of seconds or an HTTP date. Only the
    numeric form is honoured: a date needs the server's clock to agree with
    ours, and a skewed clock would produce either a busy-wait or a sleep of
    hours. Unparseable means None, which falls back to exponential backoff --
    the conservative direction.
    """
    if headers is None:
        return None
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None:
        return None
    try:
        seconds = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


class NVIDIAProvider(BaseProvider):
    name = "nvidia"

    def __init__(
        self,
        model_id: str,
        model_version: str = "unknown",
        *,
        api_key_env: str = "NVIDIA_API_KEY",
        system_prompt: str = "",
        parse_response=None,
        model_family: str | None = None,
        base_url: str = NIM_CHAT_COMPLETIONS_URL,
        timeout_seconds: float | None = None,
        max_retries: int | None = None,
        name: str | None = None,
    ):
        # WHICH HOST THIS ACTUALLY IS.
        #
        # This class is the OpenAI-compatible chat client every host in the
        # registry is built on -- Groq, Fireworks, Together, Cerebras and
        # OpenRouter all construct it with a different base_url. `name` was a
        # class attribute reading "nvidia" for all of them, so a Groq call was
        # recorded as an NVIDIA call in the execution log, in the freeze
        # manifest and in every run artifact, and its errors said "NVIDIA NIM"
        # while talking to api.groq.com. A record that misattributes the host
        # is worse than no record: it sends the next reader to the wrong
        # vendor's status page.
        self.name = name or type(self).name
        self.model = model_id
        self.model_version = model_version
        self.model_family = model_family or _infer_family(model_id)
        self.api_key_env = api_key_env
        self.system_prompt = system_prompt
        self.base_url = base_url
        self._parse = parse_response or extract_json_object
        # Own the policy per instance rather than mutating the class-level
        # default, so one slow provider cannot silently retune every other.
        resolved = timeout_seconds
        if resolved is None:
            resolved = float(os.environ.get("NVIDIA_TIMEOUT_SECONDS",
                                            NIM_DEFAULT_TIMEOUT_SECONDS))
        policy = replace(BaseProvider.retry_policy, timeout_seconds=resolved)
        # Externally settable, because the spend forecast multiplies planned
        # calls by (1 + max_retries) and an experiment freezes the number. A
        # policy that can only be changed by editing code is one the frozen
        # configuration cannot describe.
        if max_retries is not None:
            if max_retries < 0:
                raise ValueError("max_retries cannot be negative")
            policy = replace(policy, max_retries=max_retries)
        self.retry_policy = policy

    def _host_label(self) -> str:
        """`provider @ host` for an error message, so the record names the host
        that actually refused rather than the class that made the call."""
        try:
            host = self.base_url.split("/")[2]
        except IndexError:
            host = self.base_url
        return f"{self.name} ({host})"

    def _api_key(self) -> str:
        key = os.environ.get(self.api_key_env)
        if not key:
            raise RuntimeError(
                f"{self.api_key_env} is not set in the environment. The key is "
                "never read from a file or constructor argument by design -- "
                "export it before running."
            )
        return key

    def _call(self, request: GenerationRequest, timeout_seconds: float):
        messages = []
        system = request.system or self.system_prompt
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": request.prompt})

        body = json.dumps({
            "model": self.model,
            "messages": messages,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "stream": False,
        }).encode("utf-8")

        http_request = urllib.request.Request(
            self.base_url, data=body, method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key()}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                # urllib sends "Python-urllib/3.x" by default, and hosts behind
                # a bot-protection edge routinely refuse it with a 403 whose
                # body is the EDGE's error document rather than the API's. That
                # reads as an auth or permission failure and is neither. An
                # honest identifying agent is the fix.
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(http_request, timeout=timeout_seconds) as resp:
                raw_bytes = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if exc.code == 429:
                # Not a transport failure: the host is well and is telling us
                # our pace is wrong. Raised as its own type so the retry loop
                # waits instead of asking again at once, and so the outage
                # record can say "rate limited" rather than "the backend
                # failed" -- which would send a reader looking at the host.
                raise RateLimited(
                    f"{self._host_label()} HTTP 429 rate limited: {detail[:300]}",
                    retry_after=_retry_after(exc.headers)) from exc
            # 5xx and the rest are what the retry loop exists for; raising
            # lets that loop do its job.
            raise RuntimeError(
                f"{self._host_label()} HTTP {exc.code}: {detail[:500]}") from exc
        except urllib.error.URLError as exc:
            raise TimeoutError(
                f"{self._host_label()} request failed: {exc.reason}") from exc

        envelope = raw_bytes.decode("utf-8")
        payload = json.loads(envelope)
        choice = payload["choices"][0]
        # Not choice["message"]["content"]: a reasoning model leaves that null
        # and puts its reply in `reasoning_content`, and handing None to the
        # JSON extractor raises rather than failing to parse. See
        # `providers/base.content_of`.
        message = choice.get("message") or {}
        content = content_of(message)
        if choice.get("finish_reason") == "length" \
                and not (message.get("content") or "").strip():
            # Cut off mid-thought, with the answer never emitted. What is left
            # is reasoning prose, and `extract_json` takes the first balanced
            # object out of whatever it is handed -- so a quoted fragment like
            # {"A": "..."} inside the thinking got read as the model's answer,
            # and an item was flagged on the strength of it. An unfinished
            # reply is an outage. Returning "" makes it parse to None, which
            # is exactly how every layer already reports "nothing was checked".
            content = ""
        usage = payload.get("usage") or {}
        tin = usage.get("prompt_tokens")
        tout = usage.get("completion_tokens")
        parsed = self._parse(content)
        # raw_output IS THE MODEL'S REPLY, not the HTTP body that carried it.
        #
        # Every consumer assumes this. `validator/grounding.extract_json` takes
        # the first balanced JSON object out of `response.raw_output`, and the
        # HTTP envelope is itself a balanced JSON object -- so returning the
        # envelope handed the validator `{"id": "chatcmpl-...", "choices":
        # [...]}` and it read that as the model's answer. `supported` is absent
        # from an envelope, so EVERY item was flagged
        # `not_answerable_from_passage`: 91 of 94 grounding calls in Phase 0 on
        # 2026-09-03, giving specificity 0%, sensitivity 100% and a
        # discrimination rate of 0% -- a validator that flags everything,
        # measuring nothing.
        #
        # The contract was never ambiguous: `validator/scripted.py` returns
        # `json.dumps(reply)`. Only this adapter disagreed, and no test
        # compared the two, which is why 1274 green tests sat on top of it.
        return content, parsed, tin, tout
