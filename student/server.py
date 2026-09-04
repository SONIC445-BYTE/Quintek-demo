"""
HTTP transport for the learner API.

Stdlib `http.server`, matching `benchmark/analytics_api.py`: the point is a
reference implementation with no install step, not a production server. The
routing and behaviour live in `student/api.py`, so porting to FastAPI or
anything else means writing a new adapter, not rewriting the product.

CORS is permissive because the design files are opened from `file://` or a
different port during development. Narrow it before this is exposed anywhere.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .api import StudentAPI
from .db import Database
from .uploads import MAX_BYTES as MAX_UPLOAD_BYTES
from .uploads import MAX_ENCODED_BYTES

# The transport's ceiling, derived from the upload limit rather than picked
# separately -- two numbers chosen independently drift, and the one that
# drifts low rejects uploads the API would have accepted.
MAX_REQUEST_BYTES = MAX_ENCODED_BYTES + 64 * 1024

#: Which origins may call this server from a browser.
#:
#: Defaults to `*`, which is what the Android WebView needs: it loads its
#: bundle from `file:///android_asset/`, so its Origin is the literal string
#: "null" and no specific origin can be allowlisted for it. The app sends a
#: bearer token in a header rather than cookies, so `*` does not expose an
#: ambient-credential path.
#:
#: Set QUINTEK_CORS_ORIGIN to a single origin when the console is served to a
#: real browser origin and the WebView is not a client of that deployment.
#: This is the narrowing the module docstring above asks for, made a
#: deployment decision rather than a code edit.
CORS_ORIGIN = os.environ.get("QUINTEK_CORS_ORIGIN", "*")


def build_billing(db_path: str | Path | None = None, *, env=None):
    """
    Attach the billing surface, or `None` if it cannot be built.

    Billing runs on its own database and its own connection: usage and money
    are the records that must survive a bad deployment of the learning engine,
    and nothing about a notebook belongs in the same file as a subscription.
    """
    from billing.mount import BillingMount
    path = db_path or os.environ.get("QUINTEK_BILLING_DB", "billing.db")
    return BillingMount.from_env(path, env=env)


def build_console(**kwargs):
    """
    Attach the benchmark console's read-only surface, or `None`.

    The Android app has one backend setting and its two screens read two
    different globals, so a single origin has to answer both. `None` here
    means the console routes 404 -- honest, and the learner half still works.

    OFF BY DEFAULT. These are operator routes: `/api/runs` serves full run
    reports and `/ai/discovery` the model registry. Serving them from the
    origin a learner's phone is pointed at is a deliberate operator decision,
    so it is opt-in rather than something a default quietly widens.
    """
    from benchmark.analytics_mount import AnalyticsMount
    return AnalyticsMount.build(**kwargs)


def build_cost_sink(billing_mount):
    """
    The join between the two financial systems, or `None`.

    Customers pay Quintek through the gateway; Quintek separately funds
    provider accounts. Nothing links the two on its own, and without this hook
    `cost_ledger` stays empty -- every economics figure reads "unmeasured"
    while real money is being spent on inference.
    """
    if billing_mount is None:
        return None, None
    from billing.recorder import CostRecorder

    # The FACTORY, not a connection: the server is threaded, and a connection
    # made here cannot be used on a request thread. `_conn` is thread-local.
    recorder = CostRecorder(billing_mount._conn)
    return recorder, recorder


def build_api(db_path: str | Path | None = None, *, with_ai: bool = True,
              cost_sink=None) -> StudentAPI:
    """
    Assemble the engine.

    AI-backed services are attached only when a provider can actually be built.
    A server whose ingestion worker silently does nothing is worse than one
    that says the source failed and why -- so `StudentAPI` reports 503 rather
    than accepting work it cannot do.
    """
    db = Database(db_path)
    if not with_ai:
        return StudentAPI(db)

    from .ai import AIEngine
    from .generation import AIConceptExtractor, QuestionGenerator
    from .ingestion import IngestionEngine
    from .notifications import NotificationService
    from .validation import QuestionValidator

    registry, archive = None, None
    registry_path = Path("configs/model_registry.json")
    if registry_path.exists():
        from benchmark import analytics as an
        from benchmark.registry import Registry
        registry, archive = Registry(registry_path), an.RunArchive("runs")

    from benchmark.providers.registry import build_provider, spec_from_env

    base_spec = spec_from_env()

    def provider_factory(candidate):
        """
        Resolve through the provider registry rather than naming a vendor here.

        The registry entry's model_id wins when there is one -- a candidate is
        a specific model, and serving a different one under its id would
        misattribute every result. Otherwise the environment's spec applies.
        A spec that cannot be built raises; it never degrades to the scripted
        provider, because a fabricated answer must never be indistinguishable
        from a real one.
        """
        spec = dict(base_spec)
        model_id = getattr(candidate, "model_id", None)
        if model_id:
            spec["model_id"] = model_id
            spec["model_version"] = getattr(candidate, "model_version", "unknown")
        return build_provider(spec)

    ai = AIEngine(db, registry=registry, archive=archive,
                  provider_factory=provider_factory, cost_sink=cost_sink)
    validator_ai = AIEngine(
        db, registry=registry, archive=archive, provider_factory=provider_factory,
        # Validation spend is the other half of cost-per-ACCEPTED. Costing
        # generation alone would understate it by however much validation
        # costs, which on a cheap generator is most of the bill.
        cost_sink=cost_sink,
        # A different configuration for validation, so independence holds even
        # in a development deployment. Same rule as the benchmark's judge tiers.
        development_candidate=os.environ.get("QUINTEK_DEV_VALIDATOR_CANDIDATE"))

    engine = IngestionEngine(db, concept_extractor=AIConceptExtractor(db, ai))
    return StudentAPI(db, engine=engine, ai=ai,
                      generator=QuestionGenerator(db, ai),
                      validator=QuestionValidator(db, validator_ai),
                      notifier=NotificationService(db))


def make_handler(api: StudentAPI, billing=None, analytics=None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Quintek/0.4"

        def _token(self) -> str | None:
            auth = self.headers.get("Authorization") or ""
            return auth[7:].strip() if auth.lower().startswith("bearer ") else None

        def _send(self, status: int, body) -> None:
            payload = json.dumps(body, indent=2, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Access-Control-Allow-Origin", CORS_ORIGIN)
            self.send_header("Access-Control-Allow-Headers", "content-type, authorization")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
            self.end_headers()
            self.wfile.write(payload)

        def _dispatch(self, method: str) -> None:
            """
            Serve one request, and give the database connection back afterwards.

            The `finally` is not defensive tidying; on Postgres it is what
            keeps the service alive. `ThreadingHTTPServer` starts a NEW THREAD
            per request and the connection caches are thread-local, so every
            request checks a connection out of a bounded pool. Without the
            return, the ninth concurrent request against a pool of eight
            blocks until it times out, and the service stops answering while
            looking perfectly healthy.

            On SQLite `release()` is a no-op: reopening a file per request is
            pure cost, and the handle is cheap to keep.
            """
            try:
                self._serve(method)
            finally:
                api.db.release()
                if billing is not None:
                    billing.release()

        def _serve(self, method: str) -> None:
            parsed = urlparse(self.path)
            params = {k: v[0] for k, v in parse_qs(parsed.query).items()}

            # Read the raw bytes ONCE, before anything parses them. The
            # webhook signature is over exactly these bytes, and a body that
            # has been round-tripped through json is a different string.
            raw = b""
            if method in {"POST", "PUT"}:
                length = int(self.headers.get("Content-Length") or 0)
                # Checked BEFORE reading. The body is read into memory whole,
                # so a request declaring a four-gigabyte upload is the whole
                # server -- and file uploads made that reachable from outside.
                if length > MAX_REQUEST_BYTES:
                    self._send(413, {
                        "error": f"request body is {length} bytes; the limit is "
                                 f"{MAX_REQUEST_BYTES}. A file larger than "
                                 f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB has to be "
                                 "split rather than sent whole.",
                    })
                    return
                raw = self.rfile.read(length) if length else b""

            if analytics is not None and analytics.owns(parsed.path):
                # The console's read-only surface, so one origin answers both
                # Android screens. Dispatched from the raw parse_qs result:
                # AnalyticsAPI expects dict[str, list[str]], not the flattened
                # form the learner API takes.
                raw_params = parse_qs(parsed.query)
                status, payload = analytics.handle(method, parsed.path, raw_params)
                self._send(status, payload)
                return

            if billing is not None and billing.owns(parsed.path):
                # Identity comes from the session, never from the request body.
                user = api.db.user_for_token(self._token())
                status, payload = billing.handle(method, parsed.path, params, raw,
                                                 self.headers, user)
                self._send(status, payload)
                return

            body = {}
            if raw:
                try:
                    body = json.loads(raw)
                except (ValueError, json.JSONDecodeError) as exc:
                    self._send(400, {"error": f"malformed JSON body: {exc}"})
                    return
            status, payload = api.handle(method, parsed.path, params, body, self._token())
            self._send(status, payload)

        def do_GET(self):      # noqa: N802
            self._dispatch("GET")

        def do_POST(self):     # noqa: N802
            self._dispatch("POST")

        def do_PUT(self):      # noqa: N802
            self._dispatch("PUT")

        def do_OPTIONS(self):  # noqa: N802
            self._send(204, {})

        def log_message(self, fmt, *args):
            pass

    return Handler


def serve(*, host: str = "127.0.0.1", port: int = 8500,
          db_path: str | Path | None = None, with_ai: bool = True,
          with_billing: bool = True, billing_db: str | Path | None = None,
          with_console: bool = False, console_kwargs: dict | None = None) -> None:
    billing = build_billing(billing_db) if with_billing else None
    recorder, cost_sink = build_cost_sink(billing)
    api = build_api(db_path, with_ai=with_ai, cost_sink=cost_sink)
    analytics = build_console(**(console_kwargs or {})) if with_console else None
    server = ThreadingHTTPServer((host, port), make_handler(api, billing, analytics))
    print(f"Quintek student API on http://{host}:{port}  (Ctrl+C to stop)")
    if billing is None:
        print("  billing: not mounted")
    else:
        from billing.gateway_http import credentials_from_env
        from billing.mount import PREFIX

        creds = credentials_from_env()
        gateway = f"razorpay({creds['mode']})" if billing.gateway else "NONE"
        print(f"  billing: http://{host}:{port}{PREFIX}   gateway={gateway}")
        if billing.gateway is None:
            # Say it at startup, not at checkout. A payment surface that is
            # armed but cannot authenticate fails in front of a customer.
            print("    no gateway credentials configured -- checkout will refuse,"
                  " everything else works")
        elif not creds["webhook_secret_present"]:
            print("    WARNING: no webhook secret -- incoming webhooks will be"
                  " refused as unverifiable")
        if recorder is not None:
            print(f"  cost ledger: recording, {recorder.priced_models} model"
                  " price(s) configured")
            if recorder.priced_models == 0:
                # Every call would be logged as unpriced. Worth saying at
                # startup rather than discovering it on the economics screen.
                print("    WARNING: no model prices configured, so every call"
                      " will be recorded as UNPRICED (not as free)")
    if api.generator is None:
        print("  note: no AI services attached — ingestion and generation will report 503")
    else:
        # Say which model this process will actually call, at startup rather
        # than on the first learner request. A deployment silently serving the
        # scripted provider looks identical to one serving a real model until
        # somebody reads an answer.
        from benchmark.providers.registry import describe, spec_from_env
        report = describe(spec_from_env())
        label = f"{report['provider']}" + (f" · {report['model_id']}" if report["model_id"] else "")
        print(f"  provider: {label}"
              + ("" if report["is_real_model"] else "  (NOT a real model — scripted test double)"))
        if not report["buildable"]:
            print(f"  WARNING: this provider cannot be built here — {report['reason']}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
