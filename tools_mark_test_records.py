#!/usr/bin/env python3
"""
Mark records the test suite wrote into a shared execution log.

RETAIN AND MARK, NOT PURGE. These records are evidence of a real contamination
path, and the fix that closes it should be checkable against them. Deleting the
evidence would make "is it fixed?" unanswerable.

WHAT IS MARKED, AND WHY THAT IS DEFENSIBLE
------------------------------------------
Only records matching a signature no real call can produce. The contaminating
set is 229 records that are byte-identical apart from `execution_id` and
`timestamp`:

    provider       openrouter                      (a REAL registered provider)
    model          inclusionai/ling-2.6-flash
    candidate_id   cand_x
    latency_ms     12.0        <- identical across all 229
    input_tokens   524         <- identical
    output_tokens  137         <- identical

A remote model does not answer in 12.0 ms, and it certainly does not answer in
exactly 12.0 ms with exactly 524 input tokens two hundred times. The constancy
is the evidence, not the provider name -- `openrouter` is genuine, which is
precisely why a name check never caught this.

The five `ex0`..`ex4` seed fixtures are marked as `fixture` on the same basis:
sequential ids, one shared timestamp, round token counts.

Records that merely LOOK synthetic are not marked. The 44 `nvidia` records from
2026-08-20 have latencies from 945 ms to 217 s, all distinct: those are real
calls and are left alone.

    python3 tools_mark_test_records.py executions.jsonl            # dry run
    python3 tools_mark_test_records.py executions.jsonl --apply
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

# Each rule: (origin to stamp, human reason, predicate over the raw record).
# Deliberately narrow. A rule that might catch a real call does not belong here.
RULES = [
    (
        "test-harness",
        "scripted double naming itself after a real provider; constant latency and tokens",
        lambda r: (r.get("provider") == "openrouter"
                   and r.get("candidate_id") == "cand_x"
                   and r.get("latency_ms") == 12.0
                   and r.get("input_tokens") == 524
                   and r.get("output_tokens") == 137),
    ),
    (
        "fixture",
        "seeded demo rows: sequential ids, one shared timestamp, round token counts",
        lambda r: (r.get("execution_id", "") in {"ex0", "ex1", "ex2", "ex3", "ex4"}
                   and r.get("input_tokens") == 1200
                   and r.get("output_tokens") == 400),
    ),
]


def unregistered_rule():
    """
    Records naming a provider with no builder in the codebase.

    OFF BY DEFAULT, behind --include-unregistered. 610 records in the current
    log name `scripted-test-harness`, which is not a registered provider and
    would now be refused at the write boundary. They are almost certainly a
    harness, and they are also measurement history: relabelling them in bulk is
    a decision about evidence, not a cleanup, so it is opt-in and stays that
    way.
    """
    from benchmark.providers.registry import available

    known = set(available())
    return (
        "unregistered-provider",
        "no builder is registered under this provider name",
        lambda r: bool(r.get("provider")) and r.get("provider") not in known,
    )


def classify(rec: dict, extra=()) -> tuple[str, str] | None:
    for origin, reason, matches in (*RULES, *extra):
        if matches(rec):
            return origin, reason
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log")
    ap.add_argument("--apply", action="store_true",
                    help="write the marks (default is a dry run that changes nothing)")
    ap.add_argument("--include-unregistered", action="store_true",
                    help="also mark records naming a provider with no builder in the "
                         "codebase. Off by default: it relabels measurement history.")
    args = ap.parse_args()

    path = Path(args.log)
    if not path.exists():
        print(f"{path}: no such file", file=sys.stderr)
        return 2

    extra = (unregistered_rule(),) if args.include_unregistered else ()
    lines = [l for l in path.read_text().splitlines() if l.strip()]
    out: list[str] = []
    counts: dict[str, int] = {}
    already = 0

    for line in lines:
        rec = json.loads(line)
        verdict = classify(rec, extra)
        if verdict is None:
            out.append(json.dumps(rec))
            continue
        origin, reason = verdict
        if rec.get("origin") == origin:
            already += 1
            out.append(json.dumps(rec))
            continue
        rec["origin"] = origin
        rec["origin_reason"] = reason
        counts[origin] = counts.get(origin, 0) + 1
        out.append(json.dumps(rec))

    total_marked = sum(counts.values())
    print(f"{path}: {len(lines)} record(s)")
    for origin, n in sorted(counts.items()):
        print(f"  to mark {origin:14} {n}")
    if already:
        print(f"  already marked          {already}")
    if not total_marked:
        print("Nothing to mark.")
        return 0

    if not args.apply:
        print(f"\nDry run: {total_marked} record(s) would be marked, none deleted.")
        print("Re-run with --apply to write them.")
        return 0

    backup = path.with_suffix(path.suffix + ".before-marking")
    shutil.copy2(path, backup)
    path.write_text("\n".join(out) + "\n")
    print(f"\nMarked {total_marked} record(s). None deleted. Backup: {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
