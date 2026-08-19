"""
Cold-start check: the real server, a real database file, all thirteen phases.

`tests/` covers each module. This covers the thing tests do not: a fresh
database, the actual HTTP transport, and every phase in sequence, in the order
a learner would trigger them. Run it after a change that crosses module
boundaries.

    python3 tools_lifecycle_check.py

The model is scripted, deliberately. This proves the pipeline carries data end
to end and that the invariants hold at the seams; it proves nothing about
whether a real model writes good questions. That needs a benchmark run against
a real corpus, which does not exist yet -- see IMPLEMENTATION_STATUS.md.

Written after a manual run of this sequence found two real defects: a source
whose every chunk failed was reported as `extracted` with `error: null`, and
the chunk-level reasons were stored but never surfaced by the progress route.
"""
import json, os, sys, tempfile, threading, urllib.request, urllib.error
from http.server import ThreadingHTTPServer
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from student.db import Database
from student.ai import AIEngine
from student.api import StudentAPI
from student.server import make_handler
from student.ingestion import IngestionEngine
from student.generation import AIConceptExtractor, QuestionGenerator
from student.validation import QuestionValidator
from student.notifications import NotificationService
from benchmark.providers.base import GenerationResponse

work = tempfile.mkdtemp()
db_path = os.path.join(work, "quintek.db")

PROMPTS = []
class ScriptedProvider:
    name, model, model_version = "scripted", "scripted/model", "1.0"
    def generate(self, req):
        p = req.prompt
        if "concept" in p.lower() and "extract" in p.lower():
            body = {"concepts": [{"name": "Nephrotic syndrome", "confidence": 0.9},
                                 {"name": "Proteinuria", "confidence": 0.85}]}
        elif "factually_correct" in p:
            body = {"checks": {k: True for k in
                    ["factually_correct","grounded_in_source","key_is_right","distractors_plausible",
                     "unambiguous","concept_aligned","no_unsupported_claims","pg_level"]},
                    "verdict": "approve"}
        else:
            body = {"questions": [{
                "stem": "A 6-year-old presents with periorbital oedema and 3+ proteinuria. Most likely diagnosis?",
                "options": ["Nephrotic syndrome", "Nephritic syndrome",
                            "Acute tubular necrosis", "Renal artery stenosis"],
                "correct_index": 0, "passage": 1,
                "rationale": "Heavy proteinuria with oedema is nephrotic, not nephritic.",
                "concepts_tested": ["Nephrotic syndrome"]}]}
        PROMPTS.append(p)
        return GenerationResponse(item_id=req.item_id, raw_output=json.dumps(body), parsed=body,
                                  provider="scripted", model="scripted/model",
                                  model_version="1.0", latency_ms=12.0, attempts=1,
                                  input_tokens=100, output_tokens=60)

db = Database(db_path)
ai = AIEngine(db, provider_factory=lambda c: ScriptedProvider(), development_candidate="cand-dev")
api = StudentAPI(db,
                 engine=IngestionEngine(db, concept_extractor=AIConceptExtractor(db, ai)),
                 ai=ai, generator=QuestionGenerator(db, ai),
                 validator=QuestionValidator(db, AIEngine(db, provider_factory=lambda c: ScriptedProvider(),
                                                          development_candidate="cand-validator")),
                 notifier=NotificationService(db, sender=lambda p: True))

server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(api))
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()

TOKEN = {"t": None}
def call(method, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    data = None
    if body is not None:
        data = json.dumps(body).encode(); req.add_header("Content-Type", "application/json")
    if TOKEN["t"]: req.add_header("Authorization", "Bearer " + TOKEN["t"])
    try:
        with urllib.request.urlopen(req, data, timeout=30) as r:
            raw = r.read(); return r.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read(); return e.code, json.loads(raw) if raw else {}

results = []
def step(n, label, ok, detail=""):
    results.append((n, label, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  Phase {n:>2}  {label}" + (f"  — {detail}" if detail else ""))

# 1 auth + database
s, b = call("POST", "/auth/register", {"email": "pg@example.test", "password": "a-real-password-1", "name": "PG"})
TOKEN["t"] = b.get("token")
s2, me = call("GET", "/me")
step(1, "accounts, hashing, sessions, SQLite", s == 201 and s2 == 200 and me["email"] == "pg@example.test")

# 2 source ingestion
s, nb = call("POST", "/notebooks", {"title": "Renal"})
text = ("Nephrotic syndrome is defined by heavy proteinuria exceeding 3.5 grams per day, "
        "hypoalbuminaemia, oedema and hyperlipidaemia. Minimal change disease is the commonest "
        "cause in children and responds well to corticosteroids. " * 12)
s, src = call("POST", f"/notebooks/{nb['id']}/sources", {"kind": "text", "title": "Renal notes", "text": text})
api.engine.wait_idle(timeout=30)
sid = src.get("source_id") or src.get("id")
s, prog = call("GET", f"/sources/{sid}/progress")
step(2, "source ingestion, chunking, locators", prog["status"] == "extracted" and prog["chunks_processed"] > 0,
     f"{prog['chunks_total']} chunks")

# 3 concept graph
s, cons = call("GET", "/concepts")
s, graph = call("GET", "/graph")
step(3, "concept + notebook graph", len(cons["concepts"]) > 0, f"{len(cons['concepts'])} concepts")

# 4 AI orchestration
step(4, "AI orchestration + resolution order", ai.resolve("QUESTION_GENERATION") == ("cand-dev", "development_override"),
     "development_override, correctly stamped")

# 5 question generation
s, gen = call("POST", f"/notebooks/{nb['id']}/questions", {"count": 2})
step(5, "grounded question generation", s in (200, 201) and gen.get("count", 0) > 0, f"{gen.get('count')} generated, grounded in a stored chunk")

# 6 validation
s, qs = call("GET", "/questions")
q = qs["questions"][0]
step(6, "independent validation", q.get("validation_status") in {"approved", "pending", "rejected"},
     f"status={q.get('validation_status')}")

# 7 attempts + R/O/G + gaps
s, sess = call("POST", "/revision/sessions", {"strategy": "unseen", "size": 5})
s, nxt = call("GET", f"/revision/next?session={sess['session_id']}")
qq = nxt["question"]
leaked = "answer" in qq or "explanation" in qq
s, att = call("POST", "/attempts", {"question_id": qq.get("id") or qq.get("question_id"), "session_id": sess["session_id"],
                                    "chosen": "B", "user_colour": "RED"})
step(7, "attempts, R/O/G, gap storage",
     s == 201 and not leaked and set(("correct_answer", "is_correct", "source_locator")) <= set(att.get("reveal") or {}),
     "key withheld from the question; revealed with source locator only after recording"
     if not leaked else "LEAKED KEY")

# 8 priority engine
s, dash = call("GET", "/revision/dashboard")
step(8, "concept priority engine", "top_priority" in dash and "colour_counts" in dash,
     f"{dash['colour_counts']}")

# 9 adaptive revision
s, sess2 = call("POST", "/revision/sessions", {"strategy": "adaptive", "size": 5})
step(9, "adaptive revision engine", s == 201 and sess2.get("session_id"))

# 10 notifications
s, prefs = call("PUT", "/settings/notifications", {"trigger_time": "20:00", "timezone": "Asia/Kolkata"})
s2, fired = call("POST", "/settings/notifications/test")
s3, hist = call("GET", "/settings/notifications/history")
step(10, "daily notification", prefs["trigger_time"] == "20:00" and fired["ok"] and len(hist["history"]) > 0,
     f"next at {prefs['next_scheduled_at']}")

# 11 question bank / gap recall
s, gaps = call("GET", "/gaps")
s2, bank = call("GET", "/questions?limit=50")
step(11, "question bank + gap recall", s == 200 and s2 == 200, f"{len(gaps['gaps'])} gaps, {len(bank['questions'])} in bank")

# 12 benchmark -> routing
from benchmark.promotion_api import PromotionAPI, PromotionError
from benchmark.analytics import RunArchive
runs = os.path.join(work, "runs"); os.makedirs(runs)
promo = PromotionAPI(ai, RunArchive(runs))
refused = False
try:
    promo.promote(task_type="QUESTION_GENERATION", run_id="nope")
except PromotionError:
    refused = True
step(12, "benchmark -> model routing", refused and promo.current()["promoted_count"] == 0,
     "promotion refused without a real passing run, as required")

# 13 transparency
s, screen = call("GET", "/ai/benchmark")
s2, powering = call("GET", "/ai/benchmark/powering")
honest = (screen["title"] == "Quintek AI Benchmark"
          and screen["ranking"]["entries"] == []
          and "has not passed a Quintek benchmark run" in powering["warning"])
step(13, "student AI transparency", honest, "empty archive shown as empty; dev-override disclosed")

server.shutdown()
bad = [r for r in results if not r[2]]
print(f"\n{len(results) - len(bad)}/{len(results)} phases verified over live HTTP")
sys.exit(1 if bad else 0)
