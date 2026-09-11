"""
Client-side pacing, so a run does not spend its allowance being refused.

WHY THIS IS NOT JUST BACKOFF
----------------------------
Backoff reacts: it waits after a 429 has already cost an attempt. On a
per-minute quota that is a losing trade, because the attempt that was refused
still counted against nothing useful and the next one is likely to be refused
too. A run that sprints at a 30 RPM limit converts its own budget into 429s.

Pacing acts first. The limiter below spaces outbound attempts so the quota is
approached and not crossed, which costs wall clock and saves attempts -- the
right direction when attempts are the scarce thing and the wall-clock ceiling
is twenty times larger than the run needs.

The two are complements, not alternatives. Pacing keeps the steady state below
the limit; `RateLimited` backoff handles the burst, the shared quota, or the
limit being lower than documented.

WHAT THIS DELIBERATELY IS NOT
-----------------------------
Not a token-bucket with burst credit. A burst is exactly what a per-minute
quota punishes, and "allow a fast start then throttle" is how a run uses its
first thirty attempts in four seconds and then sits out the rest of the minute.
A flat minimum interval is less clever and does not have that failure.

Not a daily-ceiling mechanism either. That is `validator/budget.py`, which
already counts outbound attempts and already turns exhaustion into an
INCOMPLETE arm with no delta. A second counter would be a second thing to
disagree with the first.
"""

from __future__ import annotations

import threading
import time

#: A safety margin under the documented limit. A quota is usually enforced over
#: a sliding window the client cannot see, and clock skew and request duration
#: both push effective rate above nominal, so asking for exactly the limit
#: reliably exceeds it.
DEFAULT_SAFETY_FACTOR = 0.8


class RateLimiter:
    """
    Spaces outbound attempts to at most `requests_per_minute`.

    Thread-safe: the ingestion worker and the request threads share one
    provider, and two callers arriving together must not both decide the
    quota has room.
    """

    def __init__(self, requests_per_minute: float, *,
                 safety_factor: float = DEFAULT_SAFETY_FACTOR, clock=None, sleep=None):
        if requests_per_minute <= 0:
            raise ValueError("requests_per_minute must be positive")
        if not 0 < safety_factor <= 1:
            raise ValueError("safety_factor must be in (0, 1]")
        self.requests_per_minute = requests_per_minute
        self.safety_factor = safety_factor
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._lock = threading.Lock()
        self._next_allowed = 0.0
        self.waited_seconds = 0.0
        self.waits = 0

    @property
    def effective_rpm(self) -> float:
        return self.requests_per_minute * self.safety_factor

    @property
    def min_interval_seconds(self) -> float:
        return 60.0 / self.effective_rpm

    def acquire(self) -> float:
        """Block until the next attempt may go out. Returns seconds waited."""
        with self._lock:
            now = self._clock()
            wait = max(0.0, self._next_allowed - now)
            # Reserved inside the lock so concurrent callers queue rather than
            # all deciding the same slot is free.
            self._next_allowed = max(now, self._next_allowed) + self.min_interval_seconds
        if wait > 0:
            self._sleep(wait)
            self.waited_seconds += wait
            self.waits += 1
        return wait

    def forecast_minutes(self, attempts: int) -> float:
        """Wall clock this many attempts will take at this pace, in minutes."""
        return (attempts * self.min_interval_seconds) / 60.0

    def as_dict(self) -> dict:
        return {"requests_per_minute": self.requests_per_minute,
                "safety_factor": self.safety_factor,
                "effective_rpm": round(self.effective_rpm, 3),
                "min_interval_seconds": round(self.min_interval_seconds, 4),
                "waits": self.waits,
                "waited_seconds": round(self.waited_seconds, 2)}


def paced(provider, limiter: RateLimiter):
    """
    Wrap a provider so every OUTBOUND ATTEMPT waits its turn.

    At `_call`, not `generate`, for the same reason `validator.budget` meters
    there: a retry is another request the quota counts, and pacing that only
    saw logical calls would let a retrying run exceed the limit by the retry
    factor.
    """
    if not hasattr(provider, "_call"):
        raise ValueError(
            f"{type(provider).__name__} does not expose _call, so its outbound "
            "attempts cannot be paced. Test doubles do not need pacing; a real "
            "adapter that hides its requests cannot be metered and must not be "
            "run against a quota.")

    inner = provider._call

    def pacing_call(request, timeout_seconds):
        limiter.acquire()
        return inner(request, timeout_seconds)

    provider._call = pacing_call
    provider.rate_limiter = limiter
    return provider
