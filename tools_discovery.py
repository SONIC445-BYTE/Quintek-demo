#!/usr/bin/env python3
"""
Find out what each provider currently offers, and reconcile it with what we
already knew.

    python3 tools_discovery.py catalogue --providers nvidia
        One cheap request per provider. Records what is listed, what is new,
        what has stopped being listed, and what metadata moved. Sets nothing
        to AVAILABLE -- a listing is not an invocation.

    python3 tools_discovery.py probe --providers nvidia [--limit N]
        A minimal completion against the models a probe could tell us
        something new about. This is where AVAILABLE and RETIRED come from.
        Spends one call per model, so it works from `due_for_recheck` rather
        than probing everything every time.

    python3 tools_discovery.py status [--provider nvidia]
        The registry as a table.

    python3 tools_discovery.py shortlist --role validation
        The role's candidates, generated from the registry rather than read
        from a committed file.

    python3 tools_discovery.py check-experiment --freeze <path>
        Whether a frozen experiment's models can still be called. Reports;
        never substitutes.

WHY THIS IS A TOOL AND NOT A DAEMON
-----------------------------------
Same reasoning as `benchmark/cli.py`: a cron entry that runs a process to
completion is easier to reason about, restart and observe than a scheduler
this repository would have to keep alive. The intervals in
`configs/discovery.json` decide what a run actually does, so running it more
often than the intervals is cheap and running it less often is the only way
to fall behind.

NOTHING HERE CHANGES AN EXPERIMENT
----------------------------------
`check-experiment` reports a frozen model that has died and exits non-zero.
It does not edit the freeze, pick a replacement, or refreeze. Production
follows the catalogue; an experiment does not, and the moment a discovery
tool can re-point a frozen manifest, every result under that manifest becomes
unreadable.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from benchmark import provider_catalogue as catalogue_mod
from benchmark.discovery import (DEFAULT_POLICY_PATH, DEFAULT_REGISTRY_PATH,
                                 Availability, DiscoveryPolicy,
                                 DynamicModelRegistry, now_iso)

SNAPSHOT_DIR = Path("discovery/snapshots")

#: Requirements per role, as declared filters. The role's members are NOT
#: listed here -- that is the whole point. `discovery/shortlists.json` spelled
#: out which models served which role, so a retirement made the file wrong and
#: nothing said so.
ROLE_REQUIREMENTS = {
    "generation": dict(required_capabilities=("structured_output",), min_context=32_000),
    "validation": dict(required_capabilities=("structured_output", "reasoning"),
                       min_context=32_000),
    "vision": dict(required_capabilities=("structured_output", "vision")),
}


def _registry(args) -> DynamicModelRegistry:
    policy = DiscoveryPolicy.load(args.policy or DEFAULT_POLICY_PATH)
    return DynamicModelRegistry(args.registry or DEFAULT_REGISTRY_PATH, policy=policy)


def _sources(names: str):
    wanted = [n.strip() for n in names.split(",") if n.strip()]
    unknown = [n for n in wanted if n not in catalogue_mod.SOURCES]
    if unknown:
        raise SystemExit(
            f"unknown provider(s) {unknown}; this repository has adapters for "
            f"{', '.join(sorted(catalogue_mod.SOURCES))}")
    return [catalogue_mod.SOURCES[n] for n in wanted]


def run_catalogue(args) -> int:
    registry = _registry(args)
    exit_code = 0
    for source in _sources(args.providers):
        result = catalogue_mod.fetch_catalogue(source)
        print(f"{source.name}: {len(result.observations)} listed"
              + (f"  ({result.latency_ms:.0f}ms)" if result.latency_ms else ""))
        if not result.ok:
            # Not reconciled. An empty list from a failed fetch would mark
            # every model this provider has ever served as absent, and two
            # such runs would retire the lot.
            print(f"  NOT RECONCILED: {result.error[:200]}", file=sys.stderr)
            exit_code = 1
            continue
        report = registry.reconcile(source.name, result.observations,
                                    source=source.catalogue_url)
        print(report.render())
        if args.snapshot:
            path = SNAPSHOT_DIR / f"{now_iso().replace(':', '-')}_{source.name}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(
                {"provider": source.name, "at": report.at,
                 "catalogue": [o.__dict__ for o in result.observations],
                 "reconciliation": report.as_dict()}, indent=2), encoding="utf-8")
            print(f"  snapshot {path}")
    registry.save()
    print(f"\nregistry {registry.path}")
    return exit_code


def run_probe(args) -> int:
    registry = _registry(args)
    due = registry.due_for_recheck()
    by_provider = {s.name: s for s in _sources(args.providers)}
    due = [r for r in due if r.provider in by_provider]
    if args.limit:
        due = due[:args.limit]
    if not due:
        print("nothing is due for a probe under the configured intervals.")
        return 0
    print(f"probing {len(due)} model(s); one minimal completion each\n")
    for record in due:
        source = by_provider[record.provider]
        result = catalogue_mod.probe(source, record.model_id)
        registry.record_probe(record.key, error=result.error,
                              http_status=result.http_status,
                              latency_ms=result.latency_ms,
                              credential_ref=source.api_key_env)
        updated = registry.get(record.key)
        print(f"  {record.key:<52} {updated.availability:<24}"
              + (f"{result.latency_ms:>8.0f}ms" if result.latency_ms else ""))
        if updated.retired:
            print(f"      RETIRED: {updated.retirement_reason[:120]}")
    registry.save()
    print(f"\nregistry {registry.path}")
    return 0


def run_status(args) -> int:
    registry = _registry(args)
    if args.json:
        print(json.dumps(registry.report(), indent=2))
        return 0
    print(registry.render())
    if args.provider:
        print()
        for record in registry.by_provider(args.provider):
            print(f"  {record.model_id:<50} {record.availability:<24}"
                  f"{record.pricing_status:<10}last_verified {record.last_verified or '—'}")
    return 0


def run_shortlist(args) -> int:
    registry = _registry(args)
    if args.role not in ROLE_REQUIREMENTS:
        raise SystemExit(f"unknown role {args.role!r}; "
                         f"known: {', '.join(sorted(ROLE_REQUIREMENTS))}")
    requirements = dict(ROLE_REQUIREMENTS[args.role])
    if args.max_input_price is not None:
        requirements["max_input_price"] = args.max_input_price
    selected = registry.shortlist(limit=args.limit, **requirements)
    _kept, dropped = registry.eligible(**requirements)
    print(f"{args.role}: {len(selected)} candidate(s) from {len(registry.all())} known "
          f"model(s)\n")
    for record in selected:
        print(f"  {record.key:<52} {record.pricing_status:<9}"
              f"ctx {record.context_window or '—'}")
    if args.explain:
        print(f"\nrejected ({len(dropped)}):")
        for row in dropped:
            print(f"  {row['key']:<52} {'; '.join(row['reasons'])[:110]}")
    return 0


def run_check_experiment(args) -> int:
    """
    Does this frozen experiment still have callable models?

    Exit 2 when it does not, so a CI step or a run script stops rather than
    proceeding to spend a budget on an arm that cannot complete.
    """
    registry = _registry(args)
    raw = json.loads(Path(args.freeze).read_text(encoding="utf-8"))
    models = raw.get("models") or []
    blocked = registry.blocked_experiment_models(models)
    digest = str(raw.get("digest", ""))[:12]
    print(f"freeze {digest}  {len(models)} model(s)\n")
    for entry in models:
        key = f"{entry.get('provider')}:{entry.get('model')}"
        record = registry.get(key)
        state = record.availability if record else "NOT IN REGISTRY"
        print(f"  {entry.get('seat', ''):<10}{key:<52}{state}")
    if not blocked:
        print("\nEXPERIMENT EXECUTABLE: every frozen model is callable or unprobed.")
        return 0
    print("\nEXPERIMENT BLOCKED")
    for row in blocked:
        print(f"  {row['key']}  {row['availability']}"
              f"{'  (terminal)' if row['terminal'] else ''}")
        if row["detail"]:
            print(f"      {row['detail'][:160]}")
    print("\nThis tool will not choose a replacement. A frozen configuration is what "
          "makes the runs in a set comparable, so substituting a model silently would "
          "make every number under this digest unreadable. Establishing a new pairing "
          "is an explicit decision, recorded with --refreeze and a --note saying what "
          "changed and why.")
    return 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--registry", default="", help="path to the model registry JSON")
    parser.add_argument("--policy", default="", help="path to discovery intervals JSON")
    sub = parser.add_subparsers(dest="mode", required=True)

    cat = sub.add_parser("catalogue")
    cat.add_argument("--providers", default="nvidia")
    cat.add_argument("--snapshot", action="store_true",
                     help="also write a timestamped raw snapshot")

    prb = sub.add_parser("probe")
    prb.add_argument("--providers", default="nvidia")
    prb.add_argument("--limit", type=int, default=0,
                     help="probe at most this many models this run")

    sts = sub.add_parser("status")
    sts.add_argument("--provider", default="")
    sts.add_argument("--json", action="store_true")

    shl = sub.add_parser("shortlist")
    shl.add_argument("--role", default="validation")
    shl.add_argument("--limit", type=int, default=30)
    shl.add_argument("--max-input-price", type=float, default=None)
    shl.add_argument("--explain", action="store_true")

    chk = sub.add_parser("check-experiment")
    chk.add_argument("--freeze", required=True)

    args = parser.parse_args(argv)
    return {"catalogue": run_catalogue, "probe": run_probe, "status": run_status,
            "shortlist": run_shortlist,
            "check-experiment": run_check_experiment}[args.mode](args)


if __name__ == "__main__":
    raise SystemExit(main())
