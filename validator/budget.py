"""
A hard ceiling on inference spend, counted where the money actually leaves.

WHERE THE COUNTER SITS, AND WHY IT MATTERS
------------------------------------------
`BaseProvider.generate` owns a retry loop around `self._call`, and `_call` is
where the HTTP request is made. A budget placed around `generate` would count
one logical question and permit three requests, so a run forecast at 765 calls
could quietly spend 2295. The counter therefore sits at `_call` for any
provider that implements it, which means retries are counted, and the run
record says which boundary was used rather than asserting that it was the right
one.

A provider that overrides `generate` instead -- the replay and oracle doubles
-- has no `_call` to meter, so it is counted at `generate` and recorded as
`logical_call`. That distinction is in the record because a budget number is
only interpretable next to the definition it was counted under.

WHAT EXHAUSTION MEANS
---------------------
Nothing new. `spend` raises, the provider's retry loop records an error, the
layer raises its Unavailable, `pipeline.run` propagates it, the runner counts
an outage, and the arm is INCOMPLETE with no delta. A deliberately stopped
experiment is still an incomplete experiment, and inventing a "budget exceeded,
partial score" outcome would be inventing a way to read a stopped run as a
result.

Exhaustion does NOT consume budget: once over the ceiling, `spend` raises
without incrementing, so the spent figure in the record is the number of
requests that actually went out.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from benchmark.providers.base import BaseProvider, GenerationResponse

BOUNDARY_OUTBOUND = "outbound_attempt"
BOUNDARY_LOGICAL = "logical_call"

SEAT_CANDIDATE = "candidate"
SEAT_JUDGE = "judge"


class BudgetExhausted(RuntimeError):
    """The ceiling was reached. Deliberately an ordinary exception."""


@dataclass
class Budget:
    """
    A ceiling in outbound attempts, not in logical calls.

    `None` means no ceiling, which is a decision rather than a default: the
    runner prints the forecast against the configured budget before spending
    anything, and "no budget set" is printed as such.
    """
    max_calls: int | None = None
    max_judge_calls: int | None = None
    spent: Counter = field(default_factory=Counter)
    boundaries: dict = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.spent.values())

    def spend(self, seat: str) -> None:
        if self.max_calls is not None and self.total >= self.max_calls:
            raise BudgetExhausted(
                f"the total call budget of {self.max_calls} outbound attempts is spent. "
                "The remaining items were not measured, so this arm is incomplete and "
                "produces no delta.")
        if (seat == SEAT_JUDGE and self.max_judge_calls is not None
                and self.spent[SEAT_JUDGE] >= self.max_judge_calls):
            raise BudgetExhausted(
                f"the judge call budget of {self.max_judge_calls} outbound attempts is "
                "spent. The remaining items were not judged, so this arm is incomplete "
                "and produces no delta.")
        self.spent[seat] += 1

    def as_dict(self) -> dict:
        return {"max_calls": self.max_calls, "max_judge_calls": self.max_judge_calls,
                "spent_total": self.total, "spent_by_seat": dict(self.spent),
                "boundaries": dict(self.boundaries),
                "unit": "outbound attempts where the provider exposes one, otherwise "
                        "logical calls; see boundaries"}


def meter(provider, budget: Budget, seat: str):
    """
    Count every request this provider makes against `budget`.

    Idempotent per instance: the candidate occupies two layers with one object,
    and wrapping it twice would count each of its requests twice.
    """
    if getattr(provider, "_quintek_metered", False):
        return provider, budget.boundaries.get(str(getattr(provider, "model", "")), "")

    if type(provider)._call is not BaseProvider._call:
        boundary = BOUNDARY_OUTBOUND
        original = provider._call

        def counted(request, timeout_seconds):
            budget.spend(seat)
            return original(request, timeout_seconds)

        provider._call = counted
    else:
        boundary = BOUNDARY_LOGICAL
        original_generate = provider.generate

        def counted_generate(request):
            # Converted to a failed response rather than raised, so exhaustion
            # reaches callers by exactly the same route at both boundaries. At
            # the outbound boundary the provider's own retry loop performs this
            # conversion; here there is no retry loop to do it, and a caller
            # that saw a bare BudgetExhausted from one provider and a failed
            # response from another would need two code paths for one event.
            try:
                budget.spend(seat)
            except BudgetExhausted as exc:
                return GenerationResponse(
                    item_id=request.item_id, raw_output="", parsed=None,
                    provider=getattr(provider, "name", "unknown"),
                    model=getattr(provider, "model", "unknown"),
                    model_version=getattr(provider, "model_version", "unknown"),
                    latency_ms=0.0, error=f"BudgetExhausted: {exc}")
            return original_generate(request)

        provider.generate = counted_generate

    provider._quintek_metered = True
    budget.boundaries[str(getattr(provider, "model", "unknown"))] = boundary
    return provider, boundary
