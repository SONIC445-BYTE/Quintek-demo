"""
Run the adversarial battery against a real validator.

    NVIDIA_API_KEY=... python3 tools_adversarial_run.py \
        --provider nvidia --validator meta/llama-3.1-8b-instruct

Answers the question the Alpha-0 run raises but cannot settle from one item:
of twenty questions broken in ten known ways, how many does this validator
catch, and does it leave sound questions alone?

The control arm is not optional. A validator that flags everything scores
100% detection, so the report is meaningless without the false-flag rate
beside it.
"""

from __future__ import annotations

import argparse
import functools
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
print = functools.partial(print, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Adversarial battery against a real validator")
    parser.add_argument("--provider", default="nvidia")
    parser.add_argument("--validator", default="meta/llama-3.1-8b-instruct")
    parser.add_argument("--generator", default="meta/llama-3.1-70b-instruct",
                        help="the candidate recorded as having written each question, so the "
                             "validator's independence check has something to be independent of")
    parser.add_argument("--controls", type=int, default=10,
                        help="how many sound items to run as the control arm")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--out", default="alpha0_runs")
    args = parser.parse_args()

    from benchmark.adversarial import AdversarialRun, load_battery, write_report
    from benchmark.providers.registry import build_provider, describe
    from student.ai import AIEngine
    from student.db import Database, new_id, now_iso
    from student.validation import QuestionValidator

    spec = {"provider": args.provider, "model_id": args.validator,
            "timeout_seconds": args.timeout}
    report = describe(spec)
    if not report["buildable"]:
        print(f"cannot build the validator: {report['reason']}")
        return 2

    run_id = f"adversarial-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    out_dir = Path(args.out) / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Adversarial battery  ·  {run_id}")
    print(f"  validator: {report['provider']} · {report['model_id']}")
    print(f"  generator recorded as: {args.provider}:{args.generator}\n")

    db = Database(out_dir / "battery.db")
    uid = db.create_user("battery@quintek.test", "a-long-enough-password")
    notebook, stamp = new_id("nb"), now_iso()
    db.execute("INSERT INTO notebooks (id, owner_id, title, created_at) VALUES (?,?,?,?)",
               (notebook, uid, "Adversarial battery", stamp))
    source = new_id("src")
    db.execute("INSERT INTO sources (id, notebook_id, kind, status, uploaded_at)"
               " VALUES (?,?,?,?,?)", (source, notebook, "text", "extracted", stamp))

    gen_candidate = f"{args.provider}:{args.generator}"
    val_ai = AIEngine(db, provider_factory=lambda c: build_provider(spec),
                      development_candidate=f"{args.provider}:{args.validator}")
    validator = QuestionValidator(db, val_ai)

    counter = {"n": 0}

    def validate_corpus_item(item):
        """Store the corpus item as a question, then validate it for real."""
        counter["n"] += 1
        chunk = new_id("chk")
        db.execute("INSERT INTO source_chunks (id, source_id, ordinal, text, locator_json,"
                   " status) VALUES (?,?,?,?,?,?)",
                   (chunk, source, counter["n"],
                    item.source_passage or "(no passage was supplied with this item)",
                    json.dumps({"item": item.id}), "processed"))
        qid = new_id("q")
        db.execute(
            "INSERT INTO questions (id, primary_notebook_id, family, stem, options_json,"
            " correct_index, rationale, difficulty, reasoning_depth, source_id, chunk_id,"
            " generated_by_candidate_id, prompt_version, demo_ids_json, validation_status,"
            " generated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending', ?)",
            (qid, notebook, item.question_type, item.stem, json.dumps(item.options),
             item.correct_index, item.explanation, item.difficulty, "reasoning",
             source, chunk, gen_candidate, "battery-v1", "[]", now_iso()))
        started = time.time()
        result = validator.validate(qid)
        label = item.defect_class or "sound"
        mark = "caught" if (result["status"] == "flagged") == item.is_negative else "MISSED"
        print(f"  {counter['n']:>3}. {label:<24} -> {result['status']:<9} "
              f"[{mark}]  {time.time() - started:.1f}s")
        return result

    adversarial, sound = load_battery()
    controls = sound[:args.controls]
    print(f"  {len(adversarial)} adversarial items, {len(controls)} sound controls\n")

    started = time.time()
    result = AdversarialRun(validate_corpus_item).run(adversarial, controls)
    result["validator"] = report
    result["generator_recorded_as"] = gen_candidate
    result["elapsed_seconds"] = round(time.time() - started, 1)
    path = write_report(result, out_dir / "adversarial_report.json")

    print("\n" + "=" * 72)
    print("ADVERSARIAL BATTERY RESULT")
    print("=" * 72)
    print(f"  detection rate   : {result['detected']}/{result['adversarial_n']}"
          f"  ({(result['detection_rate'] or 0):.0%})")
    print(f"  caught for the right reason: {result['detected_for_the_right_reason']}"
          f"/{result['detected']}")
    print(f"  false-flag rate  : {result['false_flags']}/{result['control_n']}"
          f"  ({(result['false_flag_rate'] or 0):.0%})")
    print("\n  per defect class:")
    for defect, stats in result["per_defect_class"].items():
        if stats["n"]:
            print(f"    {defect:<24} {stats['detected']}/{stats['n']}")
    print(f"\n  {result['interpretation']}")
    print(f"\n  report: {path}")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
