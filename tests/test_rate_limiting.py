"""
Rate limiting: the constraint Groq brings that the paid hosts did not.

Fireworks and Together bill per token and will serve as fast as they are
asked, so the ceiling that binds there is spend. Groq's free tier meters
REQUESTS PER MINUTE, which inverts the planning: wall clock binds first and
the budget is slack.

That changes three things, and this file tests all three:

  * a 429 is not a transport failure. It says the host is well and our pace is
    wrong, and recording it as transport would send a reader to look at the
    provider's health instead of at our own rate.
  * a 429 must not be retried immediately. `backoff_base_seconds` defaults to
    0.0, so before this the loop asked again at once -- spending an attempt to
    be refused again, on the one resource that was scarce.
  * the run should not sprint into a wall it could have paced around.

No live calls. Every clock and sleep is injected, so a "26 minute" run takes
no wall clock here.
"""

from __future__ import annotations

import json
import urllib.error
from io import BytesIO
from unittest.mock import patch

import pytest

from benchmark.providers.base import GenerationRequest, RateLimited, RetryPolicy
from benchmark.providers.nvidia import NVIDIAProvider, _retry_after
from benchmark.providers.pacing import RateLimiter, paced
from validator import outage

REQUEST = GenerationRequest(item_id="q1", prompt="ask", max_tokens=64)


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "not-a-real-credential")


def _provider(**kw):
    kw.setdefault("api_key_env", "GROQ_API_KEY")
    kw.setdefault("base_url", "https://api.groq.com/openai/v1/chat/completions")
    return NVIDIAProvider("a-model-id", **kw)


def _429(retry_after=None, body=b'{"error":"rate limit"}'):
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}

    class _H(dict):
        def get(self, k, default=None):
            return dict.get(self, k, dict.get(self, k.title(), default))

    return urllib.error.HTTPError(
        url="https://api.groq.com/openai/v1/chat/completions", code=429,
        msg="Too Many Requests", hdrs=_H(headers), fp=BytesIO(body))


def _ok():
    class _R:
        def read(self):
            return json.dumps({
                "id": "c1",
                "choices": [{"message": {"role": "assistant", "content": '{"ok": true}'},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            }).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    return _R()


# ---------------------------------------------------------------------------
# A 429 is its own thing
# ---------------------------------------------------------------------------

def test_a_429_raises_rate_limited_not_a_generic_transport_error():
    provider = _provider(max_retries=0)
    with patch("urllib.request.urlopen", side_effect=_429(retry_after=7)):
        response = provider.generate(REQUEST)
    assert response.ok is False
    assert "RateLimited" in response.error
    assert "429" in response.error


def test_the_outage_record_calls_it_rate_limited_and_not_transport():
    """
    `validator/outage.py` distinguishes modes, and this must land in the right
    one: a self-inflicted pace problem and an unreliable host have opposite
    fixes.
    """
    provider = _provider(max_retries=0)
    with patch("urllib.request.urlopen", side_effect=_429()):
        response = provider.generate(REQUEST)

    assert outage.was_rate_limited(response) is True
    assert outage.MODE_RATE_LIMITED in outage.MODES
    assert outage.MODE_RATE_LIMITED in outage.MODEL_WAS_CALLED, (
        "a refused request still reached the host")
    assert outage.MODE_RATE_LIMITED in outage.SELF_INFLICTED


def test_a_real_transport_failure_is_still_transport():
    import socket

    provider = _provider(max_retries=0)
    with patch("urllib.request.urlopen", side_effect=socket.timeout("timed out")):
        response = provider.generate(REQUEST)
    assert outage.was_rate_limited(response) is False


# ---------------------------------------------------------------------------
# Retry-After is honoured; backoff when it is absent
# ---------------------------------------------------------------------------

def test_retry_after_is_parsed_only_in_its_numeric_form():
    class _H(dict):
        def get(self, k, default=None):
            return dict.get(self, k, default)

    assert _retry_after(_H({"Retry-After": "13"})) == 13.0
    # An HTTP-date needs the server's clock to agree with ours; a skewed clock
    # would produce a busy-wait or a sleep of hours. None falls back to backoff.
    assert _retry_after(_H({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})) is None
    assert _retry_after(_H({})) is None
    assert _retry_after(None) is None


def test_the_hosts_own_wait_is_honoured_over_our_guess():
    provider = _provider()
    slept = []
    with patch("time.sleep", side_effect=slept.append), \
         patch("urllib.request.urlopen", side_effect=[_429(retry_after=13), _ok()]):
        response = provider.generate(REQUEST)

    assert response.ok is True
    assert slept == [13.0], "a server that names a wait knows something we do not"
    assert response.attempts == 2


def test_without_a_retry_after_the_wait_backs_off_exponentially():
    provider = _provider(max_retries=2)
    slept = []
    with patch("time.sleep", side_effect=slept.append), \
         patch("urllib.request.urlopen", side_effect=[_429(), _429(), _ok()]):
        response = provider.generate(REQUEST)

    assert response.ok is True
    assert slept == [2.0, 4.0], "an immediate retry spends an attempt to be refused again"


def test_a_429_is_never_retried_at_once():
    """The defect this closes: backoff_base_seconds is 0.0 by default."""
    provider = _provider(max_retries=1)
    assert provider.retry_policy.backoff_base_seconds == 0.0, (
        "transport backoff stays 0.0 so the paid hosts are unaffected")
    slept = []
    with patch("time.sleep", side_effect=slept.append), \
         patch("urllib.request.urlopen", side_effect=[_429(), _ok()]):
        provider.generate(REQUEST)
    assert slept and slept[0] > 0, "a rate limit must not be retried with zero delay"


def test_an_absurd_retry_after_is_capped_rather_than_slept_through():
    """A host asking for an hour has ended the run; a process that sleeps
    through it looks hung rather than failed."""
    provider = _provider(max_retries=1)
    slept = []
    with patch("time.sleep", side_effect=slept.append), \
         patch("urllib.request.urlopen", side_effect=[_429(retry_after=3600), _ok()]):
        provider.generate(REQUEST)
    assert slept == [60.0]


def test_sustained_rate_limiting_ends_as_an_outage_not_a_hang():
    provider = _provider(max_retries=2)
    with patch("time.sleep"), patch("urllib.request.urlopen", side_effect=_429()):
        response = provider.generate(REQUEST)
    assert response.ok is False
    assert response.attempts == 3
    assert outage.was_rate_limited(response) is True


# ---------------------------------------------------------------------------
# Pacing
# ---------------------------------------------------------------------------

class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_the_limiter_spaces_attempts_under_the_documented_limit():
    clock = _Clock()
    limiter = RateLimiter(30, clock=clock, sleep=clock.sleep)
    assert limiter.effective_rpm == 24.0, "a margin under the limit, not at it"
    assert limiter.min_interval_seconds == pytest.approx(2.5)

    for _ in range(10):
        limiter.acquire()
    assert clock.now == pytest.approx(22.5), "nine gaps after the first attempt"


def test_a_burst_is_not_allowed_through():
    """
    A token bucket would let the first 30 attempts go out in four seconds and
    then sit out the rest of the minute. A per-minute quota punishes exactly
    that, so the interval is flat.
    """
    clock = _Clock()
    limiter = RateLimiter(30, clock=clock, sleep=clock.sleep)
    stamps = []
    for _ in range(5):
        limiter.acquire()
        stamps.append(clock.now)
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert all(g == pytest.approx(2.5) for g in gaps)


def test_pacing_meters_outbound_attempts_including_retries():
    """
    At `_call`, not `generate` -- a retry is another request the quota counts,
    and pacing that only saw logical calls would exceed the limit by the retry
    factor.
    """
    clock = _Clock()
    limiter = RateLimiter(30, clock=clock, sleep=clock.sleep)
    provider = paced(_provider(max_retries=2), limiter)

    with patch("time.sleep"), \
         patch("urllib.request.urlopen", side_effect=[_429(), _429(), _ok()]):
        response = provider.generate(REQUEST)

    assert response.ok is True and response.attempts == 3
    assert limiter.waits == 2, "three attempts, two of them waiting on the pace"


def test_a_provider_that_hides_its_requests_cannot_be_paced():
    class _Opaque:
        pass

    with pytest.raises(ValueError, match="_call"):
        paced(_Opaque(), RateLimiter(30))


# ---------------------------------------------------------------------------
# The exit conditions
# ---------------------------------------------------------------------------

def test_a_780_attempt_run_completes_under_a_mocked_30_rpm_limiter():
    """
    Phase 2's first exit condition. A host that enforces 30 RPM strictly, and a
    paced client: the run must finish without being throttled into failure.
    """
    clock = _Clock()
    limiter = RateLimiter(30, clock=clock, sleep=clock.sleep)
    provider = paced(_provider(max_retries=2), limiter)

    # A host that refuses anything arriving sooner than 2 seconds apart.
    state = {"last": -999.0, "refusals": 0, "served": 0}

    def host(*_a, **_kw):
        if clock.now - state["last"] < 2.0:
            state["refusals"] += 1
            raise _429(retry_after=2)
        state["last"] = clock.now
        state["served"] += 1
        return _ok()

    with patch("time.sleep", side_effect=clock.sleep), \
         patch("urllib.request.urlopen", side_effect=host):
        for _ in range(780):
            response = provider.generate(REQUEST)
            assert response.ok is True, "the run was throttled into failure"

    assert state["served"] == 780
    assert state["refusals"] == 0, (
        "pacing should keep the steady state under the limit, so backoff never "
        "has to rescue it")
    minutes = clock.now / 60.0
    assert 30 < minutes < 40, f"{minutes:.1f} minutes -- check the forecast"
    assert limiter.forecast_minutes(780) == pytest.approx(minutes, rel=0.01)


def test_a_daily_cap_hit_mid_run_is_incomplete_and_not_a_score():
    """
    Phase 2's second exit condition, and the answer to "is there already a
    mechanism": yes. `validator.budget` counts outbound attempts and raises
    `BudgetExhausted`, which becomes an outage, which makes the arm INCOMPLETE
    with no delta. A daily request ceiling is that ceiling set to whatever the
    day has left. No second counter was added.
    """
    from validator import ablation
    from validator.budget import Budget, BudgetExhausted, meter

    budget = Budget(max_calls=5)            # stands in for "5 requests left today"
    # `meter` returns (provider, boundary): the record has to say which
    # boundary the count was taken at, because a budget number is only
    # interpretable next to the definition it was counted under.
    provider, boundary = meter(_provider(max_retries=0), budget, "candidate")
    assert boundary == "outbound_attempt", (
        "a real adapter must be metered at the request, so retries count")

    served, refused = 0, None
    with patch("urllib.request.urlopen", side_effect=lambda *a, **k: _ok()):
        for _ in range(10):
            response = provider.generate(REQUEST)
            if response.ok:
                served += 1
            else:
                refused = response.error
                break

    assert served == 5, "the ceiling stopped it exactly where it was set"
    assert "budget" in (refused or "").lower()
    assert budget.total == 5, "exhaustion must not consume more budget"

    # And the arm that results is INCOMPLETE, which is not a score.
    arm = ablation.Arm(name="x", layers="ABCD", matrix=None, outages=1,
                       items_expected=100, items_decided=5)
    assert arm.completeness == ablation.INCOMPLETE
    assert arm.comparable is False, "an incomplete arm produces no delta"


def test_the_budget_already_covers_daily_ceilings_so_nothing_was_added():
    """Stated as a test so the absence of a second mechanism is deliberate."""
    import benchmark.providers.pacing as pacing

    source = pacing.__doc__ or ""
    assert "budget" in source.lower(), (
        "the pacing module must say where daily ceilings are handled, or the "
        "next reader adds a second counter that disagrees with the first")
