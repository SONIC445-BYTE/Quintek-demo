"""
Reference read-only JSON API over the analytics data layer (benchmark/analytics.py).

This is a REFERENCE implementation of the contract sections 32-40 describe,
built on the standard library only (`http.server`) so it runs with zero new
dependencies and zero assumptions about the real backend's eventual
framework/language. Treat the endpoints and response shapes as the contract
to reimplement in whatever stack the product actually ships on -- not as a
prescription that this stdlib server is meant to run in production.

Endpoints (original, frontend-contract-shaped):

  GET /api/candidates                    -> CandidateSummary[]  (frontend contract)
  GET /api/leaderboard                    -> LeaderboardEntry[]  (rank != eligibility)
  GET /api/ai-overview?candidate=<id>     -> AIOverview
  GET /api/tracks?candidate=<id>          -> TrackResult[]  (student-facing, grouped)
  GET /api/compare?a=<id>&b=<id>          -> ComparisonResult
  GET /api/failures?candidate=&track=&severity=&category=&run_id=
                                          -> FailureAnalyticsResult
  GET /api/routing?task=&candidate=&execution_id=
                                          -> RoutingDecision[]

Endpoints (added for the NVIDIA/model-registry architecture -- same data,
`/ai/*` naming, plus registry- and router-aware views the `/api/*` set
didn't need):

  GET /ai/reliability?candidate=<id>      -> alias of /api/ai-overview
  GET /ai/candidates                      -> alias of /api/candidates
  GET /ai/candidates/<id>                 -> CandidateSummary + tracks + registry entry
  GET /ai/candidates/<id>/tasks            -> task types this candidate is CURRENTLY routed for
  GET /ai/benchmark                       -> registry status counts + calibration state + leaderboard
  GET /ai/leaderboard?task=<TASK_TYPE>     -> overall leaderboard, or task-specific if `task` given
  GET /ai/routing/current                  -> every task type's current routing decision (section 17)
  GET /ai/how-it-works                     -> static description of the selection architecture

Every response is computed server-side from what's already on disk under
`runs/` (and the registry file, for the registry-aware endpoints). Nothing
here performs statistics -- see analytics.py's module docstring for why that
separation is the point.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import analytics as an
from .registry import Registry
from .router import Router, RoutingPolicy
from .eval_api import EvalAPI
from .runs_api import RunsAPI
from .tasks import TaskType, gate_ids_for


HOW_IT_WORKS = {
    "steps": [
        {"step": 1, "name": "Model pool", "description":
         "Several candidate AI configurations are registered -- different base models, "
         "prompts, and settings, each a distinct entry in the Model Registry."},
        {"step": 2, "name": "Benchmark", "description":
         "Every candidate is tested against a fixed, independent set of tasks it has never "
         "seen, across every benchmark track."},
        {"step": 3, "name": "Performance", "description":
         "Results are scored per task, with sample size and uncertainty always attached -- "
         "never a bare number."},
        {"step": 4, "name": "Safety gates", "description":
         "A candidate that fails a mandatory safety gate is excluded from selection "
         "regardless of how high its other scores are."},
        {"step": 5, "name": "Task-specific selection", "description":
         "The eligible candidate with the strongest measured result on a given task is the "
         "one routed to that task -- and only that task. No single model is 'Quintek's AI'."},
    ],
    "principle": "Quintek maintains an evaluated pool of AI configurations, measures them "
                 "against Quintek-specific tasks, filters them through safety gates, and "
                 "routes each task to the strongest eligible configuration.",
}


class AnalyticsAPI:
    """Framework-agnostic core: given a request path + query params, returns
    (status_code, json_body). Kept separate from the HTTP transport so it can
    be unit-tested without opening a socket, and so a real framework adapter
    can wrap this instead of the stdlib handler below."""

    def __init__(self, runs_root: str | Path, *,
                 failures: list[an.FailureRecord] | None = None,
                 routing_log_path: str | Path | None = None,
                 registry_path: str | Path | None = None,
                 gate_registry_path: str | Path | None = None,
                 config_path: str | Path | None = None,
                 root: str | Path | None = None,
                 run_launcher=None,
                 execution_log_path: str | Path | None = None,
                 costs_path: str | Path | None = None):
        self.archive = an.RunArchive(runs_root)
        self._failures = failures or []
        self.routing_log = an.RoutingLog(routing_log_path) if routing_log_path else None
        self.registry = Registry(registry_path) if registry_path else None
        self.router = Router(self.registry, self.archive) if self.registry else None
        # The run-centric routes live in their own module; this class composes
        # them rather than reimplementing run traversal on top of RunArchive,
        # which parses reports into dataclasses and so cannot serve the
        # unmodified report.json the console contract requires.
        self.runs = RunsAPI(runs_root, gate_registry_path=gate_registry_path,
                            config_path=config_path, root=root, run_launcher=run_launcher)
        self.eval = EvalAPI(self.archive, registry=self.registry, router=self.router,
                            execution_log_path=execution_log_path,
                            costs_path=costs_path, failures=self._failures)

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

            # -- /ai/* -----------------------------------------------------

            if path == "/ai/reliability":
                cid = one("candidate")
                if not cid:
                    return 400, {"error": "missing required query param 'candidate'"}
                try:
                    return 200, an.ai_overview(self._latest(cid))
                except KeyError:
                    return 404, {"error": f"no benchmark run found for candidate '{cid}'"}

            if path == "/ai/candidates":
                results = [self.archive.latest_run_for_candidate(c)
                          for c in self.archive.candidates()]
                return 200, {"candidates": [an.candidate_summary(r) for r in results if r]}

            if path == "/ai/how-it-works":
                return 200, HOW_IT_WORKS

            if path == "/ai/benchmark":
                return self._benchmark_summary()

            if path == "/ai/leaderboard":
                task_param = one("task")
                if task_param:
                    try:
                        task = TaskType(task_param)
                    except ValueError:
                        return 400, {"error": f"unknown task type '{task_param}'"}
                    return 200, {"task": task.value,
                                "leaderboard": an.task_leaderboard(self.archive, list(gate_ids_for(task)))}
                board = an.leaderboard(self.archive)
                enriched = []
                for entry in board:
                    d = entry.as_dict()
                    result = self.archive.latest_run_for_candidate(entry.candidate_id)
                    manifest = (result.run.candidate_manifest or {}) if result else {}
                    d["provider"] = manifest.get("provider")
                    d["model"] = manifest.get("model_id")
                    enriched.append(d)
                return 200, {"leaderboard": enriched}

            if path == "/ai/routing/current":
                if self.router is None:
                    return 400, {"error": "no model registry configured for this API instance"}
                current = {}
                for task in TaskType:
                    result = self.router.select(task, policy=RoutingPolicy.QUALITY_FIRST)
                    current[task.value] = {
                        "selected_candidate": result.selected_candidate,
                        "eligible_candidates": result.eligible_candidates,
                        "reason": result.reason,
                    }
                return 200, {"routing_current": current}

            if path.startswith("/ai/candidates/"):
                rest = path[len("/ai/candidates/"):].strip("/")
                parts = rest.split("/") if rest else []
                if len(parts) == 1 and parts[0]:
                    return self._candidate_detail(parts[0])
                if len(parts) == 2 and parts[1] == "tasks":
                    return self._candidate_tasks(parts[0])
                return 404, {"error": f"no such endpoint: {path}"}

            # Published-evaluation routes: the shapes quintek-eval-api.js
            # exports, for the student trust screen and the admin analytics
            # screens.
            if path == "/ai/eval":
                return 200, self.eval.bundle(one("candidate"))
            if path == "/ai/eval/state":
                return 200, {"state": self.eval.state()}
            if path == "/ai/eval/candidates":
                return 200, {"candidates": self.eval.candidates()}
            if path == "/ai/eval/history":
                return 200, {"history": self.eval.history()}
            if path == "/ai/eval/runs":
                return 200, {"runs": self.eval.runs()}
            if path == "/ai/eval/failures":
                return 200, {"failures": self.eval.failures()}
            if path == "/ai/eval/overall-by-candidate":
                return 200, {"overallByCandidate": self.eval.overall_by_candidate()}
            if path in ("/ai/eval/overview", "/ai/eval/tracks", "/ai/eval/track-detail"):
                cid = one("candidate")
                if not cid:
                    return 400, {"error": "missing required query param 'candidate'"}
                fn = {"/ai/eval/overview": self.eval.overview,
                      "/ai/eval/tracks": self.eval.tracks,
                      "/ai/eval/track-detail": self.eval.track_detail}[path]
                value = fn(cid)
                if value is None:
                    return 404, {"error": f"no benchmark run found for candidate '{cid}'"}
                key = {"/ai/eval/overview": "overview", "/ai/eval/tracks": "tracks",
                       "/ai/eval/track-detail": "trackDetail"}[path]
                return 200, {key: value}

            # Run-centric routes (/api/runs, /api/gates, /api/datasets,
            # /api/preflight). Delegated last so the candidate-centric routes
            # above keep precedence on any path both could claim.
            delegated = self.runs.handle_get(path, params)
            if delegated is not None:
                return delegated

            return 404, {"error": f"no such endpoint: {path}"}

        except Exception as exc:  # a broken response must say so, never fabricate data
            return 500, {"error": f"{type(exc).__name__}: {exc}"}

    def handle_post(self, path: str, body: dict) -> tuple[int, dict]:
        try:
            delegated = self.runs.handle_post(path, body)
            if delegated is not None:
                return delegated
            return 404, {"error": f"no such endpoint: POST {path}"}
        except Exception as exc:
            return 500, {"error": f"{type(exc).__name__}: {exc}"}

    def _benchmark_summary(self) -> tuple[int, dict]:
        board = an.leaderboard(self.archive)
        registry_counts: dict[str, int] = {}
        if self.registry is not None:
            for c in self.registry.all():
                registry_counts[c.status] = registry_counts.get(c.status, 0) + 1
        return 200, {
            "candidate_count": len(board),
            "registry_status_counts": registry_counts,
            "leaderboard": [e.as_dict() for e in board],
        }

    def _candidate_detail(self, candidate_id: str) -> tuple[int, dict]:
        try:
            result = self._latest(candidate_id)
        except KeyError:
            return 404, {"error": f"no benchmark run found for candidate '{candidate_id}'"}
        detail = an.candidate_summary(result)
        detail["tracks"] = an.student_track_results(result)
        detail["production_eligible"] = result.production_eligible
        detail["safety_status"] = result.safety_status
        if self.registry is not None:
            entry = self.registry.get(candidate_id)
            detail["registry"] = entry.as_dict() if entry else None
        return 200, detail

    def _candidate_tasks(self, candidate_id: str) -> tuple[int, dict]:
        if self.router is None:
            return 400, {"error": "no model registry configured for this API instance"}
        current_tasks = []
        for task in TaskType:
            result = self.router.select(task, policy=RoutingPolicy.QUALITY_FIRST)
            if result.selected_candidate == candidate_id:
                current_tasks.append(task.value)
        return 200, {"candidate_id": candidate_id, "currently_routed_for": current_tasks}


def make_handler(api: AnalyticsAPI):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, body):
            # A str body is already-rendered text (report.md); everything else
            # is JSON. Content-Type follows the body, so markdown is not
            # delivered wrapped in quotes as a JSON string.
            if isinstance(body, str):
                payload, ctype = body.encode("utf-8"), "text/markdown; charset=utf-8"
            else:
                payload, ctype = json.dumps(body, indent=2).encode("utf-8"), "application/json"
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            # The console is served from a different origin during development.
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "content-type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):  # noqa: N802 (stdlib method name)
            parsed = urlparse(self.path)
            status, body = api.handle(parsed.path, parse_qs(parsed.query))
            self._send(status, body)

        def do_POST(self):  # noqa: N802 (stdlib method name)
            parsed = urlparse(self.path)
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                parsed_body = json.loads(raw) if raw else {}
            except (ValueError, json.JSONDecodeError) as exc:
                self._send(400, {"error": f"malformed JSON body: {exc}"})
                return
            status, body = api.handle_post(parsed.path, parsed_body)
            self._send(status, body)

        def do_OPTIONS(self):  # noqa: N802 (stdlib method name)
            self._send(204, {})

        def log_message(self, fmt, *args):  # quiet by default
            pass

    return Handler


def serve(runs_root: str | Path, *, host: str = "127.0.0.1", port: int = 8420,
          failures: list[an.FailureRecord] | None = None,
          routing_log_path: str | Path | None = None,
          registry_path: str | Path | None = None,
          gate_registry_path: str | Path | None = None,
          config_path: str | Path | None = None,
          root: str | Path | None = None,
          run_launcher=None,
          execution_log_path: str | Path | None = None,
          costs_path: str | Path | None = None) -> None:
    api = AnalyticsAPI(runs_root, failures=failures, routing_log_path=routing_log_path,
                       registry_path=registry_path, gate_registry_path=gate_registry_path,
                       config_path=config_path, root=root, run_launcher=run_launcher,
                       execution_log_path=execution_log_path, costs_path=costs_path)
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
    p.add_argument("--registry", default=None)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8420)
    args = p.parse_args()
    serve(args.runs_root, host=args.host, port=args.port, routing_log_path=args.routing_log,
          registry_path=args.registry)
