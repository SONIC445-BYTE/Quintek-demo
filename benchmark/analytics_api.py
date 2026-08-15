"""
Reference read-only JSON API over the analytics data layer (benchmark/analytics.py).

This is a REFERENCE implementation of the contract sections 32-40 describe,
built on the standard library only (`http.server`) so it runs with zero new
dependencies and zero assumptions about the real backend's eventual
framework/language. Treat the endpoints and response shapes as the contract
to reimplement in whatever stack the product actually ships on -- not as a
prescription that this stdlib server is meant to run in production.

Endpoints:

  GET /api/candidates                    -> CandidateSummary[]  (frontend contract)
  GET /api/leaderboard                    -> LeaderboardEntry[]  (rank != eligibility)
  GET /api/ai-overview?candidate=<id>     -> AIOverview
  GET /api/tracks?candidate=<id>          -> TrackResult[]  (student-facing, grouped)
  GET /api/compare?a=<id>&b=<id>          -> ComparisonResult
  GET /api/failures?candidate=&track=&severity=&category=&run_id=
                                          -> FailureAnalyticsResult
  GET /api/routing?task=&candidate=&execution_id=
                                          -> RoutingDecision[]

Every response is computed server-side from what's already on disk under
`runs/`. Nothing here performs statistics -- see analytics.py's module
docstring for why that separation is the point.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import analytics as an


class AnalyticsAPI:
    """Framework-agnostic core: given a request path + query params, returns
    (status_code, json_body). Kept separate from the HTTP transport so it can
    be unit-tested without opening a socket, and so a real framework adapter
    can wrap this instead of the stdlib handler below."""

    def __init__(self, runs_root: str | Path, *,
                 failures: list[an.FailureRecord] | None = None,
                 routing_log_path: str | Path | None = None):
        self.archive = an.RunArchive(runs_root)
        self._failures = failures or []
        self.routing_log = an.RoutingLog(routing_log_path) if routing_log_path else None

    def _latest(self, candidate_id: str):
        result = self.archive.latest_run_for_candidate(candidate_id)
        if result is None:
            raise KeyError(candidate_id)
        return result

    def handle(self, path: str, params: dict[str, list[str]]) -> tuple[int, dict]:
        def one(key, default=None):
            return params.get(key, [default])[0]

        try:
            if path == "/api/candidates":
                results = [self.archive.latest_run_for_candidate(c)
                          for c in self.archive.candidates()]
                return 200, {"candidates": [an.candidate_summary(r) for r in results if r]}

            if path == "/api/leaderboard":
                board = an.leaderboard(self.archive)
                return 200, {"leaderboard": [e.as_dict() for e in board]}

            if path == "/api/ai-overview":
                cid = one("candidate")
                if not cid:
                    return 400, {"error": "missing required query param 'candidate'"}
                try:
                    return 200, an.ai_overview(self._latest(cid))
                except KeyError:
                    return 404, {"error": f"no benchmark run found for candidate '{cid}'"}

            if path == "/api/tracks":
                cid = one("candidate")
                if not cid:
                    return 400, {"error": "missing required query param 'candidate'"}
                try:
                    return 200, {"tracks": an.student_track_results(self._latest(cid))}
                except KeyError:
                    return 404, {"error": f"no benchmark run found for candidate '{cid}'"}

            if path == "/api/compare":
                a, b = one("a"), one("b")
                if not a or not b:
                    return 400, {"error": "required query params 'a' and 'b'"}
                try:
                    return 200, an.compare(self.archive, a, b).as_dict()
                except KeyError as exc:
                    return 404, {"error": str(exc)}

            if path == "/api/failures":
                total_n = one("total_n")
                result = an.failure_analytics(
                    self._failures,
                    total_n=int(total_n) if total_n else None,
                    candidate_id=one("candidate"), track=one("track"),
                    severity=one("severity"), category=one("category"),
                    run_id=one("run_id"),
                )
                return 200, {"count": result.count, "rate": result.rate, "cases": result.cases}

            if path == "/api/routing":
                if self.routing_log is None:
                    return 200, {"routing": []}
                decisions = self.routing_log.query(
                    task=one("task"), selected_candidate=one("candidate"),
                    execution_id=one("execution_id"),
                )
                return 200, {"routing": [d.as_dict() for d in decisions]}

            return 404, {"error": f"no such endpoint: {path}"}

        except Exception as exc:  # a broken response must say so, never fabricate data
            return 500, {"error": f"{type(exc).__name__}: {exc}"}


def make_handler(api: AnalyticsAPI):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (stdlib method name)
            parsed = urlparse(self.path)
            status, body = api.handle(parsed.path, parse_qs(parsed.query))
            payload = json.dumps(body, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt, *args):  # quiet by default
            pass

    return Handler


def serve(runs_root: str | Path, *, host: str = "127.0.0.1", port: int = 8420,
          failures: list[an.FailureRecord] | None = None,
          routing_log_path: str | Path | None = None) -> None:
    api = AnalyticsAPI(runs_root, failures=failures, routing_log_path=routing_log_path)
    server = ThreadingHTTPServer((host, port), make_handler(api))
    print(f"benchmark analytics reference API on http://{host}:{port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Reference analytics API (stdlib http.server)")
    p.add_argument("--runs-root", default="runs")
    p.add_argument("--routing-log", default=None)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8420)
    args = p.parse_args()
    serve(args.runs_root, host=args.host, port=args.port, routing_log_path=args.routing_log)
