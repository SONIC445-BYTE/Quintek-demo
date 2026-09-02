"""
Fetching what a provider currently offers, and finding out what actually
answers.

`benchmark/candidates.py` could always READ a discovery snapshot. Nothing in
this repository ever WROTE one -- `discovery/*.json` were produced out of band
in an earlier session and committed, which is why the registry still named a
model that had been dead for two days. This module is the missing producer.

TWO CALLS, TWO MEANINGS
-----------------------
`fetch_catalogue` asks the provider what it lists. It is one cheap request and
it establishes nothing about whether a model can be called.

`probe` sends the smallest real completion the provider will accept and
records what came back. That is the only thing that may set `AVAILABLE`.

Measured on this account, 2026-08-28, which is why the two are separate calls
with separate meanings:

    listed and dead      meta/llama-3.1-8b-instruct        410 end-of-life
    listed and 404       mistralai/mistral-7b-instruct     404 not for account
    listed and hanging   openai/gpt-oss-120b               timeout at 60s
    unlisted and alive   nvidia/nemotron-3-nano-30b-a3b    200 in 0.43s

CREDENTIALS
-----------
Read from the environment by NAME, at call time, and put into a header. Never
stored on an object, never returned, never written to a snapshot. The registry
records `credential_ref` -- the variable's name -- which is enough to
reproduce a run and useless to anyone who takes the file.

TRANSPORT IS INJECTED
---------------------
Every network call goes through a `Transport`. The default is stdlib
`urllib`, matching the rest of this harness. Tests pass a fake, so the whole
discovery and reconciliation path is exercised without spending a provider
credit -- which matters because the failures worth testing (410, 402, 429, a
model reappearing) are precisely the ones you cannot summon on demand.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .discovery import Observation

#: Smallest completion that still exercises the real inference path. One token
#: out, temperature 0, a two-character prompt. Enough to separate 200 from 410
#: from 402 from a hang; not enough to be worth budgeting for individually.
PROBE_MAX_TOKENS = 1
PROBE_PROMPT = "ok"
DEFAULT_TIMEOUT = 60.0


class MissingCredential(RuntimeError):
    """The environment variable naming this provider's key is not set."""


@dataclass
class HttpResult:
    """
    What came back, including the ways nothing came back.

    `status` is None when the request never produced an HTTP response --
    a timeout, a DNS failure, a blocked CONNECT. That is a different fact from
    a 500, and `provider_status.classify` needs both to tell them apart.
    """

    status: int | None = None
    body: str = ""
    latency_ms: float | None = None
    error: BaseException | str | None = None

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300


class Transport:
    """The seam. Anything with these two methods will do."""

    def get(self, url: str, *, headers: dict, timeout: float) -> HttpResult:
        raise NotImplementedError

    def post_json(self, url: str, *, headers: dict, payload: dict,
                  timeout: float) -> HttpResult:
        raise NotImplementedError


class UrllibTransport(Transport):
    """stdlib only, for the same reason every other network-facing module here is."""

    def _send(self, request: urllib.request.Request, timeout: float) -> HttpResult:
        start = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
                return HttpResult(status=response.status, body=body,
                                  latency_ms=(time.monotonic() - start) * 1000.0)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return HttpResult(status=exc.code, body=body,
                              latency_ms=(time.monotonic() - start) * 1000.0,
                              error=f"HTTP {exc.code}: {body[:300]}")
        except Exception as exc:                      # timeout, DNS, TLS, proxy
            return HttpResult(status=None, body="",
                              latency_ms=(time.monotonic() - start) * 1000.0, error=exc)

    def get(self, url: str, *, headers: dict, timeout: float) -> HttpResult:
        return self._send(urllib.request.Request(url, headers=headers), timeout)

    def post_json(self, url: str, *, headers: dict, payload: dict,
                  timeout: float) -> HttpResult:
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={**headers, "Content-Type": "application/json"})
        return self._send(request, timeout)


@dataclass
class CatalogueSource:
    """
    One provider's discovery endpoints and how to read them.

    Adding a provider is adding one of these, not editing a selection
    function. Nothing downstream branches on `name`.
    """

    name: str
    catalogue_url: str
    completions_url: str
    api_key_env: str = ""
    #: Providers that expose only `{id, object, created, owned_by}`. Their
    #: capability and price fields stay unknown rather than defaulting to
    #: False, which would be inventing a measurement.
    bare_catalogue: bool = True
    extra_headers: dict = field(default_factory=dict)

    def headers(self, env: dict | None = None) -> dict:
        """
        Built fresh per call, from the environment, by name.

        Raises rather than sending an unauthenticated request: a 401 recorded
        as the model's availability, when the real cause is an unset variable,
        is a wrong fact that persists in the registry.
        """
        env = os.environ if env is None else env
        headers = {"Accept": "application/json", **self.extra_headers}
        if not self.api_key_env:
            return headers
        key = env.get(self.api_key_env)
        if not key:
            raise MissingCredential(
                f"{self.api_key_env} is not set, so {self.name} cannot be discovered. "
                "The key is read from the environment by name and never stored; set "
                "the variable rather than passing a value.")
        headers["Authorization"] = f"Bearer {key}"
        return headers

    def parse(self, body: str) -> list[Observation]:
        """OpenAI-compatible `{"data": [...]}`; overridden where it is not."""
        payload = json.loads(body)
        rows = payload.get("data") if isinstance(payload, dict) else payload
        return [self.observation(row) for row in (rows or [])
                if isinstance(row, dict) and row.get("id")]

    def observation(self, row: dict) -> Observation:
        return Observation(
            provider=self.name, model_id=row["id"],
            family=_family_of(row["id"]),
            source=self.catalogue_url,
            # Nothing is claimed. A bare catalogue exposes an id and nothing
            # else, and `{}` capabilities means unknown, which `eligible()`
            # treats as "not yes" without treating it as "never".
            #
            # `price_stated=True` with a None price is deliberate and is not
            # the same as the sentinel case: this provider does not publish
            # prices at all (-> UNKNOWN), where OpenRouter publishes `-1` for
            # a model it will price at request time (-> UNPRICED). Both fail a
            # budget ceiling; only one of them means the provider has a number
            # and is declining to commit to it.
            capabilities={}, context_window=None,
            input_price=None, output_price=None, price_stated=True)

    def probe_payload(self, model_id: str) -> dict:
        return {"model": model_id, "messages": [{"role": "user", "content": PROBE_PROMPT}],
                "max_tokens": PROBE_MAX_TOKENS, "temperature": 0.0}


def _family_of(model_id: str) -> str:
    """
    The vendor path segment, or the leading token of a flat id.

    Used for diversity caps and judge independence, never for selection: two
    models from one family are two observations of one approach, and a
    shortlist that does not know that fills up with checkpoints.
    """
    base = model_id.split(":")[0]
    if "/" in base:
        return base.split("/")[0]
    return base.split("-")[0]


#: The providers this repository has adapters for. Editing this adds a
#: provider; it does not choose one, and nothing downstream reads a name from
#: it to make a decision.
SOURCES = {
    "nvidia": CatalogueSource(
        name="nvidia",
        catalogue_url="https://integrate.api.nvidia.com/v1/models",
        completions_url="https://integrate.api.nvidia.com/v1/chat/completions",
        api_key_env="NVIDIA_API_KEY"),
    "cerebras": CatalogueSource(
        name="cerebras",
        catalogue_url="https://api.cerebras.ai/v1/models",
        completions_url="https://api.cerebras.ai/v1/chat/completions",
        api_key_env="CEREBRAS_API_KEY"),
}


@dataclass
class CatalogueResult:
    provider: str
    observations: list = field(default_factory=list)
    ok: bool = False
    error: str = ""
    http_status: int | None = None
    latency_ms: float | None = None

    def as_dict(self) -> dict:
        return {"provider": self.provider, "count": len(self.observations),
                "ok": self.ok, "error": self.error, "http_status": self.http_status,
                "latency_ms": self.latency_ms}


def fetch_catalogue(source: CatalogueSource, *, transport: Transport | None = None,
                    env: dict | None = None,
                    timeout: float = DEFAULT_TIMEOUT) -> CatalogueResult:
    """
    What the provider lists, right now.

    A failure here is returned, not raised: one unreachable provider must not
    abort a discovery run across the others, and "we could not look" is a
    different fact from "the catalogue is empty" -- which is why an errored
    result carries zero observations AND `ok=False`, and the caller must not
    reconcile against it. Reconciling an empty list would mark every model
    that provider has ever served as absent.
    """
    transport = transport or UrllibTransport()
    result = CatalogueResult(provider=source.name)
    try:
        headers = source.headers(env)
    except MissingCredential as exc:
        result.error = str(exc)
        return result
    response = transport.get(source.catalogue_url, headers=headers, timeout=timeout)
    result.http_status = response.status
    result.latency_ms = response.latency_ms
    if not response.ok:
        result.error = _text_of(response)
        return result
    try:
        result.observations = source.parse(response.body)
    except (ValueError, KeyError, TypeError) as exc:
        result.error = f"catalogue did not parse: {type(exc).__name__}: {exc}"
        return result
    result.ok = True
    return result


@dataclass
class ProbeResult:
    key: str
    http_status: int | None = None
    error: BaseException | str | None = None
    latency_ms: float | None = None

    def as_dict(self) -> dict:
        return {"key": self.key, "http_status": self.http_status,
                "error": _text_of_error(self.error), "latency_ms": self.latency_ms}


def probe(source: CatalogueSource, model_id: str, *, transport: Transport | None = None,
          env: dict | None = None, timeout: float = DEFAULT_TIMEOUT) -> ProbeResult:
    """
    One minimal completion, to find out whether this model answers.

    Deliberately the same interface Quintek uses in production -- a chat
    completion, not a metadata endpoint -- because the whole point is that the
    metadata endpoint already said yes and was wrong.
    """
    transport = transport or UrllibTransport()
    key = f"{source.name}:{model_id}"
    try:
        headers = source.headers(env)
    except MissingCredential as exc:
        return ProbeResult(key=key, error=str(exc))
    response = transport.post_json(source.completions_url, headers=headers,
                                   payload=source.probe_payload(model_id),
                                   timeout=timeout)
    return ProbeResult(key=key, http_status=response.status,
                       error=None if response.ok else _text_of(response),
                       latency_ms=response.latency_ms)


def _text_of(response: HttpResult) -> BaseException | str:
    if isinstance(response.error, BaseException):
        return response.error
    if response.error:
        return str(response.error)
    if response.status is not None:
        return f"HTTP {response.status}: {response.body[:300]}"
    return "no response"


def _text_of_error(error) -> str:
    if isinstance(error, BaseException):
        return f"{type(error).__name__}: {error}"
    return str(error) if error else ""
