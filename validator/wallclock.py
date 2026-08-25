"""
A ceiling on how long an experiment run may take, independent of what it costs.

WHY THIS IS A SEPARATE MECHANISM FROM THE CALL BUDGET
-------------------------------------------------------
`validator/budget.py` bounds spend. It says nothing about time, and the
candidate/judge endpoint's own measured variance -- 0.6s for `GET /v1/models`,
72.9s and separately 180.8s for near-identical 8-token completions on the same
model (`benchmark/providers/nvidia.py`) -- means a run that respects every
call-count ceiling can still take hours. "How much money" and "how long" are
different questions, and answering the second does not require touching how
the first is verified or reported.

So this is a second, independent ceiling rather than a field added to `Budget`.
Nothing about `max_calls`, `max_judge_calls`, the WITHIN/WILL_EXCEED/IMPOSSIBLE
verdict, or the outbound-attempt accounting changes because this file exists.

SAME TREATMENT AS BUDGET EXHAUSTION
------------------------------------
`WallClockExceeded` is checked at the same choke point `budget.spend` is
checked -- immediately before a request would be sent -- and, at the boundary
that has no provider-level retry loop of its own, converted to a failed
`GenerationResponse` by exactly the pattern `budget.py` already uses for
`BudgetExhausted`. A caller that already treats one kind of outage as an
INCOMPLETE arm with no delta treats both without change.

An in-flight call that started before the deadline is not interrupted; only
the decision to start the NEXT one checks the clock.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


class WallClockExceeded(RuntimeError):
    """The elapsed-time ceiling was reached before a new call was started."""


@dataclass
class WallClock:
    """
    `max_minutes=None` means no ceiling -- a decision, not a default. The
    caller prints `NO WALL-CLOCK CEILING SET` explicitly rather than letting an
    unset value pass through as silent, unbounded running.
    """
    max_minutes: float | None = None
    started_at: float = field(default_factory=time.monotonic)

    @property
    def elapsed_minutes(self) -> float:
        return (time.monotonic() - self.started_at) / 60.0

    def check(self) -> None:
        if self.max_minutes is None:
            return
        elapsed = self.elapsed_minutes
        if elapsed >= self.max_minutes:
            raise WallClockExceeded(
                f"the wall-clock ceiling of {self.max_minutes:g} minute(s) was reached "
                f"after {elapsed:.1f} minute(s). No new calls are being started; whatever "
                "arm was in progress is incomplete and produces no delta. A call already "
                "in flight when the ceiling was crossed is not interrupted.")

    def as_dict(self) -> dict:
        return {"max_minutes": self.max_minutes,
                "elapsed_minutes": round(self.elapsed_minutes, 2)}
