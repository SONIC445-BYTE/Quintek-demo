"""
The dynamic model registry: what each provider offers today, and what it
offered before.

WHY THIS EXISTS
---------------
On 2026-08-28 the two models frozen into the validator's Phase 0 experiment
answered `410 Gone -- has reached its end of life on 2026-08-26`. Nothing in
this repository noticed. `benchmark/candidates.py` could read a discovery
snapshot but nothing produced one; `configs/model_registry.json` was seeded by
hand; `provider_registry.default_registry()` named a now-dead model as
NVIDIA's default in Python source. Changing the model a live product calls
meant editing code and redeploying.

That is fine for an experiment and wrong for a product. Providers retire
models, add models, rename them, re-price them, change entitlements and have
outages, and none of that should require a deployment.

WHAT THIS IS NOT
----------------
It is not the freeze. `validator/freeze.py` pins an experiment's configuration
and refuses to let it move, which is the opposite requirement and remains the
opposite requirement. Production follows the catalogue; an experiment does
not. Those two systems meet in exactly one place -- `blocked_experiment_models`
below, which reports a frozen model that has since died so the experiment can
be marked BLOCKED rather than silently re-pointed at a substitute.

THREE FACTS THIS MODULE REFUSES TO CONFLATE
-------------------------------------------
**Catalogue presence is not availability.** Measured on this account: NVIDIA's
`/v1/models` listed 83 models on 2026-08-28, of which `openai/gpt-oss-120b`
timed out and `mistralai/mistral-7b-instruct-v0.3` returned 404. So a
catalogue observation may never set `AVAILABLE`; only a probe can.

**Absence from a catalogue is not retirement.** The same measurement, the
other way round: `nvidia/nemotron-mini-4b-instruct` and
`nvidia/llama-3.1-nemotron-nano-vl-8b-v1` were SERVING in the 2026-08-20
snapshot and absent from the 2026-08-28 listing. One disappearance is a
listing quirk. `RETIRED` therefore needs either an explicit 410 or
`absences_before_retired` consecutive absences -- and it records which.

**Unavailable is not bad.** A 402, a 429, a firewall and a timeout say nothing
about a model's quality. Only `benchmark/fitness.py` scores models, and it
never reads a state from this module as evidence.

HISTORY IS KEPT
---------------
A record is never deleted. A retired model keeps its `first_seen`, its probe
history and -- in `benchmark/inference_log.py`, which this module does not
touch -- every inference it ever served. "What was serving generation in
March, and why was it dropped" stays answerable after the model is gone.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .provider_status import ProviderStatus, classify

#: What each role needs, in ONE place, as declared requirements rather than a
#: list of models. `discovery/shortlists.json` used to spell out which models
#: served which role; a retirement made the file wrong and nothing said so.
#: Every tool that needs a model for a role resolves it from here plus the
#: registry, so there is one answer and it moves when the catalogue does.
ROLE_REQUIREMENTS = {
    "generation": {"required_capabilities": ("structured_output",),
                   "min_context": 32_000},
    # Reasoning is required because the measured failure was a validator with
    # none: llama-3.1-8b approved a question that contradicted its own source
    # passage. See docs/VALIDATOR.md.
    "validation": {"required_capabilities": ("structured_output", "reasoning"),
                   "min_context": 32_000},
    "vision": {"required_capabilities": ("structured_output", "vision")},
    # The floor: it answers, in text. For tools that need *a* working model
    # rather than a qualified one -- a smoke test, a provider comparison.
    "any": {"required_capabilities": ()},
}

DEFAULT_REGISTRY_PATH = Path("discovery/model_registry.json")
DEFAULT_POLICY_PATH = Path("configs/discovery.json")

#: Bumped when a stored record's shape changes, so a registry written by an
#: older version is recognised rather than misread.
METADATA_VERSION = 1


class Availability:
    """
    What is known about calling this model, as observed rather than claimed.

    `UNVERIFIED` is the state every model enters on first sighting and the
    reason catalogue discovery cannot promote anything on its own.
    """

    UNVERIFIED = "UNVERIFIED"
    AVAILABLE = "AVAILABLE"
    TEMPORARILY_UNAVAILABLE = "TEMPORARILY_UNAVAILABLE"
    BILLING_BLOCKED = "BILLING_BLOCKED"
    AUTH_FAILED = "AUTH_FAILED"
    RATE_LIMITED = "RATE_LIMITED"
    RETIRED = "RETIRED"
    NOT_SERVING = "NOT_SERVING"
    UNKNOWN = "UNKNOWN"


ALL_AVAILABILITY = (
    Availability.UNVERIFIED, Availability.AVAILABLE,
    Availability.TEMPORARILY_UNAVAILABLE, Availability.BILLING_BLOCKED,
    Availability.AUTH_FAILED, Availability.RATE_LIMITED, Availability.RETIRED,
    Availability.NOT_SERVING, Availability.UNKNOWN,
)

#: The one state nothing recovers from. Kept as a set rather than an equality
#: test so a future terminal state does not need every call site edited.
TERMINAL = frozenset({Availability.RETIRED})

#: States a re-probe could plausibly change. RETIRED is deliberately absent:
#: re-probing a withdrawn model is a scheduled waste with no reachable success.
RECHECKABLE = frozenset(ALL_AVAILABILITY) - TERMINAL - {Availability.AVAILABLE}

#: How a provider's failure classification lands in this vocabulary. Written
#: as a map rather than a chain of ifs because the two vocabularies exist for
#: different readers and the translation between them should be one table.
FROM_PROVIDER_STATUS = {
    ProviderStatus.AVAILABLE: Availability.AVAILABLE,
    ProviderStatus.DEGRADED: Availability.AVAILABLE,
    ProviderStatus.MODEL_RETIRED: Availability.RETIRED,
    ProviderStatus.BILLING_BLOCKED: Availability.BILLING_BLOCKED,
    ProviderStatus.AUTH_FAILED: Availability.AUTH_FAILED,
    ProviderStatus.RATE_LIMITED: Availability.RATE_LIMITED,
    ProviderStatus.TIMEOUT: Availability.TEMPORARILY_UNAVAILABLE,
    ProviderStatus.EGRESS_BLOCKED: Availability.TEMPORARILY_UNAVAILABLE,
    ProviderStatus.MODEL_UNAVAILABLE: Availability.NOT_SERVING,
    ProviderStatus.INVALID_RESPONSE: Availability.AVAILABLE,
    ProviderStatus.UNKNOWN_ERROR: Availability.UNKNOWN,
}


class Lifecycle:
    """
    Where a model stands on the road to serving production traffic.

    DERIVED, never stored. `benchmark/registry.py` has a written state machine
    for the promotion decision, which is right there because a promotion is a
    decision somebody makes. This is not a decision -- it is a summary of what
    is currently known -- and a stored summary drifts from the facts it
    summarises. Compute it and it cannot.

        UNVERIFIED -> PROBED -> QUALIFIED -> EVALUATED -> PRODUCTION_ELIGIBLE
                        |           |
                        |           +-> DISQUALIFIED  (probe showed it cannot)
                        +-> TEMPORARILY_UNAVAILABLE -> (recovers, resumes)
                        +-> RETIRED (terminal)
    """

    UNVERIFIED = "UNVERIFIED"
    PROBED = "PROBED"
    QUALIFIED = "QUALIFIED"
    DISQUALIFIED = "DISQUALIFIED"
    EVALUATED = "EVALUATED"
    PRODUCTION_ELIGIBLE = "PRODUCTION_ELIGIBLE"
    TEMPORARILY_UNAVAILABLE = "TEMPORARILY_UNAVAILABLE"
    RETIRED = "RETIRED"


class Provenance:
    """
    Where a capability claim came from. The distinction the whole
    qualification step exists to make.

    `DECLARED`  the provider's catalogue said so. Cheap, broad, and only as
                good as the provider's metadata.
    `OBSERVED`  a probe sent a real request and inspected the reply. The only
                source that can contradict a catalogue.
    `UNKNOWN`   nobody has said or shown anything. Not False.

    A name is never a source. "It is called `-vision-`" is not evidence that
    it accepts an image, and nothing in this repository treats it as any.
    """

    DECLARED = "DECLARED"
    OBSERVED = "OBSERVED"
    UNKNOWN = "UNKNOWN"


ALL_PROVENANCE = (Provenance.DECLARED, Provenance.OBSERVED, Provenance.UNKNOWN)


@dataclass
class CapabilityClaim:
    """
    One capability, what is believed about it, and on what basis.

    `value` is tri-state on purpose: True, False, and None for "not
    established". A probe that could not run -- the model 410'd, the endpoint
    timed out -- leaves None, because a failure to observe is not an
    observation of failure. Recording it as False would permanently disqualify
    a model for an outage.
    """

    value: bool | None = None
    source: str = Provenance.UNKNOWN
    at: str = ""
    evidence: str = ""
    probe_version: str = ""

    @property
    def observed(self) -> bool:
        return self.source == Provenance.OBSERVED

    @property
    def known(self) -> bool:
        return self.value is not None and self.source != Provenance.UNKNOWN

    def as_dict(self) -> dict:
        return {"value": self.value, "source": self.source, "at": self.at,
                "evidence": self.evidence, "probe_version": self.probe_version}

    @classmethod
    def coerce(cls, raw) -> "CapabilityClaim":
        """
        Accept a stored claim, a `CapabilityClaim`, or a bare bool.

        The bare bool is what a catalogue parser produces, and it is read as
        DECLARED without a timestamp -- which is exactly what it is. Being
        able to write `capabilities={"vision": True}` keeps catalogue adapters
        and tests readable; the provenance is still recorded, not lost.
        """
        if isinstance(raw, cls):
            return raw
        if isinstance(raw, dict):
            return cls(value=raw.get("value"),
                       source=raw.get("source", Provenance.UNKNOWN),
                       at=raw.get("at", ""), evidence=raw.get("evidence", ""),
                       probe_version=raw.get("probe_version", ""))
        if isinstance(raw, bool):
            return cls(value=raw, source=Provenance.DECLARED,
                       evidence="provider catalogue")
        return cls()


class Pricing:
    """
    Four states, because `price == 0.0` answers three different questions and
    gets two of them wrong.

    `UNKNOWN`  the provider does not publish a price for this model.
    `UNPRICED` the provider published a sentinel meaning "decided at request
               time" -- OpenRouter's `-1`. Read as a number it is the cheapest
               entry in the catalogue, which is how a variable-price router
               reached the head of every shortlist once already.
    `FREE`     the provider published a real zero.
    `PAID`     the provider published a positive price.
    """

    UNKNOWN = "UNKNOWN"
    UNPRICED = "UNPRICED"
    FREE = "FREE"
    PAID = "PAID"


ALL_PRICING = (Pricing.UNKNOWN, Pricing.UNPRICED, Pricing.FREE, Pricing.PAID)

#: Neither of these may win a cheapest-first sort. `sort_key` below enforces it.
NOT_A_PRICE = frozenset({Pricing.UNKNOWN, Pricing.UNPRICED})

# Event kinds emitted by reconciliation.
EVENT_DISCOVERED = "DISCOVERED"
EVENT_RETURNED = "RETURNED"
EVENT_ABSENT = "ABSENT_FROM_CATALOGUE"
EVENT_RETIRED = "RETIRED"
EVENT_METADATA_CHANGED = "METADATA_CHANGED"
EVENT_PROBED = "PROBED"
EVENT_CAPABILITY_PROBED = "CAPABILITY_PROBED"

#: Metadata fields a change to which is worth an event. Deliberately not
#: everything: `last_seen` moves on every discovery and an event per model per
#: run would bury the changes that matter.
WATCHED_METADATA = ("context_window", "input_price", "output_price",
                    "pricing_status", "capabilities", "entry_kind")

# Values shaped like credentials, refused on write. The registry is written to
# a file that gets committed and read by tools that print it; a provider that
# echoes a key back in an error body must not put it there.
CREDENTIAL_SHAPES = re.compile(
    r"(nvapi-[A-Za-z0-9_\-]{16,}"
    r"|sk-[A-Za-z0-9_\-]{16,}"
    r"|rzp_(?:live|test)_[A-Za-z0-9]{10,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|Bearer\s+[A-Za-z0-9._\-]{16,}"
    r"|ey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})")

FORBIDDEN_KEYS = ("api_key", "apikey", "token", "secret", "password",
                  "authorization")


class CredentialInRegistry(RuntimeError):
    """Something shaped like a key reached a file that gets committed."""


def scrub(value):
    """Refuse a registry that would carry a credential into a stored file."""
    if isinstance(value, str):
        match = CREDENTIAL_SHAPES.search(value)
        if match:
            raise CredentialInRegistry(
                f"refusing to store a value shaped like a credential "
                f"({match.group(0)[:8]}...). Record the environment variable NAME as "
                "credential_ref; the value belongs in the environment and nowhere in "
                "a file this repository writes.")
        return value
    if isinstance(value, dict):
        for key in value:
            if str(key).strip().lower() in FORBIDDEN_KEYS:
                raise CredentialInRegistry(
                    f"refusing to store a field named {key!r} in the model registry.")
        return {k: scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def price_state(value: float | None, *, stated: bool = True) -> str:
    """
    Classify a catalogue price into a `Pricing` state.

    `stated=False` means the provider published a field the caller could not
    read as a number -- a sentinel. That is UNPRICED, not UNKNOWN: the
    difference is whether the provider has a price and will not commit to it,
    or does not publish prices at all.
    """
    if not stated:
        return Pricing.UNPRICED
    if value is None:
        return Pricing.UNKNOWN
    if value < 0:
        return Pricing.UNPRICED
    if value == 0:
        return Pricing.FREE
    return Pricing.PAID


@dataclass
class DiscoveryPolicy:
    """
    How often to look, and how long to wait after a failure.

    Every interval is configuration, loaded from `configs/discovery.json`.
    None of it is a module constant, because "how often do we re-probe a
    rate-limited model" is an operational decision that changes with the
    provider and must not need a deployment.
    """

    catalogue_refresh_seconds: float = 21_600.0        # 6h
    availability_recheck_seconds: float = 86_400.0     # 24h
    retired_recheck_seconds: float | None = None       # never
    failed_backoff_seconds: float = 900.0              # 15m, then doubling
    failed_backoff_multiplier: float = 2.0
    failed_backoff_max_seconds: float = 86_400.0
    health_probe_seconds: float = 3_600.0              # 1h
    #: One absence is a listing quirk -- measured on this account, see the
    #: module docstring. Two consecutive absences is a signal.
    absences_before_retired: int = 2

    @classmethod
    def load(cls, path: str | Path = DEFAULT_POLICY_PATH) -> "DiscoveryPolicy":
        path = Path(path)
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        known = {f for f in cls.__dataclass_fields__}
        unknown = sorted(set(raw) - known)
        if unknown:
            raise ValueError(
                f"{path} names {unknown}, which {'is' if len(unknown) == 1 else 'are'} "
                f"not a discovery setting. Known settings: {', '.join(sorted(known))}. "
                "A misspelled interval that is silently ignored is an interval nobody "
                "notices is not being honoured.")
        return cls(**raw)

    def as_dict(self) -> dict:
        return asdict(self)

    def backoff_for(self, consecutive_failures: int) -> float:
        """Wait after N consecutive failed probes, capped."""
        if consecutive_failures <= 0:
            return 0.0
        wait = self.failed_backoff_seconds * (
            self.failed_backoff_multiplier ** (consecutive_failures - 1))
        return min(wait, self.failed_backoff_max_seconds)


@dataclass
class Event:
    """One thing that happened to one model, with when and why."""

    at: str
    kind: str
    key: str
    detail: str = ""

    def as_dict(self) -> dict:
        return {"at": self.at, "kind": self.kind, "key": self.key, "detail": self.detail}


@dataclass
class ModelRecord:
    """
    Everything observed about one `provider:model_id`, and when.

    Nothing here is inferred. A field the provider does not expose stays None,
    which is why `capabilities` maps to `bool | None` rather than `bool`:
    "we do not know" must never read as "yes", and it must not read as "no"
    either, or an unmeasured model is permanently ineligible.
    """

    provider: str
    model_id: str
    model_version: str = ""
    family: str = ""
    first_seen: str = ""
    last_seen: str = ""
    last_verified: str = ""
    catalogue_present: bool = False
    consecutive_absences: int = 0
    availability: str = Availability.UNVERIFIED
    availability_detail: str = ""
    last_probe_status: str = ""
    capabilities: dict = field(default_factory=dict)
    context_window: int | None = None
    input_price: float | None = None
    output_price: float | None = None
    pricing_status: str = Pricing.UNKNOWN
    entry_kind: str = "MODEL"
    latency_ms_last: float | None = None
    latency_ms_best: float | None = None
    probe_successes: int = 0
    probe_failures: int = 0
    consecutive_failures: int = 0
    retired_at: str = ""
    retirement_reason: str = ""
    source: str = ""
    credential_ref: str = ""
    #: When a capability probe last ran, and which version of it. Separate
    #: from `last_verified` (availability) because the two answer different
    #: questions and go stale at different rates: a model that answers today
    #: may have been capability-probed a month ago, and that is fine.
    capabilities_probed_at: str = ""
    capability_probe_version: str = ""
    #: How many capability probes could not be run because the model was
    #: unreachable. Distinguishes "we tried and it cannot" from "we tried and
    #: could not find out".
    capability_probes_inconclusive: int = 0
    metadata_version: int = METADATA_VERSION
    history: list = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.model_id}"

    @property
    def retired(self) -> bool:
        return self.availability in TERMINAL

    @property
    def recheckable(self) -> bool:
        """Could a probe change this state? Never true for a retired model."""
        return self.availability in RECHECKABLE

    @property
    def priced(self) -> bool:
        return self.pricing_status not in NOT_A_PRICE

    def lifecycle(self, *, requirements=None, evaluated: bool = False) -> str:
        """
        The derived stage, against a role's requirements if one is given.

        `evaluated` comes from the caller because benchmark evidence lives in
        `benchmark/inference_log.py`, not here -- a registry that also counted
        observations would be a second, disagreeing source for the number the
        scoreboard already owns.
        """
        if self.retired:
            return Lifecycle.RETIRED
        if self.availability in (Availability.TEMPORARILY_UNAVAILABLE,
                                 Availability.RATE_LIMITED,
                                 Availability.BILLING_BLOCKED,
                                 Availability.AUTH_FAILED,
                                 Availability.NOT_SERVING):
            return Lifecycle.TEMPORARILY_UNAVAILABLE
        if self.availability != Availability.AVAILABLE:
            return Lifecycle.UNVERIFIED
        if requirements is None:
            return Lifecycle.PROBED
        unmet = self.unmet(requirements)
        if any("unknown" in reason for reason in unmet):
            return Lifecycle.PROBED
        if unmet:
            return Lifecycle.DISQUALIFIED
        if not evaluated:
            return Lifecycle.QUALIFIED
        return Lifecycle.PRODUCTION_ELIGIBLE

    def capability(self, name: str) -> CapabilityClaim:
        """
        What is believed about one capability, and why. Never a bare bool.

        An absent entry returns an UNKNOWN claim rather than None, so every
        call site handles "nobody has established this" the same way instead
        of each inventing its own default.
        """
        return CapabilityClaim.coerce(self.capabilities.get(name))

    def set_capability(self, name: str, claim: CapabilityClaim) -> None:
        self.capabilities[name] = claim.as_dict()

    @property
    def probed_capabilities(self) -> list[str]:
        return sorted(n for n in self.capabilities if self.capability(n).observed)

    def unmet(self, required, *, require_observed: bool = False) -> list[str]:
        """
        Which of `required` this record cannot be shown to have, and why.

        Two failures kept apart in the wording, because they need different
        actions: "unknown" is fixed by running a probe, "does not support" is
        fixed by choosing another model.
        """
        out = []
        for name in required:
            claim = self.capability(name)
            if claim.value is True:
                if require_observed and not claim.observed:
                    out.append(f"{name} is {claim.source.lower()}, not observed")
                continue
            if claim.value is None:
                out.append(f"{name} unknown, and unknown is not yes")
            else:
                out.append(f"does not support {name}")
        return out

    def note(self, kind: str, detail: str = "", *, at: str = "") -> Event:
        event = Event(at=at or now_iso(), kind=kind, key=self.key, detail=detail)
        self.history.append(event.as_dict())
        return event

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "ModelRecord":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})


@dataclass
class Reconciliation:
    """What one discovery run changed, as a report rather than a log line."""

    provider: str
    at: str
    discovered: list = field(default_factory=list)
    returned: list = field(default_factory=list)
    absent: list = field(default_factory=list)
    retired: list = field(default_factory=list)
    changed: list = field(default_factory=list)
    unchanged: int = 0

    def as_dict(self) -> dict:
        return {"provider": self.provider, "at": self.at,
                "discovered": list(self.discovered), "returned": list(self.returned),
                "absent": list(self.absent), "retired": list(self.retired),
                "changed": list(self.changed), "unchanged": self.unchanged}

    def render(self) -> str:
        lines = [f"{self.provider}  {self.at}"]
        for label, items in (("new", self.discovered), ("returned", self.returned),
                             ("absent", self.absent), ("RETIRED", self.retired),
                             ("changed", self.changed)):
            if items:
                lines.append(f"  {label:<9} {len(items):>4}  {', '.join(sorted(items)[:6])}"
                             + (" ..." if len(items) > 6 else ""))
        lines.append(f"  unchanged {self.unchanged:>4}")
        return "\n".join(lines)


@dataclass
class Observation:
    """
    One catalogue row, normalised. Produced by `benchmark/provider_catalogue.py`.

    Deliberately dumb: it carries what the provider said and nothing about
    whether the model works. `availability` is not a field here, because a
    catalogue is not allowed to set it.
    """

    provider: str
    model_id: str
    model_version: str = ""
    family: str = ""
    context_window: int | None = None
    input_price: float | None = None
    output_price: float | None = None
    price_stated: bool = True
    capabilities: dict = field(default_factory=dict)
    entry_kind: str = "MODEL"
    source: str = ""

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.model_id}"


class DynamicModelRegistry:
    """
    The persistent store. JSON-file-backed and written atomically, matching
    `benchmark/registry.py` -- small, infrequently written, and cheaper than a
    database for what it is.

    A record is added and updated. It is never removed: `RETIRED` is a state,
    not a deletion, because a benchmark result that names a model nobody can
    look up is a benchmark result nobody can audit.
    """

    def __init__(self, path: str | Path = DEFAULT_REGISTRY_PATH,
                 *, policy: DiscoveryPolicy | None = None):
        self.path = Path(path)
        self.policy = policy or DiscoveryPolicy()
        self._records: dict[str, ModelRecord] = {}
        self.events: list[Event] = []
        if self.path.exists():
            self._load()

    # ---------- persistence ----------

    def _load(self) -> None:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        for key, record in (raw.get("models") or {}).items():
            self._records[key] = ModelRecord.from_dict(record)

    def save(self) -> Path:
        payload = scrub({
            "metadata_version": METADATA_VERSION,
            "written_at": now_iso(),
            "models": {k: r.as_dict() for k, r in sorted(self._records.items())},
        })
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.path)
        return self.path

    # ---------- reads ----------

    def get(self, key: str) -> ModelRecord | None:
        return self._records.get(key)

    def all(self) -> list[ModelRecord]:
        return [self._records[k] for k in sorted(self._records)]

    def by_provider(self, provider: str) -> list[ModelRecord]:
        return [r for r in self.all() if r.provider == provider]

    def by_availability(self, status: str) -> list[ModelRecord]:
        return [r for r in self.all() if r.availability == status]

    def retired(self) -> list[ModelRecord]:
        return [r for r in self.all() if r.retired]

    # ---------- reconciliation ----------

    def reconcile(self, provider: str, observations, *, at: str = "",
                  source: str = "") -> Reconciliation:
        """
        Fold one provider's current catalogue into the stored registry.

        Four outcomes per model, each recorded as an event on the record:
        first sighting, reappearance, continued presence with changed
        metadata, and absence. Absence is counted, not acted on immediately --
        see the module docstring for why one disappearance is not retirement.

        A catalogue observation may raise `catalogue_present` and update
        metadata. It may never set `AVAILABLE`; only `record_probe` does that.
        """
        at = at or now_iso()
        report = Reconciliation(provider=provider, at=at)
        seen: set[str] = set()

        for observation in observations:
            if observation.provider != provider:
                raise ValueError(
                    f"observation {observation.key!r} was handed to the {provider!r} "
                    "reconciliation. Mixing providers in one pass makes every absence "
                    "below a false positive.")
            seen.add(observation.key)
            record = self._records.get(observation.key)
            if record is None:
                record = ModelRecord(
                    provider=observation.provider, model_id=observation.model_id,
                    first_seen=at, source=source or observation.source)
                self._records[record.key] = record
                self._apply_metadata(record, observation)
                record.catalogue_present = True
                record.last_seen = at
                record.consecutive_absences = 0
                self.events.append(record.note(
                    EVENT_DISCOVERED, "first sighting in this provider's catalogue", at=at))
                report.discovered.append(record.key)
                continue

            returning = not record.catalogue_present
            changes = self._apply_metadata(record, observation)
            record.catalogue_present = True
            record.last_seen = at
            record.consecutive_absences = 0

            if returning:
                # A model that came back is NOT automatically available again:
                # it returns to whatever its last probe said, or UNVERIFIED if
                # a retirement was recorded on absence alone and is now
                # contradicted by the catalogue.
                if record.retired and "410" not in record.retirement_reason:
                    record.availability = Availability.UNVERIFIED
                    record.availability_detail = (
                        "reappeared in the catalogue after being retired on absence "
                        "alone; awaiting a probe")
                    record.retired_at = ""
                    record.retirement_reason = ""
                self.events.append(record.note(
                    EVENT_RETURNED, "present again after being absent", at=at))
                report.returned.append(record.key)
            elif changes:
                self.events.append(record.note(
                    EVENT_METADATA_CHANGED, "; ".join(changes), at=at))
                report.changed.append(record.key)
            else:
                report.unchanged += 1

        for record in self.by_provider(provider):
            if record.key in seen or record.retired:
                continue
            record.catalogue_present = False
            record.consecutive_absences += 1
            if record.consecutive_absences >= self.policy.absences_before_retired:
                self._retire(record, at=at, reason=(
                    f"absent from the {provider} catalogue on "
                    f"{record.consecutive_absences} consecutive discovery runs"))
                report.retired.append(record.key)
            else:
                self.events.append(record.note(
                    EVENT_ABSENT,
                    f"absent from the catalogue ({record.consecutive_absences} of "
                    f"{self.policy.absences_before_retired} before retirement)", at=at))
                report.absent.append(record.key)

        return report

    @staticmethod
    def _apply_metadata(record: ModelRecord, observation: Observation) -> list[str]:
        """Copy catalogue metadata onto a record, naming what moved."""
        before = {f: getattr(record, f) for f in WATCHED_METADATA}
        record.model_version = observation.model_version or record.model_version
        record.family = observation.family or record.family
        record.entry_kind = observation.entry_kind
        record.context_window = observation.context_window
        record.input_price = (observation.input_price
                              if observation.price_stated and
                              (observation.input_price or 0) >= 0 else None)
        record.output_price = (observation.output_price
                               if observation.price_stated and
                               (observation.output_price or 0) >= 0 else None)
        record.pricing_status = price_state(observation.input_price,
                                            stated=observation.price_stated)
        for name, raw in (observation.capabilities or {}).items():
            existing = record.capability(name)
            if existing.observed:
                # A catalogue may not overwrite a probe. The catalogue is what
                # the provider says; the probe is what happened. When they
                # disagree the probe is the one that sent a request.
                continue
            record.set_capability(name, CapabilityClaim.coerce(raw))
        if observation.source:
            record.source = observation.source
        return [f"{f}: {before[f]!r} -> {getattr(record, f)!r}"
                for f in WATCHED_METADATA if before[f] != getattr(record, f)]

    def _retire(self, record: ModelRecord, *, at: str, reason: str) -> None:
        record.availability = Availability.RETIRED
        record.availability_detail = reason
        record.retired_at = at
        record.retirement_reason = reason
        record.catalogue_present = False
        self.events.append(record.note(EVENT_RETIRED, reason, at=at))

    # ---------- probing ----------

    def record_probe(self, key: str, *, error=None, http_status: int | None = None,
                     latency_ms: float | None = None, at: str = "",
                     credential_ref: str = "") -> ModelRecord:
        """
        Fold the result of one real call into the record.

        Classification is delegated to `provider_status.classify` so a probe
        and a production call reach the same conclusion from the same evidence
        -- two classifiers would eventually disagree, and the disagreement
        would show up as a model that production avoids and discovery keeps
        recommending.
        """
        record = self._records.get(key)
        if record is None:
            raise KeyError(
                f"no record for {key!r}. Probe results are folded onto a model the "
                "registry has already seen in a catalogue; probing an id nothing "
                "listed would record availability for a model with no provenance.")
        at = at or now_iso()
        status = classify(error, http_status=http_status)
        availability = FROM_PROVIDER_STATUS.get(status, Availability.UNKNOWN)

        record.last_probe_status = status
        record.last_verified = at
        record.credential_ref = credential_ref or record.credential_ref
        detail = _detail_of(error)

        if availability == Availability.AVAILABLE:
            record.probe_successes += 1
            record.consecutive_failures = 0
            record.latency_ms_last = latency_ms
            if latency_ms is not None:
                record.latency_ms_best = (latency_ms if record.latency_ms_best is None
                                          else min(record.latency_ms_best, latency_ms))
            record.availability = Availability.AVAILABLE
            record.availability_detail = ""
        else:
            record.probe_failures += 1
            record.consecutive_failures += 1
            record.latency_ms_last = latency_ms
            if availability == Availability.RETIRED:
                self._retire(record, at=at, reason=(
                    f"provider answered 410 / end-of-life"
                    + (f": {detail[:160]}" if detail else "")))
                return record
            record.availability = availability
            record.availability_detail = detail[:300]

        self.events.append(record.note(
            EVENT_PROBED, f"{status} -> {record.availability}"
                          + (f" ({latency_ms:.0f}ms)" if latency_ms is not None else ""),
            at=at))
        return record

    def record_capability_probe(self, key: str, results, *, at: str = "",
                                probe_version: str = "") -> ModelRecord:
        """
        Fold the result of a capability probe run onto the record.

        `results` maps a capability name to a `CapabilityClaim`. A claim whose
        value is None -- the probe could not run -- is counted as inconclusive
        and does NOT overwrite an existing claim. Letting an outage erase a
        good observation is how a model gets disqualified for a bad afternoon.
        """
        record = self._records.get(key)
        if record is None:
            raise KeyError(
                f"no record for {key!r}. A capability probe is folded onto a model "
                "the registry has already seen, so the observation has provenance.")
        at = at or now_iso()
        observed, inconclusive = [], []
        for name, claim in results.items():
            claim = CapabilityClaim.coerce(claim)
            if claim.value is None:
                inconclusive.append(name)
                continue
            record.set_capability(name, CapabilityClaim(
                value=claim.value, source=Provenance.OBSERVED, at=at,
                evidence=claim.evidence, probe_version=probe_version))
            observed.append(f"{name}={claim.value}")
        record.capability_probes_inconclusive += len(inconclusive)
        if observed:
            record.capabilities_probed_at = at
            record.capability_probe_version = probe_version
        detail = ", ".join(observed) or "nothing established"
        if inconclusive:
            detail += f"; inconclusive: {', '.join(sorted(inconclusive))}"
        self.events.append(record.note(EVENT_CAPABILITY_PROBED, detail, at=at))
        return record

    def due_for_capability_probe(self, requirements, *, providers=None,
                                 limit: int | None = None) -> list[ModelRecord]:
        """
        The funnel, as a query: which models is it worth spending capability
        probes on right now?

        Cheap filters first, and every one of them is a reason NOT to spend a
        call:

            AVAILABLE            an unreachable model teaches nothing
            not a router         a router is not a single model
            has an unknown among the capabilities this role needs
            has not already been shown to lack one of them

        The last two are what stop this probing 83 models to re-learn 83
        answers. A model whose reasoning claim is already False stays out;
        a model whose claim is unknown is exactly what a probe is for.
        """
        required = tuple(requirements)
        out = []
        for record in self.all():
            if providers and record.provider not in providers:
                continue
            if record.availability != Availability.AVAILABLE:
                continue
            if record.entry_kind in ("ROUTER", "AGGREGATOR"):
                continue
            claims = [record.capability(name) for name in required]
            if any(c.value is False for c in claims):
                continue
            if not any(c.value is None for c in claims):
                continue
            out.append(record)
        out.sort(key=self.sort_key)
        return out[:limit] if limit else out

    # ---------- scheduling ----------

    def due_for_recheck(self, *, now: datetime | None = None,
                        policy: DiscoveryPolicy | None = None) -> list[ModelRecord]:
        """
        Which models a probe run should spend calls on, cheapest reason first.

        A retired model is never due -- unless an operator has set
        `retired_recheck_seconds`, which exists so a provider that un-retires
        something (it happens) is not permanently invisible, and defaults to
        never because it usually does not.
        """
        policy = policy or self.policy
        now = now or datetime.now(timezone.utc)
        due = []
        for record in self.all():
            interval = self._interval_for(record, policy)
            if interval is None:
                continue
            last = parse_iso(record.last_verified)
            if last is None:
                due.append(record)
                continue
            if now - last >= timedelta(seconds=interval):
                due.append(record)
        return due

    @staticmethod
    def _interval_for(record: ModelRecord, policy: DiscoveryPolicy) -> float | None:
        """Seconds to wait before re-probing, or None for never."""
        if record.retired:
            return policy.retired_recheck_seconds
        if record.consecutive_failures:
            return policy.backoff_for(record.consecutive_failures)
        if record.availability == Availability.AVAILABLE:
            return policy.availability_recheck_seconds
        return policy.availability_recheck_seconds

    def catalogue_is_stale(self, provider: str, *, now: datetime | None = None,
                           policy: DiscoveryPolicy | None = None) -> bool:
        policy = policy or self.policy
        now = now or datetime.now(timezone.utc)
        seen = [parse_iso(r.last_seen) for r in self.by_provider(provider)]
        seen = [s for s in seen if s is not None]
        if not seen:
            return True
        return now - max(seen) >= timedelta(seconds=policy.catalogue_refresh_seconds)

    # ---------- selection ----------

    def eligible(self, *, required_capabilities=(), min_context: int | None = None,
                 max_input_price: float | None = None,
                 allow_unknown_capabilities: bool = False,
                 allow_unpriced: bool = False, require_observed: bool = False,
                 providers=None) -> tuple[list[ModelRecord], list[dict]]:
        """
        The production funnel: which records may serve work right now.

        Selection reads measured and declared attributes only. Nothing in this
        method looks at a model's name, its vendor, or its reputation -- see
        `test_no_selection_depends_on_a_model_or_vendor_name`, which asserts it
        by renaming every model and requiring the same answer.

        Two rules that look alike and are not:
          * unknown capability -> not eligible by default, because "we do not
            know" must not read as "yes";
          * unknown capability -> not permanently excluded, because the record
            stays in the registry and a probe can change it. This method is a
            view, never a state change.
        """
        required = tuple(required_capabilities)
        kept, dropped = [], []
        for record in self.all():
            if providers and record.provider not in providers:
                continue
            reasons = []
            if record.retired:
                reasons.append(f"retired: {record.retirement_reason or 'withdrawn'}")
            elif record.availability != Availability.AVAILABLE:
                reasons.append(
                    f"availability is {record.availability}"
                    + (f" ({record.availability_detail[:80]})"
                       if record.availability_detail else ""))
            if record.entry_kind in ("ROUTER", "AGGREGATOR"):
                reasons.append(f"is a {record.entry_kind.lower()}, not a single model")
            for reason in record.unmet(required, require_observed=require_observed):
                if "unknown" in reason and allow_unknown_capabilities:
                    continue
                reasons.append(reason)
            if min_context is not None:
                if record.context_window is None:
                    if not allow_unknown_capabilities:
                        reasons.append("context window unknown")
                elif record.context_window < min_context:
                    reasons.append(
                        f"context {record.context_window:,} < {min_context:,}")
            if max_input_price is not None:
                if not record.priced:
                    if not allow_unpriced:
                        reasons.append(
                            f"price is {record.pricing_status}, so it cannot be shown "
                            "to be within budget")
                elif (record.input_price or 0.0) > max_input_price:
                    reasons.append(
                        f"input ${record.input_price:.2f}/M exceeds "
                        f"${max_input_price:.2f}/M")
            if reasons:
                dropped.append({"key": record.key, "reasons": reasons,
                                "availability": record.availability})
            else:
                kept.append(record)
        return kept, dropped

    @staticmethod
    def sort_key(record: ModelRecord):
        """
        Cheapest first, then widest context, then id.

        `not record.priced` leads the tuple so UNKNOWN and UNPRICED sort LAST.
        Sorting them by their numeric price would put every model whose price
        nobody published at the head of the list.
        """
        return (not record.priced, record.input_price or 0.0,
                -(record.context_window or 0), record.key)

    def shortlist(self, *, limit: int = 30, **requirements) -> list[ModelRecord]:
        """
        A role's candidates, generated rather than written down.

        `discovery/shortlists.json` was a committed artifact: three roles with
        their models spelled out, which meant a retirement made it wrong and
        nothing said so. This regenerates the same three lists from the
        registry and the requirements, so the answer moves when the catalogue
        does.
        """
        kept, _dropped = self.eligible(**requirements)
        return sorted(kept, key=self.sort_key)[:limit]

    def resolve(self, role: str = "any", *, provider: str | None = None,
                require_observed: bool = False,
                exclude=()) -> ModelRecord | None:
        """
        The best currently-eligible model for a role, or None.

        This is what replaces a hard-coded model id in a tool. None is a real
        answer and callers must handle it: "nothing currently qualifies" is
        the correct output when a provider has retired everything that did,
        and inventing a default there is how a tool spends a budget calling a
        model that answers 410.
        """
        if role not in ROLE_REQUIREMENTS:
            raise ValueError(
                f"unknown role {role!r}; known roles: "
                f"{', '.join(sorted(ROLE_REQUIREMENTS))}")
        requirements = dict(ROLE_REQUIREMENTS[role])
        if provider:
            requirements["providers"] = (provider,)
        requirements["require_observed"] = require_observed
        excluded = set(exclude)
        for record in self.shortlist(**requirements):
            if record.key not in excluded:
                return record
        return None

    def named_retired(self, text: str) -> list[str]:
        """
        Which retired model ids appear in a blob of text.

        Used by the repository guard test. Substring matching is crude and
        deliberately so: the question is not "does this parse as a model
        reference" but "could a reader or a default value hand this id to a
        provider", and a retired id in a string literal can.
        """
        return sorted(record.model_id for record in self.retired()
                      if record.model_id and record.model_id in text)

    # ---------- the one place production and experiments meet ----------

    def blocked_experiment_models(self, models) -> list[dict]:
        """
        Which of a frozen experiment's models can no longer be called.

        Production replaces a dead model automatically; an experiment must
        not. So this REPORTS rather than substitutes, and the caller marks the
        experiment BLOCKED. Returning a replacement here is the one thing that
        would make a frozen manifest meaningless.

        `models` is any iterable of dicts carrying `provider` and `model` --
        the shape `validator/freeze.py` stores.
        """
        blocked = []
        for entry in models:
            provider = str(entry.get("provider", ""))
            model_id = str(entry.get("model", entry.get("model_id", "")))
            record = self.get(f"{provider}:{model_id}")
            if record is None:
                continue
            if record.availability in (Availability.AVAILABLE, Availability.UNVERIFIED):
                continue
            blocked.append({
                "seat": entry.get("seat", ""), "key": record.key,
                "availability": record.availability,
                "detail": record.retirement_reason or record.availability_detail,
                "observed_at": record.last_verified,
                "terminal": record.retired})
        return blocked

    # ---------- reporting ----------

    def report(self) -> dict:
        counts: dict[str, int] = {}
        for record in self.all():
            counts[record.availability] = counts.get(record.availability, 0) + 1
        return {
            "metadata_version": METADATA_VERSION,
            "models": len(self._records),
            "by_availability": counts,
            "providers": sorted({r.provider for r in self.all()}),
            "retired": [{"key": r.key, "at": r.retired_at,
                         "reason": r.retirement_reason} for r in self.retired()],
            "policy": self.policy.as_dict(),
        }

    def render(self) -> str:
        data = self.report()
        lines = [f"MODEL REGISTRY  {data['models']} model(s) across "
                 f"{len(data['providers'])} provider(s)", ""]
        for status in ALL_AVAILABILITY:
            if data["by_availability"].get(status):
                lines.append(f"  {status:<26}{data['by_availability'][status]:>5}")
        if data["retired"]:
            lines.append("")
            lines.append("RETIRED")
            for row in data["retired"]:
                lines.append(f"  {row['key']:<48} {row['at']}")
                lines.append(f"      {row['reason'][:100]}")
        return "\n".join(lines)


def _detail_of(error) -> str:
    if isinstance(error, BaseException):
        return f"{type(error).__name__}: {error}"
    return str(error) if error else ""
