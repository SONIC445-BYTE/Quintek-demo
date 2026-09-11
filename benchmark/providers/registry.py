"""
Which provider serves a call, resolved by name rather than by editing code.

The scripted provider has done its job: it proved the pipeline carries data
end to end. Keeping it is not the problem -- the problem is that the choice
between it and a real model lived in whichever `provider_factory` lambda a
caller happened to write. That is a boundary you cannot audit, and it is how
a test double reaches production.

So the boundary is a name:

    AIEngine
       |
       v
    build_provider(spec)          <- one place, resolvable, auditable
       |
       +-- "scripted"             deterministic, no network, no key
       +-- "nvidia"               NVIDIA NIM
       +-- "openai-compatible"    any /v1/chat/completions endpoint
       +-- "local"                the same, pointed at localhost

Two rules the design turns on:

  * **A provider that cannot run says so at construction, not at call time.**
    A missing API key discovered on the first learner request is an outage. A
    missing API key discovered when the server starts is a configuration
    error, which is a much better thing to have.

  * **`scripted` is never the fallback.** If a spec names a real provider and
    it cannot be built, that is an error. Silently degrading to a scripted
    model would make fabricated answers indistinguishable from real ones --
    the same defect as the frontend fixtures, one layer down.

A spec is a small dict, so it can come from JSON without a parser:

    {"provider": "nvidia", "model_id": "meta/llama-3.3-70b-instruct",
     "model_version": "2024-12", "api_key_env": "NVIDIA_API_KEY"}
"""

from __future__ import annotations

import os
from typing import Any, Callable

BUILDERS: dict[str, Callable[..., Any]] = {}


class ProviderUnavailable(RuntimeError):
    """The named provider exists but cannot be constructed here, with a reason."""


class UnknownProvider(KeyError):
    """No builder is registered under that name."""


def register(name: str):
    def decorator(fn):
        BUILDERS[name] = fn
        return fn
    return decorator


def available() -> list[str]:
    return sorted(BUILDERS)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

@register("scripted")
def _scripted(spec: dict):
    """
    Deterministic, offline, keyless. For tests and for demonstrating the
    pipeline without spending anything.

    `answers` maps item_id -> reply. `replies` is a flat list returned in
    order, which is what a pipeline demonstration usually wants.
    """
    from .scripted import ScriptedProvider

    provider = ScriptedProvider(
        answers=spec.get("answers"),
        accuracy=float(spec.get("accuracy", 1.0)),
        seed=int(spec.get("seed", 7)),
    )
    # A model_id is carried through so two scripted candidates can be told
    # apart. Judge independence is enforced by comparing candidate ids, so
    # without this the acceptance script cannot be exercised offline -- the
    # generator and validator would always be the same configuration and it
    # would refuse to run, which is correct behaviour for the wrong reason.
    if spec.get("model_id"):
        provider.model = spec["model_id"]
    if spec.get("model_version"):
        provider.model_version = spec["model_version"]
    return provider


@register("nvidia")
def _nvidia(spec: dict):
    from .nvidia import NVIDIAProvider

    key_env = spec.get("api_key_env", "NVIDIA_API_KEY")
    if not os.environ.get(key_env):
        raise ProviderUnavailable(
            f"{key_env} is not set, so the NVIDIA provider cannot authenticate. Export the "
            f"key and retry; it is deliberately read from the environment and never stored "
            f"in this repository.")
    model_id = spec.get("model_id")
    if not model_id:
        raise ProviderUnavailable(
            "the nvidia provider needs an explicit model_id -- there is no sensible default, "
            "and guessing one would misattribute every result it produced")
    return NVIDIAProvider(
        model_id, model_version=spec.get("model_version", "unknown"),
        api_key_env=key_env, system_prompt=spec.get("system_prompt", ""),
        model_family=spec.get("model_family"),
        timeout_seconds=spec.get("timeout_seconds"),
        **({"base_url": spec["base_url"]} if spec.get("base_url") else {}))


@register("openai-compatible")
def _openai_compatible(spec: dict):
    """
    Any endpoint speaking `/v1/chat/completions`.

    Implemented by pointing the NVIDIA adapter at a different base_url,
    because that adapter is already an OpenAI-compatible chat client with the
    retry, timeout and token accounting this harness needs. Reusing it beats a
    near-identical second implementation that would drift.
    """
    from .nvidia import NVIDIAProvider

    base_url = spec.get("base_url")
    if not base_url:
        raise ProviderUnavailable(
            "openai-compatible needs a base_url, e.g. "
            "https://your-host/v1/chat/completions")
    key_env = spec.get("api_key_env", "OPENAI_API_KEY")
    if spec.get("requires_key", True) and not os.environ.get(key_env):
        raise ProviderUnavailable(f"{key_env} is not set")
    model_id = spec.get("model_id")
    if not model_id:
        raise ProviderUnavailable("openai-compatible needs an explicit model_id")
    return NVIDIAProvider(
        model_id, model_version=spec.get("model_version", "unknown"),
        api_key_env=key_env, base_url=base_url,
        system_prompt=spec.get("system_prompt", ""),
        model_family=spec.get("model_family"),
        timeout_seconds=spec.get("timeout_seconds"),
        max_retries=spec.get("max_retries"),
        # The registry name, so the record says which host served the call.
        name="openai-compatible")


@register("cerebras")
def _cerebras(spec: dict):
    """
    Cerebras inference. OpenAI-compatible, and interesting to Quintek for one
    specific reason: the measured problem on this project is not model quality
    but latency -- a validator good enough to catch defects was too slow to
    serve interactively. A provider whose selling point is speed is a direct
    test of whether that trade-off is inherent or is an artefact of one host.

    Model ids are Cerebras's own short names (`llama3.1-8b`,
    `llama-3.3-70b`), not the `meta/...` paths NVIDIA uses. Passing one
    provider's id to the other is the obvious mistake and produces a 404, so
    it is left to fail loudly rather than being silently rewritten.
    """
    from .nvidia import NVIDIAProvider

    key_env = spec.get("api_key_env", "CEREBRAS_API_KEY")
    if not os.environ.get(key_env):
        raise ProviderUnavailable(
            f"{key_env} is not set, so the Cerebras provider cannot authenticate.")
    model_id = spec.get("model_id")
    if not model_id:
        raise ProviderUnavailable(
            "the cerebras provider needs an explicit model_id, e.g. 'llama3.1-8b'")
    return NVIDIAProvider(
        model_id, model_version=spec.get("model_version", "unknown"),
        api_key_env=key_env,
        base_url=spec.get("base_url", "https://api.cerebras.ai/v1/chat/completions"),
        system_prompt=spec.get("system_prompt", ""),
        model_family=spec.get("model_family"),
        timeout_seconds=spec.get("timeout_seconds"),
        max_retries=spec.get("max_retries"),
        # The registry name, so the record says which host served the call.
        name="cerebras")


@register("openrouter")
def _openrouter(spec: dict):
    """
    OpenRouter. One key, many model families behind it.

    That matters here beyond convenience: `docs/JUDGE_INDEPENDENCE.md` Tier 2
    wants a judge from a DIFFERENT model family, and on the NVIDIA account
    only two llama ids actually served, so generator and validator had to be
    the same family. A gateway carrying several families is the cheapest route
    to satisfying that requirement properly.
    """
    from .nvidia import NVIDIAProvider

    key_env = spec.get("api_key_env", "OPENROUTER_API_KEY")
    if not os.environ.get(key_env):
        raise ProviderUnavailable(
            f"{key_env} is not set, so the OpenRouter provider cannot authenticate.")
    model_id = spec.get("model_id")
    if not model_id:
        raise ProviderUnavailable(
            "the openrouter provider needs an explicit model_id, e.g. "
            "'meta-llama/llama-3.3-70b-instruct'")
    return NVIDIAProvider(
        model_id, model_version=spec.get("model_version", "unknown"),
        api_key_env=key_env,
        base_url=spec.get("base_url", "https://openrouter.ai/api/v1/chat/completions"),
        system_prompt=spec.get("system_prompt", ""),
        model_family=spec.get("model_family"),
        timeout_seconds=spec.get("timeout_seconds"),
        max_retries=spec.get("max_retries"),
        # The registry name, so the record says which host served the call.
        name="openrouter")



# ---------------------------------------------------------------------------
# Paid OpenAI-compatible hosts
# ---------------------------------------------------------------------------
#
# Fireworks and Together both speak `/v1/chat/completions`, so neither needs a
# new adapter -- the difference between them is three strings: the base URL,
# the key variable, and the shape of a model id. Both are registered rather
# than one being chosen, because the interface does not differ in any way that
# would make picking early worth doing, and a name that exists is easier to
# check a credential against than one that has to be written first.
#
# THE DEFAULT BASE URLS BELOW ARE UNVERIFIED. Nothing in this repository has
# ever reached either host, and no test here proves a URL is right -- a mocked
# transport cannot. Confirm each against the provider's own documentation
# before the first live call, or pass `base_url` explicitly. `preflight`
# prints the URL it would use for exactly this reason.

@register("fireworks")
def _fireworks(spec: dict):
    """
    Fireworks AI. Model ids are account-scoped paths, e.g.
    `accounts/fireworks/models/llama-v3p1-70b-instruct` -- not the `meta/...`
    form NVIDIA uses. Passing one host's id to another produces a 404, which
    is left to fail loudly rather than being rewritten on a guess.
    """
    return _openai_host(spec, name="fireworks", key_env="FIREWORKS_API_KEY",
                        base_url="https://api.fireworks.ai/inference/v1/chat/completions",
                        example="accounts/fireworks/models/llama-v3p1-70b-instruct")


@register("together")
def _together(spec: dict):
    """
    Together AI. Model ids are Hugging Face style, e.g.
    `meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo`.
    """
    return _openai_host(spec, name="together", key_env="TOGETHER_API_KEY",
                        base_url="https://api.together.xyz/v1/chat/completions",
                        example="meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo")


@register("groq")
def _groq(spec: dict):
    """
    Groq. OpenAI-compatible, and different from Fireworks and Together in the
    one way that matters for planning a run.

    THE BINDING CONSTRAINT IS RATE, NOT PRICE. The paid hosts bill per token
    and will serve as fast as you ask. Groq's free tier meters requests per
    minute -- roughly 30 at the time of writing, which nothing here can verify
    -- and a run that sprints at it spends its attempts on 429s instead of
    answers. So a Groq run is planned in WALL CLOCK first and budget second,
    which is the reverse of every other host in this registry. See
    `benchmark/providers/pacing.py`.

    Daily request and token ceilings are a SEPARATE limit from the per-minute
    one. Hitting one mid-run is an INCOMPLETE, never a score -- a run that
    stopped because it ran out of allowance measured a fraction of the corpus,
    and the fraction is not a result.

    Model ids are plain names rather than account-scoped paths, and the exact
    strings change as Groq's catalogue does, so none is defaulted here: the id
    must be given explicitly and `preflight` prints it back for checking
    against Groq's own model list before the first call.
    """
    return _openai_host(spec, name="groq", key_env="GROQ_API_KEY",
                        base_url="https://api.groq.com/openai/v1/chat/completions",
                        example="a plain model id from Groq's catalogue, e.g. the "
                                "llama / gpt-oss / qwen families -- check the exact "
                                "string against Groq's docs, none is assumed here")


def _openai_host(spec: dict, *, name: str, key_env: str, base_url: str, example: str):
    """Shared body for the OpenAI-compatible hosts. `name` is keyword-only and
    has no default: see the comment on the constructor call below."""
    from .nvidia import NVIDIAProvider

    key_env = spec.get("api_key_env", key_env)
    # Read at BUILD time only to decide whether the provider can be built, and
    # never stored: `NVIDIAProvider._api_key` reads the variable again at call
    # time. The value does not live on the instance, so it cannot reach a
    # manifest, a record or a traceback.
    if not os.environ.get(key_env):
        raise ProviderUnavailable(
            f"{key_env} is not set, so this provider cannot authenticate.")
    model_id = spec.get("model_id")
    if not model_id:
        raise ProviderUnavailable(
            f"this provider needs an explicit model_id, e.g. {example!r}")
    return NVIDIAProvider(
        model_id, model_version=spec.get("model_version", "unknown"),
        api_key_env=key_env,
        base_url=spec.get("base_url", base_url),
        system_prompt=spec.get("system_prompt", ""),
        model_family=spec.get("model_family"),
        timeout_seconds=spec.get("timeout_seconds"),
        max_retries=spec.get("max_retries"),
        # The registry name, so the record says which host served the call.
        # Required, not defaulted: this function builds the adapter for three
        # different hosts, and a forgotten name would file all of them under
        # whichever one the default happened to be.
        name=name)


@register("local")
def _local(spec: dict):
    """
    A locally served OpenAI-compatible endpoint (llama.cpp, vLLM, Ollama's
    compat shim). No key by default, because a local server usually has none;
    everything else is identical to `openai-compatible`.
    """
    merged = {"base_url": "http://127.0.0.1:8000/v1/chat/completions",
              "requires_key": False, **spec}
    return _openai_compatible(merged)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def build_provider(spec: dict | str):
    """
    Construct the provider a spec names.

    A bare string is taken as the provider name, so `build_provider("scripted")`
    works for the common case.
    """
    if isinstance(spec, str):
        spec = {"provider": spec}
    name = (spec.get("provider") or "").strip()
    if not name:
        raise UnknownProvider(
            f"a provider spec must name a provider; known names are {', '.join(available())}")
    if name not in BUILDERS:
        raise UnknownProvider(
            f"unknown provider {name!r}; known names are {', '.join(available())}")
    return BUILDERS[name](spec)


def spec_from_env(prefix: str = "QUINTEK") -> dict:
    """
    A provider spec from environment variables, for deployments that configure
    by env rather than by file.

        QUINTEK_PROVIDER=nvidia
        QUINTEK_MODEL_ID=meta/llama-3.3-70b-instruct
        QUINTEK_MODEL_VERSION=2024-12
        QUINTEK_BASE_URL=...            (optional)
        QUINTEK_API_KEY_ENV=...         (optional; names the key variable)
        QUINTEK_TIMEOUT_SECONDS=...     (optional)
        QUINTEK_MAX_RETRIES=...         (optional)

    Defaults to `scripted`, and says so, rather than defaulting to a real
    provider that would start spending money because a variable was unset.
    """
    spec = {"provider": os.environ.get(f"{prefix}_PROVIDER", "scripted")}
    for key, env in (("model_id", "MODEL_ID"), ("model_version", "MODEL_VERSION"),
                     ("base_url", "BASE_URL"), ("api_key_env", "API_KEY_ENV"),
                     ("model_family", "MODEL_FAMILY")):
        value = os.environ.get(f"{prefix}_{env}")
        if value:
            spec[key] = value
    timeout = os.environ.get(f"{prefix}_TIMEOUT_SECONDS")
    if timeout:
        spec["timeout_seconds"] = float(timeout)
    retries = os.environ.get(f"{prefix}_MAX_RETRIES")
    if retries:
        spec["max_retries"] = int(retries)
    return spec


def describe(spec: dict | str) -> dict:
    """
    What a spec would build, without building it.

    Used by the acceptance checklist and by the admin console to answer "is
    this deployment actually talking to a model" without making a call.
    """
    if isinstance(spec, str):
        spec = {"provider": spec}
    name = spec.get("provider", "")
    known = name in BUILDERS
    buildable, reason = False, ""
    if known:
        try:
            build_provider(spec)
            buildable = True
        except ProviderUnavailable as exc:
            reason = str(exc)
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
    else:
        reason = f"unknown provider {name!r}"
    return {
        "provider": name, "model_id": spec.get("model_id"),
        "known": known, "buildable": buildable, "reason": reason,
        # The distinction that matters for every claim built on top of a run.
        "is_real_model": known and name != "scripted",
    }
