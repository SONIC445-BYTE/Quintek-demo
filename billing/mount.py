"""
Putting the billing API on the wire.

`billing/api.py` decides what happens; this decides how a request reaches it.
Keeping the two apart is what lets the whole billing engine be tested without
a socket, and it is why porting off `http.server` later is an adapter change.

Two things here are not incidental:

  The webhook needs the RAW request body. Razorpay signs the exact bytes it
  sent, so re-serialising the parsed JSON produces a different string and a
  failed signature -- which historically is the moment somebody disables the
  check "temporarily".

  Identity comes from the learner session, not from the request. `user_id`
  and `is_admin` are resolved from the bearer token against the student
  database; a body that claims a user id is ignored. Otherwise anybody could
  read anybody's usage by typing their id.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from .api import BillingAPI
from .db import connect
from .plans import PlanStore

# Everything billing serves lives under this prefix, so that `/me` (the
# learner's profile, served by the student API) and `/me/usage` (billing) can
# never collide by accident. A route collision in a payment surface is not a
# routing bug, it is a billing bug.
PREFIX = "/billing"

SIGNATURE_HEADERS = ("X-Razorpay-Signature", "X-Signature")


class BillingMount:
    """
    Owns a billing database and serves it over HTTP.

    Connections are THREAD-LOCAL. `ThreadingHTTPServer` hands each request to a
    different thread, and sqlite refuses a connection used outside the thread
    that made it -- which does not show up in any test that calls `handle`
    directly, only in one that opens a socket. `student/db.py` learned this
    already; the same rule applies here, and the consequence of getting it
    wrong on a payment surface is a dropped connection mid-checkout.
    """

    def __init__(self, db_path: str | Path = "billing.db", *, gateway=None,
                 seed: str | Path | None = "configs/plans.json"):
        self.db_path = str(db_path)
        self.gateway = gateway
        self._local = threading.local()
        conn = self._conn()
        if seed and not conn.execute("SELECT 1 FROM plans LIMIT 1").fetchone():
            PlanStore(conn).seed_from_config(seed)

    def _conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = connect(self.db_path)
            self._local.conn = conn
        return conn

    def release(self) -> None:
        """
        Return this thread's connection, and drop the API bound to it.

        On Postgres the connection goes back to a bounded pool, so a threaded
        server does not open one backend per request. The cached `BillingAPI`
        has to go with it: it holds the connection, and reusing it on the next
        request would reach through a returned handle.

        A no-op on SQLite, where the connection is a cheap file handle worth
        keeping for the life of the thread.
        """
        conn = getattr(self._local, "conn", None)
        if conn is None or not getattr(conn, "is_postgres", False):
            return
        conn.close()
        self._local.conn = None
        self._local.api = None

    def _api(self) -> BillingAPI:
        api = getattr(self._local, "api", None)
        if api is None:
            api = BillingAPI(self._conn(), gateway=self.gateway)
            self._local.api = api
        return api

    @property
    def plans(self) -> PlanStore:
        # A property, not an attribute: an attribute built in __init__ would
        # carry the constructing thread's connection into every request.
        return PlanStore(self._conn())

    @classmethod
    def from_env(cls, db_path: str | Path = "billing.db", *, env=None,
                 seed: str | Path | None = "configs/plans.json") -> "BillingMount":
        from .gateway_http import adapter_from_env
        return cls(db_path, gateway=adapter_from_env(env), seed=seed)

    # ---------- routing ----------

    @staticmethod
    def owns(path: str) -> bool:
        return path == PREFIX or path.startswith(PREFIX + "/")

    @staticmethod
    def strip(path: str) -> str:
        return path[len(PREFIX):] or "/"

    @staticmethod
    def signature_from(headers) -> str:
        for name in SIGNATURE_HEADERS:
            value = headers.get(name)
            if value:
                return value.strip()
        return ""

    def handle(self, method: str, path: str, params: dict, raw: bytes, headers,
               user_row) -> tuple[int, object]:
        """
        `user_row` is the student database's row for the bearer token, or None.

        A malformed JSON body is a 400 here rather than an exception, with one
        exception of its own: the webhook route is reached with raw bytes and
        must not be rejected for being unparseable, because deciding whether
        an unparseable payload is genuine is the signature check's job.
        """
        inner = self.strip(path)
        body: dict = {}
        if raw:
            try:
                body = json.loads(raw)
            except (ValueError, json.JSONDecodeError):
                if not inner.startswith("/webhooks/"):
                    return 400, {"error": "malformed JSON body"}
                body = {}
        if not isinstance(body, dict):
            body = {}

        user_id = user_row["id"] if user_row is not None else None
        is_admin = bool(user_row is not None
                        and (user_row["role"] or "").lower() == "admin")

        return self._api().handle(method, inner, params, body, user_id=user_id,
                               is_admin=is_admin, raw_body=raw,
                               signature=self.signature_from(headers))
