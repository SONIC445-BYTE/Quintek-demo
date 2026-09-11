#!/usr/bin/env python3
"""
Check a provider configuration without spending a call on it.

WHAT THIS IS FOR
----------------
The gap this closes is between "the configuration looks right" and "the first
live call works". Every earlier failure on this project was found by spending
real inference on it: D017 burned three Phase 0 arms before anyone noticed the
adapter was returning the HTTP envelope as the model's answer, and the max
tokens ceiling cost 52 items before it was measured.

So this reports what WOULD happen, and makes no request. It answers:

  * is the provider name one this codebase can build?
  * is the credential present -- by NAME, never by value?
  * which endpoint, model id, timeout and retry policy would be used?
  * would the run be a measurement, or a scripted double?

WHAT IT CANNOT TELL YOU
-----------------------
Whether the endpoint is correct, whether the model id exists on that host, or
whether the credential is valid. None of those can be known without a call.
The base URLs for the paid hosts are defaults nothing here has ever reached,
and this command prints the URL precisely so it can be checked against the
provider's documentation by a human before the first call.

    python3 tools_provider_preflight.py                       # from the environment
    python3 tools_provider_preflight.py --provider fireworks \
        --model-id accounts/fireworks/models/llama-v3p1-70b-instruct
    python3 tools_provider_preflight.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from benchmark.providers.registry import available, describe, spec_from_env

# Which environment variable each provider reads by default. Names only.
DEFAULT_KEY_ENV = {
    "nvidia": "NVIDIA_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
    "fireworks": "FIREWORKS_API_KEY",
    "together": "TOGETHER_API_KEY",
    "groq": "GROQ_API_KEY",
    "openai-compatible": "OPENAI_API_KEY",
}


def preflight(spec: dict) -> dict:
    """A report on `spec`. Makes no network request."""
    report = describe(spec)
    provider = report["provider"]
    key_env = spec.get("api_key_env") or DEFAULT_KEY_ENV.get(provider, "")

    built = None
    if report["buildable"]:
        from benchmark.providers.registry import build_provider
        built = build_provider(spec)

    report.update({
        "credential_ref": key_env or None,
        # PRESENCE, never the value. A preflight that printed a key would be a
        # credential leak with a helpful tone of voice.
        "credential_present": bool(os.environ.get(key_env)) if key_env else None,
        "base_url": (getattr(built, "base_url", None)
                     or spec.get("base_url")),
        "model_version": spec.get("model_version", "unknown"),
        "model_family": getattr(built, "model_family", None) or spec.get("model_family"),
        "timeout_seconds": (built.retry_policy.timeout_seconds if built
                            else spec.get("timeout_seconds")),
        "max_retries": (built.retry_policy.max_retries if built
                        else spec.get("max_retries")),
    })
    retries = report["max_retries"]
    report["outbound_attempts_per_call"] = (None if retries is None else retries + 1)
    # What is binding on this host. Groq meters REQUESTS PER MINUTE, so a run
    # against it is planned in wall clock first; the paid hosts bill per token
    # and the ceiling that matters there is spend.
    report["binding_constraint"] = {
        "groq": "requests per minute (free tier ~30 RPM, unverified) and a separate "
                "daily ceiling. Plan wall clock first, budget second.",
    }.get(provider, "token spend" if report["is_real_model"] else "")
    report["unverified"] = [
        "the endpoint is a default; nothing here has ever reached it",
        "whether the model id exists on that host",
        "whether the credential is accepted",
    ] if report["is_real_model"] else []
    return report


def _render(r: dict) -> str:
    out = [f"provider        {r['provider']}"]
    if not r["known"]:
        out += [f"                UNKNOWN. Known names: {', '.join(available())}"]
        return "\n".join(out)
    out += [
        f"model id        {r['model_id'] or '(none set)'}",
        f"model version   {r['model_version']}",
        f"model family    {r['model_family'] or '(inferred at build)'}",
        f"endpoint        {r['base_url'] or '(provider default)'}",
        f"credential      {r['credential_ref'] or '(none needed)'}"
        + ("" if r["credential_present"] is None
           else ("  PRESENT" if r["credential_present"] else "  NOT SET")),
        f"timeout         {r['timeout_seconds']}s",
        f"retries         {r['max_retries']} "
        f"(up to {r['outbound_attempts_per_call']} outbound attempts per call)",
    ]
    if r.get("binding_constraint"):
        out.append(f"binding limit   {r['binding_constraint']}")
    out.append("")
    if r["buildable"]:
        out.append("BUILDABLE: this configuration constructs.")
    else:
        out.append(f"NOT BUILDABLE: {r['reason']}")
    if not r["is_real_model"]:
        out.append("NOT A REAL MODEL: this is a scripted double. Runs are not measurements.")
    if r["unverified"]:
        out += ["", "NOT CHECKED BY THIS COMMAND -- no request was made:"]
        out += [f"  - {u}" for u in r["unverified"]]
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--provider")
    ap.add_argument("--model-id")
    ap.add_argument("--model-version")
    ap.add_argument("--base-url")
    ap.add_argument("--api-key-env", help="the NAME of the variable holding the key")
    ap.add_argument("--timeout-seconds", type=float)
    ap.add_argument("--max-retries", type=int)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    spec = spec_from_env()
    for field, value in (("provider", args.provider), ("model_id", args.model_id),
                         ("model_version", args.model_version), ("base_url", args.base_url),
                         ("api_key_env", args.api_key_env),
                         ("timeout_seconds", args.timeout_seconds),
                         ("max_retries", args.max_retries)):
        if value is not None:
            spec[field] = value

    report = preflight(spec)
    print(json.dumps(report, indent=2) if args.json else _render(report))
    # Non-zero when it would not run, so CI can gate on it.
    return 0 if report["buildable"] else 1


if __name__ == "__main__":
    sys.exit(main())
