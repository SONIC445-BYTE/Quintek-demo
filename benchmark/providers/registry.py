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
        timeout_seconds=spec.get("timeout_seconds"))


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
