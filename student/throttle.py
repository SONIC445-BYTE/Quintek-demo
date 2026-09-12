"""
A ceiling on requests, at the HTTP layer, where an abusive caller actually is.

WHY NOT REUSE `benchmark/providers/pacing.py`
----------------------------------------------
That limiter PACES US -- it makes our own outbound calls wait so a provider
does not refuse them, and it blocks the caller to do it. This one REFUSES
somebody else, and blocking is exactly the wrong response: a thread held open
sleeping is the resource an abusive caller is trying to consume. Same word,
opposite job, so a separate module rather than a flag on that one.

THE KEY IS THE TOKEN WHERE THERE IS ONE
-----------------------------------------
Keying only on address punishes everybody behind one NAT -- a hospital, a
university residence, a mobile carrier -- for one caller's behaviour, and that
is most of the intended audience. So an authenticated request is limited per
ACCOUNT, and only an unauthenticated one falls back to the address, where
there is nothing better.

The token is hashed before it becomes a key, so the in-memory table cannot
hand out live credentials if it is ever dumped or logged.

A FIXED WINDOW, NOT A SLIDING ONE
-----------------------------------
A fixed window allows a burst across a boundary -- up to twice the limit in
one instant, if a caller times it. That is a known and accepted cost. The
alternative keeps a timestamp per request, which is per-caller unbounded
memory, and an in-process limiter that can be made to consume memory by
sending requests is a new denial of service rather than a fix for one.

NOT A SUBSTITUTE FOR ONE AT THE EDGE
--------------------------------------
This is per process. Two workers mean two limiters and twice the ceiling, and
nothing here survives a restart. A real deployment wants this at the reverse
proxy. What this gives is a floor that is present by default, rather than
nothing at all until somebody configures a proxy.
"""

from __future__ import annotations

import hashlib
import threading
import time

#: Requests per window per caller. Chosen against what the app does: a
#: revision session is one call per question answered, and a human answering
#: postgraduate questions does not exceed one a second for a minute.
DEFAULT_LIMIT = 120

#: Seconds.
DEFAULT_WINDOW = 60.0

#: Paths where the limit is far tighter, because guessing is the attack.
#: Unauthenticated and cheap to call, which is the combination worth bounding.
SENSITIVE = {"/auth/login": 10, "/auth/register": 5}


class RateLimiter:
    """Fixed-window counter, keyed per caller. Thread-safe."""

    def __init__(self, *, limit: int = DEFAULT_LIMIT, window: float = DEFAULT_WINDOW,
                 clock=None):
        self.limit = limit
        self.window = window
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._windows: dict[tuple[str, str], tuple[float, int]] = {}

    def key_for(self, *, token: str | None, address: str, path: str) -> tuple[str, str]:
        """
        Who is being counted, and under which bucket.

        Hashed, never the token itself: this table lives in memory for the life
        of the process and may end up in a heap dump or a traceback.
        """
        if token:
            who = "t:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]
        else:
            who = "a:" + (address or "unknown")
        return who, path if path in SENSITIVE else "*"

    def check(self, *, token: str | None, address: str, path: str) -> dict:
        """
        `{"allowed": bool, "remaining": int, "retry_after": float}`.

        Never blocks. Holding a thread asleep is the resource an abusive caller
        wants; the answer is to refuse quickly and let the socket close.
        """
        limit = SENSITIVE.get(path, self.limit)
        key = self.key_for(token=token, address=address, path=path)
        now = self._clock()
        with self._lock:
            started, count = self._windows.get(key, (now, 0))
            if now - started >= self.window:
                started, count = now, 0
            count += 1
            self._windows[key] = (started, count)
        remaining = max(0, limit - count)
        return {"allowed": count <= limit, "limit": limit, "remaining": remaining,
                "retry_after": max(0.0, round(self.window - (now - started), 2))}

    def forget(self, *, older_than: float | None = None) -> int:
        """
        Drop windows that have expired.

        Called opportunistically rather than on a timer: without it the table
        grows with every distinct caller forever, which is the memory leak
        that turns a rate limiter into the outage it was meant to prevent.
        """
        cutoff = self._clock() - (older_than or self.window * 2)
        with self._lock:
            stale = [k for k, (started, _n) in self._windows.items() if started < cutoff]
            for key in stale:
                del self._windows[key]
        return len(stale)

    def __len__(self) -> int:
        return len(self._windows)
