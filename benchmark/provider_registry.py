"""
What each provider IS, separately from whether it works here today.

    ADAPTER WORKS  ≠  PROVIDER REACHABLE  ≠  PROVIDER HEALTHY

Three independent facts that a single "status" field would flatten:

  * **adapter** -- is the code written? Does it construct?
  * **mock** -- is it tested against a simulated endpoint? This is the only
    one of the three that stays true regardless of where the process runs.
  * **live** -- has a real call to the real host ever succeeded FROM HERE?

Flattening them produces the wrong conclusion twice over. A correct,
fully-tested adapter behind a corporate firewall reads as a broken provider.
A reachable host with an untested adapter reads as a working one. Both
mistakes have been made on this project.

So a provider blocked by egress policy is recorded as
`EGRESS_BLOCKED`, with `adapter: yes, mock: yes, live: not from here` -- and
nothing about it is a quality signal. When the same code runs somewhere the
host is permitted, the live column changes and nothing else has to.

The registry is descriptive. It does not choose providers -- that is
`benchmark/quintek_router.py` -- and it does not judge models, which is
`benchmark/fitness.py`. It answers "what have we got, and what is wrong with
it".
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .provider_status import ProviderStatus, assess

# Capabilities a task can require. Declared per model, because sending a
# vision task to a text-only model wastes a call to learn something the
# registry already knew.
CAPABILITIES = (
    "reasoning", "coding", "vision", "tool_calling", "structured_output",
    "long_context", "medical_qa",
)


@dataclass
class ProviderEntry:
    """
    One provider adapter and everything known about it.

    `live_ok` is None until a real call has been attempted, and stays None
    when the attempt never reached the host -- "not tested" and "tested and
    failed" are different, and only the second is evidence.
    """

    name: str
    adapter_ok: bool = True
    mock_tested: bool = False
    live_ok: bool | None = None
    status: str = ProviderStatus.AVAILABLE
    blocked_host: str = ""
    detail: str = ""
    default_model: str = ""
    api_key_env: str = ""
    base_url: str = ""
    notes: str = ""

    @property
    def usable_here(self) -> bool:
        return self.status not in (ProviderStatus.EGRESS_BLOCKED,
                                   ProviderStatus.AUTH_FAILED)

    @property
    def live_symbol(self) -> str:
        return {True: "yes", False: "failed", None: "—"}[self.live_ok]

    def as_dict(self) -> dict:
        return {"name": self.name, "adapter": self.adapter_ok, "mock": self.mock_tested,
                "live": self.live_symbol, "status": self.status,
                "usable_here": self.usable_here, "blocked_host": self.blocked_host,
                "detail": self.detail, "default_model": self.default_model,
                "api_key_env": self.api_key_env, "notes": self.notes}


@dataclass
class ModelEntry:
    """
    One `provider:model` pair, with what it claims to be able to do.

    Provider and model are separate dimensions on purpose. The interesting
    finding is usually not "provider P is best" but "model M is best, and P
    happens to be the faster host for it" -- which is invisible if the two are
    collapsed into one identifier.
    """

    provider: str
    model_id: str
    capabilities: dict[str, bool] = field(default_factory=dict)
    context_tokens: int | None = None
    cost_per_1k_usd: float | None = None
    model_family: str = ""
    notes: str = ""

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.model_id}"

    def supports(self, capability: str) -> bool:
        return bool(self.capabilities.get(capability, False))

    def missing(self, required) -> list[str]:
        return sorted(c for c in required if not self.supports(c))

    def as_dict(self) -> dict:
        return {"key": self.key, "provider": self.provider, "model_id": self.model_id,
                "capabilities": dict(self.capabilities),
                "context_tokens": self.context_tokens,
                "cost_per_1k_usd": self.cost_per_1k_usd,
                "model_family": self.model_family, "notes": self.notes}


class ProviderRegistry:
    """Providers, models, and the evidence behind each one's status."""

    def __init__(self):
        self._providers: dict[str, ProviderEntry] = {}
        self._models: dict[str, ModelEntry] = {}

    # ---------- registration ----------

    def add_provider(self, entry: ProviderEntry) -> ProviderEntry:
        self._providers[entry.name] = entry
        return entry

    def add_model(self, entry: ModelEntry) -> ModelEntry:
        if entry.provider not in self._providers:
            raise KeyError(
                f"model {entry.key!r} names provider {entry.provider!r}, which is not "
                "registered. Registering a model against an unknown provider is how a "
                "routable id ends up with nothing behind it.")
        self._models[entry.key] = entry
        return entry

    def provider(self, name: str) -> ProviderEntry | None:
        return self._providers.get(name)

    def model(self, key: str) -> ModelEntry | None:
        return self._models.get(key)

    def providers(self) -> list[ProviderEntry]:
        return [self._providers[n] for n in sorted(self._providers)]

    def models(self, *, provider: str | None = None) -> list[ModelEntry]:
        entries = [self._models[k] for k in sorted(self._models)]
        return [m for m in entries if not provider or m.provider == provider]

    # ---------- observation ----------

    def record_probe(self, name: str, *, error=None, http_status: int | None = None,
                     latency_ms: float | None = None,
                     slow_threshold_ms: float | None = None,
                     host: str = "") -> ProviderEntry:
        """
        Record the outcome of a real call and update the provider's status.

        `live_ok` is set False ONLY when the host answered and the answer was
        unusable. A call that never reached the host leaves it None, because
        "we could not test this" is not a test result.
        """
        entry = self._providers.get(name)
        if entry is None:
            raise KeyError(f"unknown provider {name!r}")
        verdict = assess(error, http_status=http_status, latency_ms=latency_ms,
                         slow_threshold_ms=slow_threshold_ms)
        entry.status = verdict.status
        entry.detail = verdict.detail or verdict.policy.guidance

        if verdict.ok or verdict.status == ProviderStatus.DEGRADED:
            entry.live_ok = True
        elif verdict.status in (ProviderStatus.EGRESS_BLOCKED, ProviderStatus.AUTH_FAILED):
            entry.live_ok = None            # never reached / never authorised
            if verdict.status == ProviderStatus.EGRESS_BLOCKED:
                entry.blocked_host = host or entry.blocked_host
        else:
            entry.live_ok = False
        return entry

    # ---------- views ----------

    def eligible(self, *, required_capabilities=(),
                 exclude_statuses=()) -> tuple[list[ModelEntry], list[dict]]:
        """
        LAYER 1 -- "can I use this at all?"

        Reachability, credentials, model existence and declared capability.
        Deliberately separate from layer 2 ("should I?"), because a provider
        that merely works must not become the preferred provider by default.
        """
        blocked = set(exclude_statuses) or {
            ProviderStatus.EGRESS_BLOCKED, ProviderStatus.AUTH_FAILED,
            ProviderStatus.MODEL_UNAVAILABLE}
        required = tuple(required_capabilities)

        kept, dropped = [], []
        for model in self.models():
            provider = self._providers[model.provider]
            if not provider.adapter_ok:
                dropped.append({"key": model.key, "layer": 1, "reason": "no working adapter"})
                continue
            if provider.status in blocked:
                dropped.append({
                    "key": model.key, "layer": 1, "status": provider.status,
                    "reason": f"provider is {provider.status}"
                              + (f" ({provider.blocked_host})" if provider.blocked_host
                                 else ""),
                    # The flag that stops an environmental block being read as
                    # evidence about the model.
                    "environmental": provider.status in (
                        ProviderStatus.EGRESS_BLOCKED, ProviderStatus.AUTH_FAILED)})
                continue
            missing = model.missing(required)
            if missing:
                dropped.append({"key": model.key, "layer": 1,
                                "reason": f"does not claim {', '.join(missing)}"})
                continue
            kept.append(model)
        return kept, dropped

    def report(self) -> dict:
        """The registry as a table, plus what is blocking what."""
        rows = [p.as_dict() for p in self.providers()]
        blocked = [p for p in self.providers()
                   if p.status == ProviderStatus.EGRESS_BLOCKED]
        return {
            "providers": rows,
            "models": [m.as_dict() for m in self.models()],
            "usable_providers": [p.name for p in self.providers() if p.usable_here],
            "egress_blocked": [{"name": p.name, "host": p.blocked_host} for p in blocked],
            "note": ("" if not blocked else
                     f"{len(blocked)} provider(s) are blocked by this environment's egress "
                     "policy. Their adapters are written and mock-tested; they have not "
                     "failed. Running the same code where those hosts are permitted is the "
                     "only thing that changes."),
        }

    def render(self) -> str:
        """The human-readable table."""
        lines = ["PROVIDERS", ""]
        for provider in self.providers():
            lines.append(f"{provider.name}")
            lines.append(f"  adapter: {'yes' if provider.adapter_ok else 'NO'}")
            lines.append(f"  mock:    {'yes' if provider.mock_tested else 'NO'}")
            lines.append(f"  live:    {provider.live_symbol}")
            lines.append(f"  status:  {provider.status}")
            if provider.blocked_host:
                lines.append(f"  host:    {provider.blocked_host}")
            if provider.detail:
                lines.append(f"  reason:  {provider.detail[:90]}")
            lines.append("")
        return "\n".join(lines)


def default_registry() -> ProviderRegistry:
    """
    The providers this repository has adapters for, with what is known about
    each as of the last probe. Statuses are set by `record_probe`, not here --
    this only declares what exists.
    """
    registry = ProviderRegistry()
    registry.add_provider(ProviderEntry(
        "nvidia", mock_tested=True, default_model="meta/llama-3.1-8b-instruct",
        api_key_env="NVIDIA_API_KEY",
        base_url="https://integrate.api.nvidia.com/v1/chat/completions",
        notes="Serverless. Single requests fine; 70B endpoints show severe latency spikes."))
    registry.add_provider(ProviderEntry(
        "cerebras", mock_tested=True, default_model="llama3.1-8b",
        api_key_env="CEREBRAS_API_KEY",
        base_url="https://api.cerebras.ai/v1/chat/completions",
        notes="Speed-oriented host; directly tests whether the quality/latency tension is "
              "inherent or an artefact of one endpoint."))
    registry.add_provider(ProviderEntry(
        "openrouter", mock_tested=True, default_model="meta-llama/llama-3.1-8b-instruct",
        api_key_env="OPENROUTER_API_KEY",
        base_url="https://openrouter.ai/api/v1/chat/completions",
        notes="Several model families behind one key -- the cheapest route to satisfying "
              "judge independence properly."))
    registry.add_provider(ProviderEntry(
        "scripted", mock_tested=True, live_ok=True, default_model="scripted/model",
        notes="Deterministic test double. Never a production candidate."))
    return registry
