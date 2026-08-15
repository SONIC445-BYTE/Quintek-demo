"""
Command line interface.

    python -m benchmark.cli validate <dataset.jsonl>
    python -m benchmark.cli preflight <dataset.jsonl>
    python -m benchmark.cli demo
    python -m benchmark.cli gates
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def cmd_validate(args) -> int:
    from . import dataset as ds

    rep = ds.validate(args.dataset)
    print(f"items:  {rep.n_items}")
    print(f"hash:   {rep.dataset_hash[:32]}")
    print(f"tracks: {rep.by_track}")
    print(f"splits: {rep.by_split}")
    if rep.errors:
        print(f"\n{len(rep.errors)} ERROR(S):")
        for e in rep.errors[:25]:
            print(f"  - {e}")
        if len(rep.errors) > 25:
            print(f"  ... and {len(rep.errors) - 25} more")
    print("\nVALID" if rep.ok else "\nINVALID — dataset cannot be scored")
    return 0 if rep.ok else 1


def cmd_gates(args) -> int:
    from .gates import GateRegistry

    reg = GateRegistry(ROOT / "configs" / "gate_registry_v0_4.json")
    print(f"registry {reg.version}   calibrated={reg.is_calibrated}")
    print(f"{reg.calibration_state[:70]}...\n")
    print(f"{'GATE ID':<24}{'METRIC':<34}{'DIR':<7}{'THRESH':>9}{'MIN N':>8}  UNIT")
    for key, s in reg.tracks.items():
        print(f"{s['gate_id']:<24}{s['metric'][:33]:<34}{s['direction']:<7}"
              f"{s['threshold']:>9}{s['min_n']:>8}  {s.get('n_unit','')}")
    print(f"\nreliability gates: {len(reg.reliability)}")
    for gid, s in reg.reliability.items():
        print(f"  {gid:<38}{s['direction']:<7}{s['threshold']}")
    print(f"\nintegrity preconditions: {len(reg.integrity_checks)}")
    print(f"outcome states: {list(reg.outcome_states)}")
    return 0


def cmd_preflight(args) -> int:
    from .runner import Runner

    r = Runner(ROOT / "configs" / "v0_4.yaml", root=ROOT)
    from . import dataset as ds

    n = len(ds.load(args.dataset))
    proj = r.project_cost(n)
    print(json.dumps(proj, indent=2))
    ceiling, reason = r.review.ceiling(r.registry)
    print(f"\nmax attainable outcome: {ceiling}")
    if reason:
        print(f"reason: {reason}")
    return 0 if proj["within_budget"] else 1


def cmd_demo(args) -> int:
    """End-to-end harness demonstration on the synthetic corpus."""
    from .demo import run_demo

    run_demo()
    return 0


def cmd_serve_analytics(args) -> int:
    """Reference analytics API over runs/ -- see benchmark/analytics_api.py."""
    from .analytics_api import serve

    serve(args.runs_root, host=args.host, port=args.port, routing_log_path=args.routing_log)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="benchmark", description="PG Revision Benchmark v0.4")
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate", help="validate a dataset file")
    v.add_argument("dataset")
    v.set_defaults(func=cmd_validate)

    g = sub.add_parser("gates", help="print the gate registry")
    g.set_defaults(func=cmd_gates)

    pf = sub.add_parser("preflight", help="project cost and print outcome ceiling")
    pf.add_argument("dataset")
    pf.set_defaults(func=cmd_preflight)

    d = sub.add_parser("demo", help="run the harness end-to-end on synthetic data")
    d.set_defaults(func=cmd_demo)

    sa = sub.add_parser("serve-analytics", help="run the reference analytics JSON API")
    sa.add_argument("--runs-root", default="runs")
    sa.add_argument("--routing-log", default=None)
    sa.add_argument("--host", default="127.0.0.1")
    sa.add_argument("--port", type=int, default=8420)
    sa.set_defaults(func=cmd_serve_analytics)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
