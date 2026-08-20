"""
Quintek Real-Model Benchmark v0.1 -- the same tasks, across providers.

    python3 tools_provider_matrix.py --providers nvidia,cerebras,openrouter \
        --n 20 --mode evaluation

Answers the question that matters, which is not "is NVIDIA good?" but:

    which model/provider combination is best for Quintek, at which task?

Design follows from what the earlier runs cost:

  * **Rotation, not blocks.** Every candidate meets a comparable spread of
    tasks, and the same task is answered by several of them, so a difference
    between candidates is not a difference between the questions they drew.
  * **Quota per (candidate, task type).** The interesting result is "C is best
    at conceptual reasoning, B at vignettes", which a single total hides.
  * **A breaker in the path.** A stalling endpoint stops taking work instead
    of absorbing the run into timeouts.
  * **Small first.** 20 items, not 500. If a provider cannot produce usable
    output over 20, paying for 480 more establishes nothing.
  * **Latency recorded per call**, because the finding on this project was
    never about quality alone.

Every result is written to the inference ledger, so the matrix is a view over
recorded evidence rather than a number that exists only in this script's
stdout.
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

# provider -> (model_id, key_env). Deliberately explicit: guessing a model id
# for a provider would misattribute every result it produced.
DEFAULT_MODELS = {
    "nvidia": ("meta/llama-3.1-8b-instruct", "NVIDIA_API_KEY"),
    "cerebras": ("llama3.1-8b", "CEREBRAS_API_KEY"),
    "openrouter": ("meta-llama/llama-3.1-8b-instruct", "OPENROUTER_API_KEY"),
    "scripted": ("scripted/model", ""),
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Provider comparison matrix")
    parser.add_argument("--providers", default="scripted",
                        help="comma-separated provider names")
    parser.add_argument("--models", default="",
                        help="comma-separated provider=model overrides")
    parser.add_argument("--n", type=int, default=20, help="tasks to run (start small)")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--replicas", type=int, default=2,
                        help="how many candidates answer each task")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--ledger", default="inference.db")
    parser.add_argument("--out", default="alpha0_runs")
    args = parser.parse_args()

    from benchmark.adversarial import load_battery
    from benchmark.batch import BatchRunner, Job, StopPolicy
    from benchmark.corpus import load
    from benchmark.evaluation import build_rotation, paired_coverage
    from benchmark.health import BreakerPolicy, HealthRegistry, PROTOTYPE
    from benchmark.inference_log import InferenceLog, InferenceRecord
    from benchmark.providers.base import GenerationRequest
    from benchmark.providers.registry import build_provider, describe

    overrides = {}
    for pair in filter(None, args.models.split(",")):
        provider, _, model = pair.partition("=")
        overrides[provider.strip()] = model.strip()

    specs, unusable = {}, {}
    for provider in [p.strip() for p in args.providers.split(",") if p.strip()]:
        model, _ = DEFAULT_MODELS.get(provider, (None, ""))
        model = overrides.get(provider, model)
        spec = {"provider": provider, "model_id": model, "timeout_seconds": args.timeout}
        report = describe(spec)
        if report["buildable"]:
            specs[f"{provider}:{model}"] = spec
        else:
            unusable[f"{provider}:{model}"] = report["reason"]

    print("Quintek Real-Model Benchmark v0.1")
    for key, spec in specs.items():
        print(f"  ready   {key}")
    for key, reason in unusable.items():
        print(f"  SKIP    {key}  — {reason[:100]}")
    if not specs:
        print("\nNo provider could be built. Nothing to compare.")
        return 2
    if len(specs) < 2:
        print("\n  note: only one candidate is usable, so this run measures it rather than "
              "comparing anything.")
    print()

    # Tasks: sound corpus items (can the model answer?) plus adversarial ones
    # (can it tell that something is wrong?). Both matter and they measure
    # different things.
    sound = [i for i in load("corpus/development.jsonl")][:args.n]
    adversarial, _ = load_battery()
    items = {i.id: i for i in sound}
    tasks = [(i.id, i.question_type.upper()) for i in sound]

    candidates = sorted(specs)
    plan = build_rotation(tasks, candidates, replicas=min(args.replicas, len(candidates)))
    print(f"  {len(tasks)} tasks x {min(args.replicas, len(candidates))} replicas "
          f"across {len(candidates)} candidate(s)")
    print(f"  quota matrix: {json.dumps(plan.quota_matrix())}\n")

    ledger = InferenceLog(args.ledger)
    health = HealthRegistry(policy=BreakerPolicy(
        failure_threshold=3, timeout_weight=2, cooldown_seconds=45,
        slow_call_ms=args.timeout * 1000))
    for key in specs:
        # Every serverless endpoint here is a prototype until measured
        # otherwise. Declared, not inferred -- see benchmark/health.py.
        health.declare(key, PROTOTYPE)

    jobs = []
    for assignment in plan.assignments:
        for candidate in assignment.candidates:
            jobs.append(Job(task_id=assignment.task_id, task_type=assignment.task_type,
                            candidate=candidate,
                            payload={"item_id": assignment.task_id}))

    def execute(job):
        item = items[job.payload["item_id"]]
        spec = specs[job.candidate]
        provider = build_provider(spec)
        options = "\n".join(f"{chr(65 + n)}. {o}" for n, o in enumerate(item.options))
        prompt = (
            "Answer this postgraduate medical question. Reply with ONLY a JSON object:\n"
            '{"answer": "A", "confidence": 0.0, "reasoning": "one sentence"}\n\n'
            f"{item.stem}\n{options}")

        started = time.monotonic()
        response = provider.generate(GenerationRequest(item_id=job.task_id, prompt=prompt,
                                                       max_tokens=250, temperature=0.0))
        latency_ms = (time.monotonic() - started) * 1000
        parsed = response.parsed if isinstance(response.parsed, dict) else None
        letter = (parsed or {}).get("answer", "")
        correct = (isinstance(letter, str) and letter[:1].upper()
                   == chr(65 + item.correct_index))

        record = InferenceRecord(
            provider=spec["provider"], model=spec["model_id"], task_id=job.task_id,
            task_type=job.task_type, task_complexity=item.difficulty,
            prompt_tokens=response.input_tokens, output_tokens=response.output_tokens,
            latency_ms=latency_ms, success=response.ok, timeout="timeout" in
            (response.error or "").lower(), error=response.error,
            attempts=response.attempts, routing_mode="evaluation",
            routing_reason="provider comparison matrix",
            structured_ok=parsed is not None)
        run_id = ledger.record(record)
        ledger.record_outcome(run_id, quality_score=1.0 if correct else 0.0,
                              accepted=correct, judged_by="corpus key")

        if not response.ok:
            raise RuntimeError(response.error or "provider call failed")
        return {"raw_output": response.raw_output, "accepted": correct}

    print("  running…")
    started = time.time()
    result = BatchRunner(
        execute, workers=args.workers, health=health,
        candidate_for=lambda j: j.candidate, max_attempts=2,
        stop_policy=StopPolicy(max_consecutive_failures=10),
        on_job=lambda j: print(f"    {j.task_id:<10} {j.candidate:<40} {j.status:<8}"
                               f"{(j.latency_ms or 0) / 1000:6.1f}s"
                               f"{'  ✓' if j.accepted else ''}")
    ).run(jobs)
    elapsed = time.time() - started

    # ---- the matrix ----
    matrix: dict[str, dict[str, dict]] = {}
    for job in result["jobs"]:
        cell = matrix.setdefault(job["candidate"], {}).setdefault(
            job["task_type"], {"n": 0, "correct": 0, "latencies": []})
        if job["status"] == "done":
            cell["n"] += 1
            cell["correct"] += 1 if job["accepted"] else 0
            if job["latency_ms"]:
                cell["latencies"].append(job["latency_ms"])

    print("\n" + "=" * 78)
    print("PROVIDER x TASK-TYPE MATRIX   (accuracy, n, median latency)")
    print("=" * 78)
    task_types = sorted({j["task_type"] for j in result["jobs"]})
    print(f"  {'candidate':<42}" + "".join(f"{t[:11]:>12}" for t in task_types))
    for candidate in sorted(matrix):
        row = f"  {candidate:<42}"
        for task_type in task_types:
            cell = matrix[candidate].get(task_type)
            if not cell or not cell["n"]:
                row += f"{'—':>12}"
            else:
                lat = sorted(cell["latencies"])
                median = lat[len(lat) // 2] / 1000 if lat else 0
                row += f"{cell['correct']}/{cell['n']} {median:.1f}s".rjust(12)
        print(row)

    coverage = paired_coverage([(j["candidate"], j["task_id"]) for j in result["jobs"]
                                if j["status"] == "done"])
    print("\n  paired coverage:", json.dumps(coverage["candidates"]))
    if coverage["note"]:
        print("  " + coverage["note"])
    print(f"\n  {result['completed']}/{result['total']} jobs completed in {elapsed:.0f}s")
    if result["stopped_early"]:
        print(f"  STOPPED EARLY: {result['stop_reason']}")
    for key, report in health.all_health().items():
        print(f"  health {key:<40} state={report['observed_state']:<10} "
              f"p95={((report['latency_p95_ms'] or 0) / 1000):.1f}s "
              f"circuit={report['circuit']['state']}")

    out_dir = Path(args.out) / f"matrix-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {"matrix": {c: {t: {k: v for k, v in cell.items() if k != "latencies"}
                              for t, cell in by_type.items()}
                          for c, by_type in matrix.items()},
               "batch": result, "coverage": coverage,
               "health": health.all_health(), "skipped_providers": unusable,
               "elapsed_seconds": round(elapsed, 1)}
    (out_dir / "matrix_report.json").write_text(json.dumps(payload, indent=2, default=str))
    print(f"\n  report: {out_dir / 'matrix_report.json'}")
    print(f"  ledger: {args.ledger}  ({ledger.count()} inferences recorded)")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
