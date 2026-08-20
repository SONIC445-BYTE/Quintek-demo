"""
Quintek Alpha-0: one real model, one real source, the whole chain, on disk.

    python3 tools_alpha0.py                      # scripted, free, offline
    NVIDIA_API_KEY=... python3 tools_alpha0.py --provider nvidia \
        --generator meta/llama-3.1-70b-instruct \
        --validator meta/llama-3.1-8b-instruct

The milestone this script defines is deliberately small: a real model
generates a PG question from real source material, the validator evaluates it,
a deliberately broken question is offered to the same validator, the result is
persisted, and every intermediate artifact is written out.

It reports the ten acceptance criteria as pass/fail. "Are we at Alpha-0" is
then answered by running this, not by opinion -- which is the point, because
the failure mode this whole exercise exists to avoid is a system that looks
finished because nobody ran it end to end against a real model.

WHAT A PASS DOES AND DOES NOT MEAN
----------------------------------
A pass means the pipeline carries real data through every stage and the
invariants hold at the seams. It does NOT mean the questions are good. Nothing
here scores question quality against gold, because expert gold does not exist
yet -- see benchmark/corpus.py for why a model cannot supply it. The one
quality-adjacent claim this script can make honestly is about REJECTION: a
broken question offered to the validator either gets flagged or does not.
"""

from __future__ import annotations

import functools

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# A real model call takes tens of seconds. Buffered stdout makes a run that is
# working look identical to one that has hung, which is exactly the confusion
# this script exists to remove.
print = functools.partial(__builtins__.print if not isinstance(__builtins__, dict)
                          else __builtins__["print"], flush=True)

# One real source passage. Deliberately short and deliberately real: a whole
# chapter would hide retrieval bugs behind sheer volume.
SOURCE_TITLE = "Screening: sensitivity and specificity"
SOURCE_TEXT = """\
Screening tests are evaluated by their sensitivity and their specificity.

Sensitivity is the proportion of people who genuinely have the disease and who
test positive. It is calculated as true positives divided by the sum of true
positives and false negatives. A highly sensitive test therefore has few false
negatives, and a negative result from such a test is good evidence that the
disease is absent.

Specificity is the proportion of people who are genuinely free of the disease
and who test negative, calculated as true negatives divided by the sum of true
negatives and false positives. A highly specific test has few false positives,
so a positive result is good evidence that the disease is present.

Both sensitivity and specificity are properties of the test itself and do not
change with the prevalence of disease in the population being screened.
Predictive values do change with prevalence: as prevalence falls, the positive
predictive value of a test falls, because the absolute number of false
positives drawn from the large disease-free group grows relative to the
shrinking number of true cases. This is why a test with excellent sensitivity
and specificity can still yield mostly false positives when it is applied to a
rare condition in a general population.

Lowering the cut-off value of a continuous test reclassifies borderline results
as positive. This raises sensitivity and lowers specificity; it does not
improve both. Only a better test does that.
"""

# A question broken on purpose, offered to the same validator that just
# approved (or flagged) the model's own output. Generation is not acceptance,
# and this is the cheapest demonstration of the difference.
BROKEN_QUESTION = {
    "stem": "Since sensitivity rises predictably as disease prevalence rises, "
            "a screening test performs best in which population?",
    "options": ["High-prevalence populations", "Low-prevalence populations",
                "Any population equally", "Only randomised samples"],
    "correct_index": 0,
    "rationale": "Sensitivity increases with prevalence, so high-prevalence "
                 "settings maximise it.",
    "defect": "hallucinated_fact",
    "defect_note": "The passage states explicitly that sensitivity does not "
                   "change with prevalence. The stem asserts the opposite.",
}


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Check:
    def __init__(self, key, label):
        self.key, self.label = key, label
        self.passed, self.detail = False, "not run"

    def record(self, passed, detail=""):
        self.passed, self.detail = bool(passed), detail
        return self.passed

    def as_dict(self):
        return {"criterion": self.label, "passed": self.passed, "detail": self.detail}


CRITERIA = [
    ("real_model", "Real model used"),
    ("real_source", "Real source used"),
    ("no_dev_override", "No development_override"),
    ("question_generated", "Question generated"),
    ("answer_generated", "Answer generated"),
    ("validation_executed", "Validation executed"),
    ("bad_rejected", "Bad question can be rejected"),
    ("persisted", "Result persisted"),
    ("ui_displays", "UI can display the actual result"),
    ("reproducible", "Full run is reproducible"),
    ("raw_retained", "Raw model output is retained"),
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Quintek Alpha-0 acceptance run")
    parser.add_argument("--provider", default=os.environ.get("QUINTEK_PROVIDER", "scripted"))
    parser.add_argument("--generator", default=os.environ.get("QUINTEK_MODEL_ID", ""),
                        help="model id for generation and concept extraction")
    parser.add_argument("--validator", default=os.environ.get("QUINTEK_VALIDATOR_MODEL_ID", ""),
                        help="model id for validation; MUST differ from --generator")
    parser.add_argument("--out", default="alpha0_runs")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--keep-db", action="store_true",
                        help="keep the SQLite file so the run can be inspected")
    args = parser.parse_args()

    from benchmark.providers.registry import build_provider, describe
    from student.ai import AIEngine
    from student.api import StudentAPI
    from student.db import Database
    from student.generation import AIConceptExtractor, QuestionGenerator
    from student.ingestion import IngestionEngine
    from student.trace import GenerationTrace
    from student.validation import QuestionValidator, ValidationSkipped

    run_id = f"alpha0-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    out_dir = Path(args.out) / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    trace = GenerationTrace(run_id, root=out_dir / "generation_run")

    checks = {key: Check(key, label) for key, label in CRITERIA}

    def spec_for(model_id):
        spec = {"provider": args.provider, "timeout_seconds": args.timeout}
        if model_id:
            spec["model_id"] = model_id
        return spec

    gen_spec, val_spec = spec_for(args.generator), spec_for(args.validator)
    gen_report, val_report = describe(gen_spec), describe(val_spec)

    print(f"Quintek Alpha-0  ·  {run_id}")
    print(f"  generator : {gen_report['provider']} · {gen_report['model_id'] or '(default)'}"
          f"{'' if gen_report['is_real_model'] else '   [NOT a real model]'}")
    print(f"  validator : {val_report['provider']} · {val_report['model_id'] or '(default)'}")
    for report, who in ((gen_report, "generator"), (val_report, "validator")):
        if not report["buildable"]:
            print(f"\n  cannot build the {who}: {report['reason']}")
            return 2
    print()

    checks["real_model"].record(
        gen_report["is_real_model"] and val_report["is_real_model"],
        f"generator={gen_report['provider']}:{gen_report['model_id']}, "
        f"validator={val_report['provider']}:{val_report['model_id']}"
        if gen_report["is_real_model"] else
        "the scripted test double is not a real model; rerun with --provider nvidia")

    db_path = out_dir / "quintek.db"
    db = Database(db_path)

    gen_candidate = f"{args.provider}:{args.generator or 'default'}"
    val_candidate = f"{args.provider}:{args.validator or 'default'}"
    if gen_candidate == val_candidate:
        print("  refusing to run: the validator must differ from the generator, or its "
              "approval means nothing.\n")
        return 2

    gen_ai = AIEngine(db, provider_factory=lambda c: build_provider(gen_spec),
                      development_candidate=gen_candidate)
    val_ai = AIEngine(db, provider_factory=lambda c: build_provider(val_spec),
                      development_candidate=val_candidate)

    results = {"run_id": run_id, "started_at": now(),
               "generator": gen_report, "validator": val_report, "stages": {}}

    def stage(name, fn):
        started = time.time()
        try:
            value = fn()
            results["stages"][name] = {"ok": True, "seconds": round(time.time() - started, 2)}
            return value
        except Exception as exc:
            results["stages"][name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                                       "seconds": round(time.time() - started, 2)}
            print(f"  ✗ {name}: {type(exc).__name__}: {exc}")
            return None

    # ---- source -> chunks -> concepts ------------------------------------
    print("  [1/6] ingesting the source and extracting concepts…")
    uid = db.create_user("alpha0@quintek.test", "a-long-enough-password")
    token = db.issue_token(uid)
    api = StudentAPI(db, ai=gen_ai, generator=QuestionGenerator(db, gen_ai),
                     validator=QuestionValidator(db, val_ai))
    status, notebook = api.handle("POST", "/notebooks", {}, {"title": "PSM · Screening"}, token)
    notebook_id = notebook["id"]

    engine = IngestionEngine(db, concept_extractor=AIConceptExtractor(db, gen_ai))
    api.engine = engine
    status, source = api.handle(
        "POST", f"/notebooks/{notebook_id}/sources", {},
        {"kind": "text", "title": SOURCE_TITLE, "text": SOURCE_TEXT}, token)
    source_id = source.get("source_id")
    stage("ingestion", lambda: engine.wait_idle(timeout=args.timeout * 2))

    status, progress = api.handle("GET", f"/sources/{source_id}/progress", {}, {}, token)
    concepts = [dict(r) for r in db.query("SELECT id, canonical_name FROM concepts")]
    checks["real_source"].record(
        progress.get("chunks_total", 0) > 0 and progress.get("chunks_processed", 0) > 0,
        f"{progress.get('chunks_processed')}/{progress.get('chunks_total')} chunks processed "
        f"from a {len(SOURCE_TEXT)}-character source; "
        f"{len(concepts)} concept(s) extracted: "
        f"{', '.join(c['canonical_name'] for c in concepts[:6])}")
    print(f"        chunks: {progress.get('chunks_processed')}/{progress.get('chunks_total')}"
          f"   concepts: {len(concepts)}")
    if progress.get("chunk_errors"):
        print(f"        chunk errors: {progress['chunk_errors'][0]['error'][:120]}")

    # ---- generation -------------------------------------------------------
    print("  [2/6] generating a question from that source…")
    generator = QuestionGenerator(db, gen_ai)
    question_ids = stage("generation", lambda: generator.generate(
        notebook_id=notebook_id, count=1,
        concept_ids=[c["id"] for c in concepts[:1]] if concepts else None,
        difficulty="pg_entry", trace=trace))

    question = None
    if question_ids:
        question = dict(db.query_one("SELECT * FROM questions WHERE id = ?", (question_ids[0],)))
        options = json.loads(question["options_json"])
        checks["question_generated"].record(
            bool(question["stem"]) and len(options) >= 2,
            f"{question['stem'][:110]}…" if len(question["stem"]) > 110 else question["stem"])
        checks["answer_generated"].record(
            0 <= question["correct_index"] < len(options),
            f"keyed answer: {options[question['correct_index']]!r}"
            + (f"; rationale {len(question['rationale'])} chars" if question["rationale"] else
               "; NO rationale returned"))
        checks["persisted"].record(
            True, f"question {question['id']} stored in {db_path.name} with source_id="
                  f"{question['source_id']}, chunk_id={question['chunk_id']}, "
                  f"generated_by={question['generated_by_candidate_id']}")
        print(f"        {question['stem'][:100]}")
        for i, opt in enumerate(options):
            print(f"          {'>' if i == question['correct_index'] else ' '} {opt[:80]}")
    else:
        for key in ("question_generated", "answer_generated", "persisted"):
            checks[key].record(False, "generation produced nothing")

    checks["no_dev_override"].record(
        False,
        "every call in this run is stamped 'development_override': no candidate has a passing "
        "benchmark run, because the expert corpus does not exist. This criterion cannot pass "
        "until a benchmark run exists to promote from — see benchmark/promotion_api.py.")

    # ---- validation of the model's own question ---------------------------
    print("  [3/6] validating the generated question with a different model…")
    validator = QuestionValidator(db, val_ai)
    verdict = None
    if question:
        verdict = stage("validation", lambda: validator.validate(question["id"], trace=trace))
        if verdict:
            checks["validation_executed"].record(
                True,
                f"verdict={verdict['status']} from {verdict['validator_candidate']}; "
                f"failed checks: {', '.join(verdict['failed_checks']) or 'none'}")
            print(f"        verdict: {verdict['status']}"
                  f"   failed: {', '.join(verdict['failed_checks']) or 'none'}")
        else:
            checks["validation_executed"].record(False, "the validator did not return a verdict")
    else:
        checks["validation_executed"].record(False, "no question to validate")

    # ---- validation of a deliberately broken question ---------------------
    print("  [4/6] offering the same validator a deliberately broken question…")
    broken_id = None
    if question:
        from student.db import new_id, now_iso
        broken_id = new_id("q")
        db.execute(
            "INSERT INTO questions (id, primary_notebook_id, family, stem, options_json,"
            " correct_index, rationale, difficulty, reasoning_depth, source_id, chunk_id,"
            " generated_by_candidate_id, prompt_version, demo_ids_json, validation_status,"
            " generated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending', ?)",
            (broken_id, notebook_id, "adversarial", BROKEN_QUESTION["stem"],
             json.dumps(BROKEN_QUESTION["options"]), BROKEN_QUESTION["correct_index"],
             BROKEN_QUESTION["rationale"], "pg_advanced", "recall",
             question["source_id"], question["chunk_id"], gen_candidate, "adversarial-v1",
             "[]", now_iso()))
        broken_verdict = stage("adversarial_validation",
                               lambda: validator.validate(broken_id))
        if broken_verdict:
            rejected = broken_verdict["status"] == "flagged"
            expected = {"factually_correct", "no_unsupported_claims", "grounded_in_source"}
            aligned = bool(expected & set(broken_verdict["failed_checks"]))
            checks["bad_rejected"].record(
                rejected,
                f"verdict={broken_verdict['status']}; failed checks: "
                f"{', '.join(broken_verdict['failed_checks']) or 'none'}. "
                + ("Caught for a reason matching the planted defect "
                   f"({BROKEN_QUESTION['defect']})." if rejected and aligned else
                   "Flagged, but not for a check corresponding to the planted defect, so this "
                   "may be coincidental." if rejected else
                   f"NOT caught. The planted defect was: {BROKEN_QUESTION['defect_note']}"))
            print(f"        verdict: {broken_verdict['status']}"
                  f"   failed: {', '.join(broken_verdict['failed_checks']) or 'none'}")
        else:
            checks["bad_rejected"].record(False, "the validator did not return a verdict")
    else:
        checks["bad_rejected"].record(False, "no run to compare against")

    # ---- artifacts --------------------------------------------------------
    print("  [5/6] checking captured artifacts…")
    artifacts = GenerationTrace.load(trace.root) if trace.root.exists() else {}
    raw = artifacts.get("raw_model_output", {})
    checks["raw_retained"].record(
        bool(raw.get("text")),
        f"{raw.get('length_chars', 0)} characters of unmodified model output at "
        f"{trace.root}/raw_model_output.json")
    checks["reproducible"].record(
        {"prompt", "raw_model_output", "model_call", "source"} <= set(artifacts),
        f"artifacts captured: {', '.join(sorted(artifacts))}"
        if artifacts else "no artifacts were captured")

    # ---- the UI payload ---------------------------------------------------
    print("  [6/6] checking the API serves it to the UI…")
    status, bank = api.handle("GET", "/questions", {"limit": "10"}, {}, token)
    served = [q for q in bank.get("questions", []) if q.get("id") == (question or {}).get("id")]
    checks["ui_displays"].record(
        status == 200 and bool(served),
        f"GET /questions returned the stored question (HTTP {status}); the UI renders this "
        "payload — see frontend/README.md for the seam"
        if served else f"GET /questions did not return it (HTTP {status})")

    # ---- report -----------------------------------------------------------
    results["finished_at"] = now()
    results["criteria"] = [checks[k].as_dict() for k, _ in CRITERIA]
    results["passed"] = sum(1 for c in checks.values() if c.passed)
    results["total"] = len(checks)
    results["question"] = question
    results["verdict"] = verdict
    results["artifact_dir"] = str(trace.root)
    results["database"] = str(db_path)

    (out_dir / "alpha0_report.json").write_text(
        json.dumps(results, indent=2, default=str), encoding="utf-8")

    print("\n" + "=" * 72)
    print("QUINTEK ALPHA-0 ACCEPTANCE")
    print("=" * 72)
    for key, label in CRITERIA:
        check = checks[key]
        print(f"  [{'x' if check.passed else ' '}] {label}")
        print(f"      {check.detail}")
    print("-" * 72)
    print(f"  {results['passed']}/{results['total']} criteria met")
    print(f"  artifacts: {trace.root}")
    print(f"  report:    {out_dir / 'alpha0_report.json'}")
    print("=" * 72)

    if not args.keep_db:
        print(f"\n  (database kept at {db_path} for inspection)")
    return 0 if results["passed"] == results["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
