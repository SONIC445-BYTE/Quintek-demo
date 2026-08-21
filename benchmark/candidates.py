"""
The candidate funnel: from a provider catalogue to a benchmarkable shortlist.

    ~516 catalogue entries
            |
            v  inference actually confirmed
            |
            v  required capabilities
            |
            v  cost and latency constraints
            |
    ~15-30 candidates  ->  controlled benchmark  ->  5-10 production models

Two distinctions this module refuses to blur.

**Catalogue presence is not availability.** A `/v1/models` listing says what a
provider's catalogue contains, not what this key can call. Measured on this
project: NVIDIA lists 102 models and a sample of them returned 404, 500 and
timeouts on inference; Cerebras lists 2 and both return 402. So `CatalogueEntry`
carries `inference_status`, which starts as `UNVERIFIED` and only becomes
`SERVING` when a real call succeeded. Nothing enters a shortlist on catalogue
evidence alone.

**Unavailable now is not unavailable forever.** `402 payment_required` is a
billing state, not a model property. It is stored as an observation with a
timestamp -- `inference_status`, `reason`, `observed_at` -- so it can be
re-checked and change, rather than being baked in as "this model is bad".

Selection is by declared, objective filters only. No filter in this module
consults a model's name, vendor, or reputation, because a shortlist built on
reputation is a shortlist that cannot be argued with -- and the entire point
of the benchmark downstream is to find out which model is actually better at
Quintek's tasks, which is frequently not the famous one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Inference availability, observed rather than assumed.
UNVERIFIED = "UNVERIFIED"
SERVING = "SERVING"
MODEL_UNAVAILABLE = "404_MODEL_UNAVAILABLE"
BILLING_BLOCKED = "BILLING_BLOCKED"
AUTH_BLOCKED = "401/403_AUTH"
RATE_LIMITED = "429_RATE_LIMIT"
PROVIDER_ERROR = "5xx_PROVIDER"
TIMEOUT = "TIMEOUT"
UNKNOWN = "UNKNOWN"

# What an entry IS, as its catalogue described it. A FACT carried in from
# discovery, not a judgement made here.
#
# The distinction is not academic. `openrouter/free` is a Free Models Router:
# it selects among models rather than being one. It reached all three
# shortlists because it passed every capability filter and prices itself at
# 0/0 -- so the `-1` sentinel guard that catches `openrouter/auto` misses it
# completely. A leaderboard containing it compares model against model against
# a model-selection algorithm, which makes every ranking on it unreadable.
ENTRY_MODEL = "MODEL"
ENTRY_ROUTER = "ROUTER"
ENTRY_ALIAS = "ALIAS"
ENTRY_AGGREGATOR = "AGGREGATOR"
ENTRY_UNKNOWN = "UNKNOWN"

# Kinds that are not a single model and therefore cannot be a candidate in a
# model benchmark. Routers may be worth evaluating one day -- against each
# OTHER, on a separate board, with their own baseline. Not on this one.
NOT_A_SINGLE_MODEL = {ENTRY_ROUTER, ENTRY_AGGREGATOR}

# Statuses that could become SERVING without any change to the model itself.
# Kept apart from permanent unavailability so a shortlist can be rebuilt when
# a bill is paid or a rate limit clears.
TRANSIENT = {BILLING_BLOCKED, RATE_LIMITED, PROVIDER_ERROR, TIMEOUT, UNVERIFIED}


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class CatalogueEntry:
    """
    One model as the provider's catalogue describes it, plus what we have
    independently observed about calling it.

    Capability fields are None when the provider does not expose them --
    distinct from False. NVIDIA and Cerebras return only an id, so claiming
    "no structured output" for their models would be inventing a measurement.
    """

    provider: str
    model_id: str
    context_length: int | None = None
    supports_structured: bool | None = None
    supports_tools: bool | None = None
    supports_reasoning: bool | None = None
    supports_vision: bool | None = None
    input_modalities: list[str] = field(default_factory=list)
    output_modalities: list[str] = field(default_factory=list)
    price_in_per_m: float | None = None
    price_out_per_m: float | None = None
    # Observed, not assumed.
    inference_status: str = UNVERIFIED
    inference_reason: str = ""
    observed_at: str = ""
    latency_ms: float | None = None
    # What the source said this is. Defaults to MODEL because most providers
    # expose nothing else and their catalogues are models; a provider that
    # says otherwise -- OpenRouter marks routers with tokenizer "Router" and
    # aliases with an alias_target -- is carried through unchanged.
    entry_kind: str = ENTRY_MODEL
    alias_target: str | None = None

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.model_id}"

    @property
    def confirmed(self) -> bool:
        return self.inference_status == SERVING

    @property
    def recheckable(self) -> bool:
        """Could become available again without the model changing."""
        return self.inference_status in TRANSIENT

    def record_probe(self, status: str, *, reason: str = "",
                     latency_ms: float | None = None) -> "CatalogueEntry":
        self.inference_status = status
        self.inference_reason = reason
        self.latency_ms = latency_ms
        self.observed_at = now_iso()
        return self

    def as_dict(self) -> dict:
        return {"key": self.key, "provider": self.provider, "model_id": self.model_id,
                "context_length": self.context_length,
                "structured": self.supports_structured, "tools": self.supports_tools,
                "reasoning": self.supports_reasoning, "vision": self.supports_vision,
                "input_modalities": list(self.input_modalities),
                "output_modalities": list(self.output_modalities),
                "price_in_per_m": self.price_in_per_m,
                "price_out_per_m": self.price_out_per_m,
                "inference_status": self.inference_status,
                "inference_reason": self.inference_reason,
                "observed_at": self.observed_at, "latency_ms": self.latency_ms,
                "confirmed": self.confirmed, "recheckable": self.recheckable}


@dataclass
class Filter:
    """
    One objective requirement, with the reason it exists.

    `require_confirmed_inference` defaults True everywhere. A shortlist that
    admits unverified entries hands the expensive benchmark a queue of 404s.
    """

    name: str
    require_confirmed_inference: bool = True
    # A router is not a model. Left as a named requirement rather than a
    # hardcoded skip so a future router-versus-router board can switch it off
    # deliberately instead of by editing this class.
    require_single_model: bool = True
    require_structured: bool = False
    require_reasoning: bool = False
    require_vision: bool = False
    require_tools: bool = False
    min_context: int | None = None
    max_price_in_per_m: float | None = None
    max_price_out_per_m: float | None = None
    max_latency_ms: float | None = None
    # Quintek's pipeline reads text. A model that cannot emit text cannot
    # write a question however much else it can do -- and several catalogue
    # entries are image or audio generators that pass every other filter.
    require_text_output: bool = True
    # When a provider exposes no capability metadata, is the entry admitted?
    # False by default: "we do not know" must not read as "yes".
    allow_unknown_capabilities: bool = False

    def check(self, entry: CatalogueEntry) -> list[str]:
        """Every reason this entry fails, or an empty list if it passes."""
        failures = []

        if self.require_single_model and entry.entry_kind in NOT_A_SINGLE_MODEL:
            failures.append(
                f"is a {entry.entry_kind.lower()}, not a single model: it selects "
                "among models, so ranking it beside them compares a model with a "
                "model-selection algorithm")

        if self.require_confirmed_inference and not entry.confirmed:
            failures.append(
                f"inference not confirmed ({entry.inference_status})"
                + (f": {entry.inference_reason}" if entry.inference_reason else ""))

        def capability(flag: bool | None, label: str):
            if flag is True:
                return
            if flag is None:
                if not self.allow_unknown_capabilities:
                    failures.append(
                        f"{label} not exposed by this provider's catalogue, and unknown "
                        "capabilities are not admitted")
                return
            failures.append(f"does not support {label}")

        if self.require_structured:
            capability(entry.supports_structured, "structured output")
        if self.require_reasoning:
            capability(entry.supports_reasoning, "reasoning")
        if self.require_vision:
            capability(entry.supports_vision, "vision")
        if self.require_tools:
            capability(entry.supports_tools, "tool calling")

        if self.require_text_output and entry.output_modalities:
            if "text" not in entry.output_modalities:
                failures.append(
                    f"emits {'/'.join(entry.output_modalities)}, not text")

        if self.min_context is not None:
            if entry.context_length is None:
                if not self.allow_unknown_capabilities:
                    failures.append("context length not exposed by this provider")
            elif entry.context_length < self.min_context:
                failures.append(
                    f"context {entry.context_length:,} < required {self.min_context:,}")

        for value, ceiling, label in (
                (entry.price_in_per_m, self.max_price_in_per_m, "input price"),
                (entry.price_out_per_m, self.max_price_out_per_m, "output price")):
            if ceiling is None:
                continue
            if value is None:
                if not self.allow_unknown_capabilities:
                    failures.append(
                        f"{label} is not stated by the catalogue, so it cannot be shown to "
                        "be within budget")
            elif value > ceiling:
                failures.append(f"{label} ${value:.2f}/M exceeds ${ceiling:.2f}/M")

        if (self.max_latency_ms is not None and entry.latency_ms is not None
                and entry.latency_ms > self.max_latency_ms):
            failures.append(
                f"probe latency {entry.latency_ms:.0f}ms exceeds "
                f"{self.max_latency_ms:.0f}ms")
        return failures


# The three roles Quintek needs, as declared filters. Thresholds are stated
# here so they can be argued with and changed in one place, rather than being
# scattered through a selection script.
ROLE_FILTERS = {
    "generation": Filter(
        "generation", require_structured=True, min_context=32_000),
    "validation": Filter(
        # Reasoning is required because the measured failure was a validator
        # with none: llama-3.1-8b approved a question that contradicted its
        # own source passage.
        "validation", require_structured=True, require_reasoning=True,
        min_context=32_000),
    "vision": Filter(
        "vision", require_structured=True, require_vision=True),
}


@dataclass
class Shortlist:
    role: str
    selected: list[CatalogueEntry]
    rejected: list[dict]
    filter_used: Filter

    @property
    def size(self) -> int:
        return len(self.selected)

    def rejection_summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self.rejected:
            for reason in entry["reasons"]:
                head = reason.split(" (")[0].split(":")[0]
                counts[head] = counts.get(head, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def as_dict(self) -> dict:
        return {"role": self.role, "size": self.size,
                "selected": [e.as_dict() for e in self.selected],
                "rejected_count": len(self.rejected),
                "rejection_summary": self.rejection_summary(),
                "filter": vars(self.filter_used)}


def apply_filter(entries: list[CatalogueEntry], spec: Filter) -> Shortlist:
    selected, rejected = [], []
    for entry in entries:
        failures = spec.check(entry)
        if failures:
            rejected.append({"key": entry.key, "reasons": failures})
        else:
            selected.append(entry)
    return Shortlist(spec.name, selected, rejected, spec)


def rank_for_shortlist(entries: list[CatalogueEntry], *, limit: int = 30
                       ) -> list[CatalogueEntry]:
    """
    Trim a passing set to a benchmarkable size WITHOUT judging quality.

    The only ordering used is cost then context then id -- all objective, none
    a proxy for how good a model is. Picking the "best-looking" models here
    would decide the benchmark's outcome before running it, and the whole
    reason for the benchmark is that reputation is a poor predictor of
    performance on Quintek's specific tasks.
    """
    def sort_key(entry: CatalogueEntry):
        # Unpriced sorts LAST, not first. Treating "no stated price" as zero
        # is what put the variable-price router aliases at the head of every
        # shortlist.
        unpriced = entry.price_in_per_m is None
        return (unpriced, entry.price_in_per_m or 0.0,
                -(entry.context_length or 0), entry.key)

    return sorted(entries, key=sort_key)[:limit]


def diversify(entries: list[CatalogueEntry], *, per_provider: int = 10,
              per_family: int = 3) -> list[CatalogueEntry]:
    """
    Cap how many entries any one provider or model family contributes.

    Without this a shortlist fills with fifteen variants of one family and the
    benchmark measures that family's checkpoints rather than comparing
    approaches. Family is taken from the id's leading path segment, which is
    how every provider here namespaces its models.
    """
    by_provider: dict[str, int] = {}
    by_family: dict[str, int] = {}
    kept = []
    for entry in entries:
        family = entry.model_id.split("/")[0] if "/" in entry.model_id else \
            entry.model_id.split("-")[0]
        if by_provider.get(entry.provider, 0) >= per_provider:
            continue
        if by_family.get(family, 0) >= per_family:
            continue
        by_provider[entry.provider] = by_provider.get(entry.provider, 0) + 1
        by_family[family] = by_family.get(family, 0) + 1
        kept.append(entry)
    return kept


# ---------------------------------------------------------------------------
# Loading a catalogue
# ---------------------------------------------------------------------------

def from_openrouter(model: dict) -> CatalogueEntry:
    architecture = model.get("architecture") or {}
    parameters = set(model.get("supported_parameters") or [])
    pricing = model.get("pricing") or {}
    modalities = list(architecture.get("input_modalities") or [])

    def price(field):
        """
        Price per million tokens, or None when the catalogue does not state one.

        OpenRouter uses `-1` for models whose price is decided at request time
        (the `auto` routers). That is a sentinel, not a price. Read literally
        it is the cheapest number in the catalogue, which sorted those two
        entries to the top of every shortlist -- so it becomes None, and an
        unknown price fails a price ceiling rather than beating it.
        """
        value = pricing.get(field)
        try:
            number = float(value) if value is not None else None
        except (TypeError, ValueError):
            return None
        if number is None or number < 0:
            return None
        return number * 1_000_000

    # What the entry IS, straight from what OpenRouter said. A routing product
    # carries tokenizer "Router"; an alias carries alias_target. Neither is
    # inferred from the name -- `openrouter/free` and `openrouter/auto` share a
    # prefix with nothing that guarantees kind, and a name-based rule would
    # miss a router published under any other vendor's namespace.
    alias = model.get("alias_target") or None
    if alias:
        kind = ENTRY_ALIAS
    elif architecture.get("tokenizer") == "Router":
        kind = ENTRY_ROUTER
    else:
        kind = ENTRY_MODEL

    return CatalogueEntry(
        provider="openrouter", model_id=model["id"],
        entry_kind=kind,
        alias_target=(alias.get("slug") if isinstance(alias, dict) else alias),
        context_length=model.get("context_length"),
        supports_structured=bool(parameters & {"response_format", "structured_outputs"}),
        supports_tools=bool(parameters & {"tools", "tool_choice"}),
        supports_reasoning=bool(model.get("reasoning")) or bool(parameters & {"reasoning"}),
        supports_vision="image" in modalities,
        input_modalities=modalities,
        output_modalities=list(architecture.get("output_modalities") or []),
        price_in_per_m=price("prompt"), price_out_per_m=price("completion"))


def from_bare(provider: str, model: dict) -> CatalogueEntry:
    """
    A provider whose catalogue exposes only an id.

    Every capability stays None rather than defaulting to False. NVIDIA and
    Cerebras return `id / object / created / owned_by`, so asserting anything
    about structured output or context would be inventing a measurement.
    """
    return CatalogueEntry(provider=provider, model_id=model["id"])


def apply_capability_probe(entries: list[CatalogueEntry], path: str | Path) -> int:
    """
    Merge an EMPIRICAL capability probe onto entries whose catalogue is bare.

    NVIDIA and Cerebras publish only a model id, so every capability field
    starts None and the "unknown is not yes" rule excludes them from every
    shortlist. That is the right default and the wrong outcome: 27 NVIDIA
    models demonstrably serve, and discarding them unmeasured is the same
    mistake as admitting them unmeasured, in the other direction.

    So structured-output support is measured instead of assumed -- send a
    request asking for a JSON object, see whether a usable one comes back.
    That is exactly the capability Quintek's pipeline depends on, tested the
    way the pipeline will use it.
    """
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    provider = payload["provider"].lower()
    by_id = {e.model_id: e for e in entries if e.provider == provider}
    updated = 0
    for result in payload.get("results", []):
        entry = by_id.get(result["model"])
        if entry is None or result.get("structured_ok") is None:
            continue
        entry.supports_structured = bool(result["structured_ok"])
        if result.get("latency_ms") is not None:
            entry.latency_ms = result["latency_ms"]
        updated += 1
    return updated


def load_catalogue(path: str | Path) -> list[CatalogueEntry]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    entries: list[CatalogueEntry] = []
    for provider, block in payload.items():
        for model in block.get("models", []):
            if provider.lower() == "openrouter":
                entries.append(from_openrouter(model))
            else:
                entries.append(from_bare(provider.lower(), model))
    return entries


def apply_probe_results(entries: list[CatalogueEntry], path: str | Path) -> int:
    """Merge an inference probe back onto the catalogue. Returns rows updated."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    provider = payload["provider"].lower()
    by_id = {e.model_id: e for e in entries if e.provider == provider}
    updated = 0
    for result in payload.get("results", []):
        entry = by_id.get(result["model"])
        if entry is None:
            continue
        entry.record_probe(result["status"], reason=result.get("detail", "")[:120],
                           latency_ms=result.get("latency_ms"))
        updated += 1
    return updated
