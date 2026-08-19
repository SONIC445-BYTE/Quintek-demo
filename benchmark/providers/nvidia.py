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

from .base import BaseProvider, GenerationRequest

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
#: (`provider.retry_policy.timeout_seconds = ...`) or globally with
#: NVIDIA_TIMEOUT_SECONDS for a dedicated deployment where 30s is realistic.
NIM_DEFAULT_TIMEOUT_SECONDS = 180.0


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
    ):
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
        self.retry_policy = replace(BaseProvider.retry_policy,
                                    timeout_seconds=resolved)

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
            },
        )
        try:
            with urllib.request.urlopen(http_request, timeout=timeout_seconds) as resp:
                raw_bytes = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            # 429/5xx are exactly what the retry loop in BaseProvider.generate
            # exists for; raising lets that loop do its job.
            raise RuntimeError(f"NVIDIA NIM HTTP {exc.code}: {detail[:500]}") from exc
        except urllib.error.URLError as exc:
            raise TimeoutError(f"NVIDIA NIM request failed: {exc.reason}") from exc

        raw = raw_bytes.decode("utf-8")
        payload = json.loads(raw)
        choice = payload["choices"][0]
        content = choice["message"]["content"]
        usage = payload.get("usage") or {}
        tin = usage.get("prompt_tokens")
        tout = usage.get("completion_tokens")
        parsed = self._parse(content)
        return raw, parsed, tin, tout
