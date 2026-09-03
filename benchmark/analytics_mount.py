"""
The analytics/console surface, mounted into the learner server.

WHY THIS EXISTS
---------------
The two shipped Android screens read two different globals:
`__QUINTEK_STUDENT_API__` for the learner surface and `__QUINTEK_API__` for the
console. The app has ONE backend setting, so both globals are set to one
origin -- and that origin then has to answer both surfaces or one screen goes
dark. Measured before this module existed: pointed at the analytics server,
every learner route returned 404; pointed at the learner server, the console's
`/api/runs`, `/api/gates`, `/api/preflight` and the richer `/ai/*` routes
returned 404.

This is not a new architecture. `billing/mount.py` already establishes the
pattern -- a service exposes `owns(path)` and `handle(...)`, and the HTTP
transport dispatches to whichever mount claims the path before falling through
to the primary API. `student/api.py` already re-serves `/ai/eval` and
`/ai/benchmark/*` for exactly this reason, documented there as "served from the
learner backend so the app talks to one origin". This module finishes that job
for the routes the learner API does not own.

Both services remain independently runnable: `serve-analytics` is unchanged,
and this mount is opt-in.

WHO OWNS WHAT
-------------
The learner API owns a small, deliberate set of `/ai/*` routes and re-serves
them itself. Ownership is declared explicitly rather than discovered by
falling through on 404, because a fallback would silently reroute a genuine
learner-side 404 into the analytics archive and make a missing notebook look
like a missing benchmark run.

`/ai/eval` is served by BOTH, deliberately and identically -- verified equal
key-for-key. The learner API keeps it, so the phone's transparency screen is
answered by the server holding its session.
"""

from __future__ import annotations

from pathlib import Path

#: Exact `/ai/*` paths `student/api.py::_ai` answers itself.
STUDENT_OWNED_AI_EXACT = frozenset({
    "/ai/eval",
    "/ai/benchmark",
    "/ai/benchmark/categories",
    "/ai/benchmark/ranking",
    "/ai/benchmark/powering",
})

#: `/ai/*` prefixes the learner API answers itself (`/ai/models/<id>` and
#: `/ai/models/<id>/history`).
STUDENT_OWNED_AI_PREFIX = ("/ai/models/",)


class AnalyticsMount:
    """
    Routes the console's endpoints to `AnalyticsAPI`, inside the learner server.

    Shaped like `billing.mount.BillingMount` on purpose: `owns()` then
    `handle()`, so `student/server.py` dispatches to it with the same three
    lines it already uses for billing.
    """

    def __init__(self, api):
        self.api = api

    @classmethod
    def build(cls, *, runs_root: str | Path = "runs", root: Path | None = None,
              registry_path=None, gate_registry_path=None, config_path=None,
              routing_log_path=None, execution_log_path=None, costs_path=None,
              model_registry_path=None) -> "AnalyticsMount | None":
        """
        Assemble the analytics API, or `None` if it cannot be built.

        Returning `None` rather than raising keeps a learner deployment that
        has no benchmark archive working: the console routes then 404, which
        is honest, instead of the whole learner server refusing to start.
        """
        try:
            from .analytics_api import AnalyticsAPI
            return cls(AnalyticsAPI(
                runs_root, routing_log_path=routing_log_path,
                registry_path=registry_path, gate_registry_path=gate_registry_path,
                config_path=config_path, root=root,
                execution_log_path=execution_log_path, costs_path=costs_path,
                model_registry_path=model_registry_path))
        except Exception:
            return None

    @staticmethod
    def owns(path: str) -> bool:
        """
        Whether the console owns this path.

        `/api/*` is entirely the console's. `/ai/*` is the console's EXCEPT the
        routes the learner API answers itself.
        """
        if path == "/api" or path.startswith("/api/"):
            return True
        if path == "/ai" or path.startswith("/ai/"):
            if path in STUDENT_OWNED_AI_EXACT:
                return False
            return not any(path.startswith(p) for p in STUDENT_OWNED_AI_PREFIX)
        return False

    def handle(self, method: str, path: str, params: dict) -> tuple[int, object]:
        """
        `params` arrives as `parse_qs` output -- `dict[str, list[str]]` -- which
        is what `AnalyticsAPI.handle` expects. The learner API's own dispatcher
        flattens to `dict[str, str]`, so the transport must NOT hand this the
        flattened form.
        """
        if method != "GET":
            # Every console route here is read-only. Promotion, the one
            # analytics surface that writes, is deliberately not mounted: it
            # changes what the product serves and belongs on an operator's
            # own server, not on the origin a phone is pointed at.
            return 405, {"error": "the benchmark console is read-only over this mount"}
        return self.api.handle(path, params)
