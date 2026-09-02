"""
Why a provider call failed, and what that implies about retrying.

"It failed" is not enough information to act on. Three failures that look
identical to a `try/except` demand opposite responses:

    CONNECT 403 (egress policy)  -> will never succeed here. Stop. Try nobody
                                    again until an operator changes something.
    429 (rate limited)           -> will succeed later, and retrying NOW makes
                                    it worse. Back off, keep the provider.
    504 (gateway timeout)        -> might succeed immediately. Retry once.
    410 (model retired)          -> will never succeed again, anywhere, for
                                    anyone. Stop calling it, and record that
                                    it is gone rather than re-probing forever.

Treating all three as "error, retry twice, open the circuit" is how this
project spent an hour hammering an endpoint that was never going to answer,
and how a blocked host ends up recorded as a bad provider.

THE DISTINCTION THIS MODULE ENFORCES
------------------------------------
    ADAPTER WORKS  ≠  PROVIDER REACHABLE  ≠  PROVIDER HEALTHY

An adapter that is correct, tested, and cannot reach its host from this
network has not failed. `EGRESS_BLOCKED` says exactly that, and it is
deliberately not a quality signal: nothing in `benchmark/fitness.py` may read
it as evidence about the model.

A FOURTH DISTINCTION, ADDED FROM AN INCIDENT
--------------------------------------------
    NOT ENTITLED  (404)   !=   WITHDRAWN  (410)

On 2026-08-28 both models frozen into the validator's Phase 0 experiment
answered 410 "has reached its end of life on 2026-08-26". A 404 from the same
host means "not found for this account" and a billing or entitlement change
reverses it; a 410 means the weights are gone. Collapsing them puts a dead
model on a permanent re-probe schedule and keeps it in the routable set.

Classification is from observable evidence -- exception type, HTTP status,
response body -- never from a provider's own claim about itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


class ProviderStatus:
    AVAILABLE = "AVAILABLE"
    DEGRADED = "DEGRADED"
    RATE_LIMITED = "RATE_LIMITED"
    TIMEOUT = "TIMEOUT"
    AUTH_FAILED = "AUTH_FAILED"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    # The model existed and has been withdrawn by the provider. Permanent in a
    # way MODEL_UNAVAILABLE is not: a 404 on NVIDIA means "not entitled for
    # this account", which a billing change can reverse; a 410 means the
    # weights are gone for everybody. Measured 2026-08-28:
    # meta/llama-3.1-8b-instruct and meta/llama-3.1-70b-instruct both answered
    # 410 "has reached its end of life on 2026-08-26". Before this status
    # existed both classified as UNKNOWN_ERROR -- retryable, circuit reopening
    # every 60s, forever.
    MODEL_RETIRED = "MODEL_RETIRED"
    BILLING_BLOCKED = "BILLING_BLOCKED"
    EGRESS_BLOCKED = "EGRESS_BLOCKED"
    # The TCP connection was never established, so the provider never saw the
    # request. This is the one failure class that is not evidence about the
    # provider AT ALL -- it is evidence about the caller's own network.
    #
    # It is separate from TIMEOUT because a timeout is ambiguous: the request
    # may well have arrived and the answer been lost on the way back, so a
    # timeout is conservatively an observation. "Connection refused" is not
    # ambiguous. Nothing was listening; nothing was asked.
    #
    # Measured 2026-09-02: Phase 0 recorded 32 consecutive
    # "[Errno 111] Connection refused" failures at 0.1s each as the session
    # container was torn down, while the endpoint answered HTTP 200 from the
    # next container minutes later. Classified as TIMEOUT they would have
    # entered an arm as 32 model outages -- a fact about the harness recorded
    # as a fact about the model.
    UNREACHED = "UNREACHED"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


ALL_STATUSES = (
    ProviderStatus.AVAILABLE, ProviderStatus.DEGRADED, ProviderStatus.RATE_LIMITED,
    ProviderStatus.TIMEOUT, ProviderStatus.AUTH_FAILED, ProviderStatus.INVALID_RESPONSE,
    ProviderStatus.MODEL_UNAVAILABLE, ProviderStatus.MODEL_RETIRED,
    ProviderStatus.BILLING_BLOCKED, ProviderStatus.EGRESS_BLOCKED,
    ProviderStatus.UNKNOWN_ERROR,
)


@dataclass(frozen=True)
class StatusPolicy:
    """
    What to do about a status.

    `retryable`      -- is another attempt at THIS provider worth making now?
    `max_retries`    -- how many, if so.
    `backoff_seconds`-- wait before retrying; rate limits need real backoff.
    `open_circuit`   -- stop sending work here.
    `circuit_seconds`-- for how long. None means "until an operator intervenes",
                        which is the honest answer for a policy denial: no
                        amount of waiting fixes it.
    `counts_against_quality` -- may this failure influence the model's score?
                        False for everything environmental. A model is not
                        worse because a firewall exists.
    `fallback`       -- should the router immediately try a different provider?
    """

    status: str
    retryable: bool
    max_retries: int
    backoff_seconds: float
    open_circuit: bool
    circuit_seconds: float | None
    counts_against_quality: bool
    fallback: bool
    guidance: str


POLICIES: dict[str, StatusPolicy] = {
    ProviderStatus.AVAILABLE: StatusPolicy(
        ProviderStatus.AVAILABLE, False, 0, 0.0, False, None, False, False,
        "The call succeeded."),

    ProviderStatus.EGRESS_BLOCKED: StatusPolicy(
        ProviderStatus.EGRESS_BLOCKED, False, 0, 0.0, True, None, False, True,
        "This network will not reach the host. Retrying cannot help and the circuit stays "
        "open until an operator changes the egress policy. This says nothing about the "
        "adapter or the model."),

    ProviderStatus.AUTH_FAILED: StatusPolicy(
        ProviderStatus.AUTH_FAILED, False, 0, 0.0, True, None, False, True,
        "The credential was rejected. Retrying with the same key produces the same answer; "
        "a human must supply a working one."),

    ProviderStatus.MODEL_UNAVAILABLE: StatusPolicy(
        ProviderStatus.MODEL_UNAVAILABLE, False, 0, 0.0, True, None, False, True,
        "This provider does not serve that model id. Another attempt cannot conjure it, and "
        "the model may be excellent elsewhere -- this is a routing fact, not a quality one."),

    ProviderStatus.MODEL_RETIRED: StatusPolicy(
        # The one failure no amount of waiting, paying or re-authenticating
        # fixes. circuit_seconds=None so the breaker never re-probes: the
        # model is not coming back, and a re-probe schedule for a retired
        # model is a permanent low-grade waste with no reachable success.
        ProviderStatus.MODEL_RETIRED, False, 0, 0.0, True, None, False, True,
        "The provider has withdrawn this model. It will not return, so retrying and "
        "re-probing are both pointless; route to a different model and record the "
        "retirement. This says nothing about how good the model was."),

    ProviderStatus.BILLING_BLOCKED: StatusPolicy(
        # A billing state, not a model property, and not a credential problem
        # either -- the key is valid, the account cannot pay. Recheckable, but
        # only after a human acts, so the circuit stays open until then.
        ProviderStatus.BILLING_BLOCKED, False, 0, 0.0, True, None, False, True,
        "The account cannot pay for this call. The key is valid and the model is fine; "
        "retrying spends attempts on a state only a human can change."),

    ProviderStatus.RATE_LIMITED: StatusPolicy(
        # Retrying immediately is what caused the limit. Back off, and do not
        # open the circuit -- the provider is healthy, we are being greedy.
        ProviderStatus.RATE_LIMITED, True, 2, 10.0, False, None, False, True,
        "The provider is throttling us. Back off and route elsewhere meanwhile; this is our "
        "request rate, not the provider's health."),

    ProviderStatus.UNREACHED: StatusPolicy(
        # Retryable and emphatically NOT counted against quality: the model
        # was never asked. The circuit does not open, because the thing that
        # is broken is on this side of the socket and closing the circuit on
        # the provider would blame it for our outage.
        ProviderStatus.UNREACHED, True, 2, 1.0, False, None, False, True,
        "The connection was never established, so the provider never saw this request. It "
        "is evidence about this network, not about the model, and it must never be recorded "
        "as an answer the model failed to give."),

    ProviderStatus.TIMEOUT: StatusPolicy(
        ProviderStatus.TIMEOUT, True, 1, 0.0, True, 60.0, False, True,
        "Might succeed immediately, so one retry is worth it -- but repeated timeouts are an "
        "endpoint capacity problem and the circuit should open before the queue drains into "
        "them."),

    ProviderStatus.DEGRADED: StatusPolicy(
        ProviderStatus.DEGRADED, True, 1, 1.0, True, 120.0, False, True,
        "Answering, but not well enough to rely on. Keep it as a fallback, not a first "
        "choice."),

    ProviderStatus.INVALID_RESPONSE: StatusPolicy(
        # The one failure class that IS about the model: it replied, and the
        # reply was unusable.
        ProviderStatus.INVALID_RESPONSE, True, 1, 0.0, False, None, True, False,
        "The provider answered but the reply could not be used. This one does count against "
        "the model: a model that is right but unparseable is unusable."),

    ProviderStatus.UNKNOWN_ERROR: StatusPolicy(
        ProviderStatus.UNKNOWN_ERROR, True, 1, 1.0, True, 60.0, False, True,
        "Unclassified. Retried once and then treated conservatively, because an error nobody "
        "has characterised is not one to keep paying for."),
}


def policy_for(status: str) -> StatusPolicy:
    return POLICIES.get(status, POLICIES[ProviderStatus.UNKNOWN_ERROR])


# Signatures, most specific first. Ordering matters: a proxy's 403 on CONNECT
# and an API's 403 on a request mean completely different things, and the
# tunnel wording is what separates them.
_EGRESS_PATTERNS = (
    r"connect tunnel failed",
    r"tunnel connection failed",
    r"proxy.*403",
    r"403.*connect",
    r"connect_rejected",
    r"policy denial",
    r"egress",
    r"blocked by (?:the )?(?:organization|org|firewall)",
)
# The connection never came up. Deliberately narrow: every one of these means
# the TCP handshake did not complete, so the request cannot have been seen.
# "Connection reset" is NOT here -- a reset can arrive after the request was
# delivered, and the conservative reading of an ambiguous failure is that it
# counts.
_UNREACHED_PATTERNS = (
    r"connection refused", r"\[errno 111\]",
    r"name or service not known", r"temporary failure in name resolution",
    r"nodename nor servname provided", r"\[errno -2\]",
    r"no route to host", r"\[errno 113\]",
    r"network is unreachable", r"\[errno 101\]",
)
# Retirement, checked before the 404 patterns: a withdrawn model and one this
# account cannot see are both "you may not call this", and only one of them
# can ever come back.
_RETIRED_PATTERNS = (r"\b410\b", r"end[ -]of[ -]life", r"\beol\b",
                     r"has been retired", r"\bis retired\b", r"no longer available",
                     r"no longer served", r"\bsunset(?:ted|ting)?\b",
                     r"deprecated and removed", r"410 gone")
# Payment, checked before the rate patterns because a 402 body sometimes
# mentions quota and the two demand different responses -- one needs a card,
# the other needs patience.
_BILLING_PATTERNS = (r"\b402\b", r"payment[ _]required", r"insufficient (?:credit|credits|balance|funds)",
                     r"billing (?:is )?(?:required|blocked)", r"no active subscription")
_AUTH_PATTERNS = (r"\b401\b", r"\b403\b(?!.*connect)", r"unauthorized", r"invalid api key",
                  r"authentication", r"api key not (?:found|valid)", r"forbidden")
_RATE_PATTERNS = (r"\b429\b", r"rate.?limit", r"too many requests", r"quota exceeded")
_TIMEOUT_PATTERNS = (r"timed? ?out", r"\b504\b", r"\b408\b", r"deadline exceeded",
                     r"read timeout")
_MODEL_PATTERNS = (r"\b404\b", r"model.*not found", r"unknown model", r"no such model",
                   r"does not exist")
_INVALID_PATTERNS = (r"could not parse", r"invalid json", r"unparseable",
                     r"no json object", r"malformed")


def _matches(text: str, patterns) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def classify(error: str | BaseException | None = None, *, http_status: int | None = None,
             parsed_ok: bool | None = None, latency_ms: float | None = None,
             slow_threshold_ms: float | None = None) -> str:
    """
    Map observable evidence to a `ProviderStatus`.

    Checked in order of how confidently the evidence identifies the class.
    Egress first, because a CONNECT 403 is otherwise indistinguishable from an
    API 403 and the two demand opposite responses -- one is permanent and
    environmental, the other is a credential problem.
    """
    text = ""
    if isinstance(error, BaseException):
        text = f"{type(error).__name__}: {error}"
    elif error:
        text = str(error)

    if text:
        if _matches(text, _EGRESS_PATTERNS):
            return ProviderStatus.EGRESS_BLOCKED
        # Before TIMEOUT: the NVIDIA adapter wraps every urllib URLError as
        # TimeoutError, so "connection refused" arrives wearing a timeout's
        # class and would otherwise be read as an endpoint that was too slow.
        if _matches(text, _UNREACHED_PATTERNS):
            return ProviderStatus.UNREACHED
        if _matches(text, _RETIRED_PATTERNS) or http_status == 410:
            return ProviderStatus.MODEL_RETIRED
        if _matches(text, _BILLING_PATTERNS) or http_status == 402:
            return ProviderStatus.BILLING_BLOCKED
        if _matches(text, _RATE_PATTERNS) or http_status == 429:
            return ProviderStatus.RATE_LIMITED
        if _matches(text, _TIMEOUT_PATTERNS) or isinstance(error, TimeoutError):
            return ProviderStatus.TIMEOUT
        if _matches(text, _MODEL_PATTERNS) or http_status == 404:
            return ProviderStatus.MODEL_UNAVAILABLE
        if _matches(text, _AUTH_PATTERNS) or http_status in (401, 403):
            return ProviderStatus.AUTH_FAILED
        if _matches(text, _INVALID_PATTERNS):
            return ProviderStatus.INVALID_RESPONSE
        return ProviderStatus.UNKNOWN_ERROR

    if http_status is not None and http_status >= 400:
        return {429: ProviderStatus.RATE_LIMITED, 404: ProviderStatus.MODEL_UNAVAILABLE,
                410: ProviderStatus.MODEL_RETIRED, 402: ProviderStatus.BILLING_BLOCKED,
                401: ProviderStatus.AUTH_FAILED, 403: ProviderStatus.AUTH_FAILED,
                408: ProviderStatus.TIMEOUT, 504: ProviderStatus.TIMEOUT,
                }.get(http_status, ProviderStatus.UNKNOWN_ERROR)

    if parsed_ok is False:
        return ProviderStatus.INVALID_RESPONSE

    # Succeeded, but slowly enough that calling it healthy would be untrue.
    if (slow_threshold_ms is not None and latency_ms is not None
            and latency_ms > slow_threshold_ms):
        return ProviderStatus.DEGRADED

    return ProviderStatus.AVAILABLE


@dataclass
class Verdict:
    """A classification plus what to do about it, ready to act on."""

    status: str
    policy: StatusPolicy
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == ProviderStatus.AVAILABLE

    @property
    def environmental(self) -> bool:
        """
        True when the failure says nothing about the model or the adapter.

        The flag `benchmark/fitness.py` consults to make sure a firewall never
        becomes evidence about a model's quality.
        """
        return not self.policy.counts_against_quality and not self.ok

    def as_dict(self) -> dict:
        return {"status": self.status, "detail": self.detail, "ok": self.ok,
                "environmental": self.environmental,
                "retryable": self.policy.retryable,
                "max_retries": self.policy.max_retries,
                "backoff_seconds": self.policy.backoff_seconds,
                "open_circuit": self.policy.open_circuit,
                "circuit_seconds": self.policy.circuit_seconds,
                "counts_against_quality": self.policy.counts_against_quality,
                "fallback": self.policy.fallback,
                "guidance": self.policy.guidance}


def assess(error=None, **kwargs) -> Verdict:
    status = classify(error, **kwargs)
    detail = (f"{type(error).__name__}: {error}" if isinstance(error, BaseException)
              else (str(error) if error else ""))
    return Verdict(status=status, policy=policy_for(status), detail=detail)
