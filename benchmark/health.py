"""
Provider health, and the circuit breaker that stops a dead endpoint being
hammered.

Written directly from a measurement. `meta/llama-3.1-70b-instruct` on NVIDIA
serverless produced per-item latencies of 7s, 15s, 30s, 36s, 65s, 103s and
then an indefinite stall. The batch kept issuing requests into that stall
because nothing was watching. The result was an experiment that measured the
endpoint rather than the model, and cost an hour to find out.

THE DISTINCTION THIS MODULE EXISTS TO PRESERVE
----------------------------------------------
    MODEL QUALITY  ≠  PROVIDER PERFORMANCE  ≠  INFRASTRUCTURE PERFORMANCE

A model is not bad because its host is slow. `llama-3.1-70b` caught 10 of 10
adversarial questions; on that evidence it is the better *model*. It is also
unusable interactively on that *endpoint*. Those are two findings, and a
system that folds them into one number will throw away a good model because
somebody's GPU pool was busy.

So health is tracked per `provider:model` and never written back into any
quality score. `benchmark/fitness.py` combines them at the point of decision,
where the combination can be explained.

THE BREAKER
-----------
Three consecutive failures opens the circuit. While open, calls are refused
immediately rather than queued behind a timeout -- the whole point is to stop
paying 180 seconds to learn what the last three calls already established.
After a cooldown the breaker goes HALF_OPEN and lets exactly one probe
through: success closes it, failure reopens it with a longer cooldown.

Timeouts count double toward opening, because a timeout costs the full
timeout budget while an error is usually instant. Ten timeouts is a much
worse morning than ten 400s.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field

CLOSED, OPEN, HALF_OPEN = "CLOSED", "OPEN", "HALF_OPEN"

# Provider lifecycle, distinct from the MODEL lifecycle in registry.py.
# A provider can be healthy while every model on it is unevaluated, and a
# model can be excellent on a provider that is not fit to serve production.
UNTESTED = "UNTESTED"        # nothing has ever been run through it
PROTOTYPE = "PROTOTYPE"      # works for single requests; not for batch
QUALIFIED = "QUALIFIED"      # meets latency and reliability requirements
DEGRADED = "DEGRADED"        # was qualified, currently failing
UNAVAILABLE = "UNAVAILABLE"  # cannot be reached at all

PROVIDER_STATES = (UNTESTED, PROTOTYPE, QUALIFIED, DEGRADED, UNAVAILABLE)


@dataclass
class BreakerPolicy:
    failure_threshold: int = 3
    # A timeout counts double: it costs the full budget, an error costs nothing.
    timeout_weight: int = 2
    cooldown_seconds: float = 60.0
    # Each successive reopen waits longer, so a persistently dead endpoint is
    # probed less and less rather than at a fixed drumbeat.
    cooldown_multiplier: float = 2.0
    max_cooldown_seconds: float = 900.0
    # Latency above which a call counts as unhealthy even though it succeeded.
    # A 103-second success is not a success for an interactive product.
    slow_call_ms: float | None = None


@dataclass
class CircuitBreaker:
    """One breaker per `provider:model`."""

    key: str
    policy: BreakerPolicy = field(default_factory=BreakerPolicy)
    state: str = CLOSED
    failure_weight: int = 0
    consecutive_successes: int = 0
    opened_at: float | None = None
    current_cooldown: float | None = None
    trips: int = 0
    last_error: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ---------- gate ----------

    def allows(self, *, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        with self._lock:
            if self.state == CLOSED:
                return True
            if self.state == HALF_OPEN:
                return True     # exactly one probe is expected to be in flight
            cooldown = self.current_cooldown or self.policy.cooldown_seconds
            if self.opened_at is not None and now - self.opened_at >= cooldown:
                self.state = HALF_OPEN
                return True
            return False

    def refusal_reason(self, *, now: float | None = None) -> str:
        now = now if now is not None else time.monotonic()
        cooldown = self.current_cooldown or self.policy.cooldown_seconds
        remaining = max(0.0, cooldown - (now - (self.opened_at or now)))
        return (f"circuit open for {self.key} after {self.trips} trip(s); "
                f"retrying in {remaining:.0f}s. Last error: {self.last_error or 'unknown'}")

    # ---------- outcomes ----------

    def record_success(self, *, latency_ms: float | None = None,
                       now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        slow = (self.policy.slow_call_ms is not None and latency_ms is not None
                and latency_ms > self.policy.slow_call_ms)
        with self._lock:
            if slow:
                # Succeeded, but too slowly to count as health.
                self.failure_weight += 1
                self.consecutive_successes = 0
                self._maybe_open(now, f"slow call: {latency_ms:.0f}ms")
                return
            self.consecutive_successes += 1
            self.failure_weight = 0
            if self.state in (HALF_OPEN, OPEN):
                self.state = CLOSED
                self.opened_at = None
                self.current_cooldown = None

    def record_failure(self, *, timeout: bool = False, error: str = "",
                       now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        with self._lock:
            self.consecutive_successes = 0
            self.failure_weight += self.policy.timeout_weight if timeout else 1
            self.last_error = error or ("timeout" if timeout else "error")
            self._maybe_open(now, self.last_error)

    def _maybe_open(self, now: float, reason: str) -> None:
        """Caller holds the lock."""
        if self.failure_weight < self.policy.failure_threshold:
            return
        was_open = self.state != CLOSED
        self.state = OPEN
        self.opened_at = now
        self.trips += 1
        self.last_error = reason
        base = self.current_cooldown if was_open else None
        cooldown = (base or self.policy.cooldown_seconds)
        if was_open:
            cooldown *= self.policy.cooldown_multiplier
        self.current_cooldown = min(cooldown, self.policy.max_cooldown_seconds)
        self.failure_weight = 0

    def as_dict(self) -> dict:
        return {"key": self.key, "state": self.state, "trips": self.trips,
                "failure_weight": self.failure_weight,
                "consecutive_successes": self.consecutive_successes,
                "cooldown_seconds": self.current_cooldown, "last_error": self.last_error}


class HealthRegistry:
    """
    Breakers plus rolling health, keyed by `provider:model`.

    Health is computed from the last `window` inferences rather than all of
    history: an endpoint that was terrible last month and is fine today should
    be usable today, and one that was fine last month and is failing now must
    not be protected by its record.
    """

    def __init__(self, *, policy: BreakerPolicy | None = None, window: int = 20):
        self.policy = policy or BreakerPolicy()
        self.window = window
        self._breakers: dict[str, CircuitBreaker] = {}
        self._recent: dict[str, deque] = {}
        self._states: dict[str, str] = {}
        self._lock = threading.Lock()

    def breaker(self, key: str) -> CircuitBreaker:
        with self._lock:
            if key not in self._breakers:
                self._breakers[key] = CircuitBreaker(key, self.policy)
                self._recent[key] = deque(maxlen=self.window)
            return self._breakers[key]

    def allows(self, key: str) -> bool:
        return self.breaker(key).allows()

    def refusal_reason(self, key: str) -> str:
        return self.breaker(key).refusal_reason()

    def observe(self, key: str, *, success: bool, latency_ms: float | None = None,
                timeout: bool = False, error: str = "",
                status: str | None = None) -> dict:
        """
        Record one call. Returns the verdict, so a caller can act on it.

        `status` may be supplied by a caller that already classified the
        failure; otherwise it is derived from the error text. The verdict
        decides what happens to the circuit, because the classes demand
        different things:

          EGRESS_BLOCKED  -> open, and stay open. Retrying cannot help, so a
                             cooldown that reopens the circuit every 60s just
                             wastes calls learning the same thing.
          RATE_LIMITED    -> do NOT open. The provider is healthy; we are
                             being greedy. Opening would take a working
                             provider out of rotation for our own mistake.
          TIMEOUT         -> open after the threshold, with a cooldown.
          INVALID_RESPONSE-> do not open; the host is fine, the model is not.

        The uniform "three failures and open for 60 seconds" this replaces
        treated a firewall and a burst of traffic identically.
        """
        from .provider_status import ProviderStatus, Verdict, assess, policy_for

        if status:
            verdict = Verdict(status=status, policy=policy_for(status), detail=error)
        else:
            verdict = assess(error or None, latency_ms=latency_ms,
                             slow_threshold_ms=self.policy.slow_call_ms)

        # A failure must never classify as AVAILABLE. Without this, a caller
        # reporting `success=False, timeout=True` and no error text produced a
        # verdict of AVAILABLE, whose policy does not open circuits -- so the
        # failure was recorded nowhere and the breaker never tripped. Silent
        # loss of a failure signal is the worst outcome this module can have.
        if not success and verdict.ok:
            fallback = (ProviderStatus.TIMEOUT if timeout else ProviderStatus.UNKNOWN_ERROR)
            verdict = Verdict(status=fallback, policy=policy_for(fallback),
                              detail=error or ("timeout" if timeout else "unspecified error"))

        breaker = self.breaker(key)
        if success and verdict.ok:
            breaker.record_success(latency_ms=latency_ms)
        elif verdict.policy.open_circuit:
            breaker.record_failure(
                timeout=timeout or verdict.status == ProviderStatus.TIMEOUT,
                error=verdict.detail or verdict.status)
            if verdict.policy.circuit_seconds is None and breaker.state == OPEN:
                # "Until an operator intervenes" -- there is no cooldown that
                # makes a policy denial or a rejected credential succeed.
                breaker.current_cooldown = float("inf")
            elif verdict.policy.circuit_seconds is not None and breaker.state == OPEN:
                breaker.current_cooldown = min(
                    breaker.current_cooldown or verdict.policy.circuit_seconds,
                    verdict.policy.circuit_seconds)
        # Everything else (rate limits, invalid responses) is recorded but
        # deliberately does not touch the circuit.

        with self._lock:
            self._recent[key].append(
                {"success": success, "latency_ms": latency_ms, "timeout": timeout,
                 "status": verdict.status,
                 "environmental": verdict.environmental})
            if verdict.status in (ProviderStatus.EGRESS_BLOCKED,
                                  ProviderStatus.AUTH_FAILED,
                                  ProviderStatus.MODEL_UNAVAILABLE):
                self._states[key] = UNAVAILABLE
        return verdict.as_dict()

    def declare(self, key: str, state: str) -> None:
        """
        Set a provider's declared state, e.g. marking an endpoint PROTOTYPE.

        Declared rather than inferred: "this endpoint is for single requests,
        not batch" is an operator's judgement about what it is FOR, which no
        amount of latency data can establish on its own.
        """
        if state not in PROVIDER_STATES:
            raise ValueError(f"unknown provider state {state!r}; "
                             f"expected one of {', '.join(PROVIDER_STATES)}")
        with self._lock:
            self._states[key] = state

    def declared_state(self, key: str) -> str:
        return self._states.get(key, UNTESTED)

    def health(self, key: str) -> dict:
        """
        Rolling health for one candidate. Every rate carries its n.

        `observed_state` is derived from the window; `declared_state` is what
        an operator said. They are reported side by side rather than merged,
        because "measured as slow" and "designated a prototype" are different
        claims and the difference decides what you do about it.
        """
        breaker = self.breaker(key)
        with self._lock:
            recent = list(self._recent.get(key, ()))
        n = len(recent)
        successes = sum(1 for r in recent if r["success"])
        timeouts = sum(1 for r in recent if r["timeout"])
        # Failures that say nothing about the model or the adapter. Counted
        # separately so a firewall never reads as a quality signal.
        environmental = sum(1 for r in recent if r.get("environmental"))
        statuses: dict[str, int] = {}
        for r in recent:
            if r.get("status"):
                statuses[r["status"]] = statuses.get(r["status"], 0) + 1
        latencies = sorted(r["latency_ms"] for r in recent
                           if r["success"] and r["latency_ms"] is not None)

        def pct(values, fraction):
            if not values:
                return None
            index = min(len(values) - 1, int(round(fraction * (len(values) - 1))))
            return values[index]

        observed = UNTESTED
        if n:
            if breaker.state == OPEN:
                observed = UNAVAILABLE
            elif successes / n < 0.8 or timeouts:
                observed = DEGRADED
            else:
                observed = QUALIFIED

        return {
            "key": key, "n": n,
            "success_rate": (successes / n) if n else None,
            "timeout_rate": (timeouts / n) if n else None,
            "environmental_failures": environmental,
            "status_counts": statuses,
            # The rate that excludes environmental failures -- the one a
            # quality judgement may legitimately consult.
            "attributable_success_rate": (
                (successes / (n - environmental)) if n - environmental > 0 else None),
            "latency_p50_ms": pct(latencies, 0.5),
            "latency_p95_ms": pct(latencies, 0.95),
            "latency_max_ms": latencies[-1] if latencies else None,
            "circuit": breaker.as_dict(),
            "observed_state": observed,
            "declared_state": self.declared_state(key),
            "usable_now": breaker.allows(),
        }

    def all_health(self) -> dict[str, dict]:
        with self._lock:
            keys = sorted(set(self._breakers) | set(self._states))
        return {key: self.health(key) for key in keys}
