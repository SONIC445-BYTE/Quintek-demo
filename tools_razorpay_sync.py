#!/usr/bin/env python3
"""
Create Quintek's plans on Razorpay and record the ids -- one way round.

    RAZORPAY_KEY_ID=... RAZORPAY_KEY_SECRET=... python3 tools_razorpay_sync.py check
    python3 tools_razorpay_sync.py status
    python3 tools_razorpay_sync.py sync            # dry run, prints what it would do
    python3 tools_razorpay_sync.py sync --apply

Razorpay collects money. It is not Quintek's pricing database: allowances,
daily caps, rollover and compute weights live in `plans`, and the gateway's id
is an attribute of a Quintek plan rather than the other way round. That is why
this tool only ever writes `gateway_plan_id` back, and never reads a price out
of Razorpay.

Credentials come from the environment and are never printed. `check` reports
their SHAPE -- present, length, test or live, stray whitespace -- because every
common misconfiguration is diagnosable without revealing the value.

`sync` is idempotent and refuses to relink a plan that already has an id. Two
gateway plans for one Quintek plan means some subscribers are billed against a
record nobody is watching, and the only way that surfaces is a complaint.
"""

from __future__ import annotations

import argparse
import json
import sys

from billing.db import connect
from billing.gateway import GatewayError
from billing.gateway_http import (ENV_KEY_ID, ENV_KEY_SECRET, OK, HttpTransport,
                                  adapter_from_env, credentials_from_env,
                                  diagnose)
from billing.money import Money
from billing.plans import PlanStore

GATEWAY = "razorpay"

# Razorpay's vocabulary for a billing period, mapped from Quintek's. Kept here
# rather than in the adapter because it is specific to plan CREATION, and the
# adapter's job is subscriptions.
PERIOD = {"monthly": "monthly", "annual": "yearly"}


def open_store(db_path: str) -> tuple[PlanStore, object]:
    conn = connect(db_path)
    store = PlanStore(conn)
    if not conn.execute("SELECT 1 FROM plans LIMIT 1").fetchone():
        store.seed_from_config()
    return store, conn


def plan_payload(plan) -> dict:
    """The Razorpay plan body for one Quintek plan."""
    period = PERIOD.get(plan.billing_interval)
    if period is None:
        raise ValueError(f"{plan.id}: {plan.billing_interval} has no gateway period")
    return {
        "period": period,
        "interval": 1,
        "item": {
            "name": f"Quintek {plan.name} ({plan.billing_interval})",
            # Minor units, as Razorpay expects and as Quintek stores them. No
            # conversion happens here, because a conversion is a place to lose
            # a factor of a hundred.
            "amount": plan.price_minor,
            "currency": plan.currency,
            "description": (f"{plan.monthly_question_allowance} questions per month,"
                            f" up to {plan.daily_question_limit} per day"),
        },
        "notes": {"quintek_plan_id": plan.id, "quintek_family": plan.family},
    }


def cmd_check(args) -> int:
    report = credentials_from_env()
    print("RAZORPAY CREDENTIALS (shape only -- no value is printed)")
    for key, value in report.items():
        print(f"  {key:<28} {value}")
    if report["mode"] == "live":
        print("\n  These are LIVE credentials. This tool creates real plans"
              " against a real account.")
    if not (report["key_id_present"] and report["key_secret_present"]):
        print(f"\n  Nothing to test: set {ENV_KEY_ID} and {ENV_KEY_SECRET}.")
        return 1
    if report["whitespace_in_raw_values"]:
        print("\n  NOTE: a value carried surrounding whitespace. It is stripped"
              " before use, but a trailing newline from a copy-paste is a"
              " frequent cause of a 401.")

    adapter = adapter_from_env(transport=HttpTransport())
    result = diagnose(adapter)

    print("\nAUTHENTICATION  (control endpoint: does the key work at all?)")
    print(f"  credentials_valid       {result['credentials_valid']}"
          f"   [{result['control']}]")
    print("\nSUBSCRIPTIONS   (the API this integration actually needs)")
    print(f"  subscriptions_enabled   {result['subscriptions_enabled']}"
          f"   [{result['subscriptions']}]")

    if result["verdict"] == OK:
        print("\n  Ready. `sync --apply` can create the plans.")
        return 0

    print(f"\n  VERDICT: {result['verdict']}")
    print(f"  {result['remedy']}")
    if result["subscriptions_detail"]:
        print(f"\n  Razorpay said: {result['subscriptions_detail']}")
    return 2


def cmd_status(args) -> int:
    store, _ = open_store(args.db)
    print(f"{'plan':<22}{'price':>10}{'interval':>10}   gateway plan id")
    for plan in store.all_active():
        gateway, ref = store.gateway_ref(plan.id)
        marker = ref or ("—" if plan.price_minor == 0 else "NOT LINKED")
        print(f"{plan.id:<22}{Money(plan.price_minor, plan.currency).format():>10}"
              f"{plan.billing_interval:>10}   {marker}")
    unlinked = store.unlinked()
    if unlinked:
        print(f"\n{len(unlinked)} paid plan(s) have no gateway id."
              " Run `sync --apply` with credentials configured.")
    return 0


def cmd_sync(args) -> int:
    store, conn = open_store(args.db)
    pending = store.unlinked()
    if not pending:
        print("Every active paid plan already has a gateway id. Nothing to do.")
        return 0

    adapter = adapter_from_env(transport=HttpTransport()) if args.apply else None
    if args.apply and adapter is None:
        print(f"Refusing to apply: no credentials. Set {ENV_KEY_ID}"
              f" and {ENV_KEY_SECRET}.")
        return 1

    failures = 0
    for plan in pending:
        payload = plan_payload(plan)
        if not args.apply:
            print(f"WOULD CREATE  {plan.id}")
            print("    " + json.dumps(payload))
            continue
        try:
            response = adapter._call("POST", "/v1/plans", payload)
        except GatewayError as exc:
            print(f"FAILED  {plan.id}: {exc}")
            failures += 1
            continue
        gateway_plan_id = response.get("id", "")
        if not gateway_plan_id:
            print(f"FAILED  {plan.id}: Razorpay returned no plan id: {response}")
            failures += 1
            continue
        # Write the link back IMMEDIATELY, before creating the next plan. A
        # crash between creating and recording leaves an orphan plan on the
        # gateway that the next run would duplicate.
        store.set_gateway_ref(plan.id, GATEWAY, gateway_plan_id)
        print(f"CREATED {plan.id} -> {gateway_plan_id}")

    if not args.apply:
        print(f"\nDry run. {len(pending)} plan(s) would be created."
              " Re-run with --apply.")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="billing.db")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="report credential shape and test authentication")
    sub.add_parser("status", help="which plans are linked to the gateway")
    syncer = sub.add_parser("sync", help="create the missing gateway plans")
    syncer.add_argument("--apply", action="store_true",
                        help="actually create them (default is a dry run)")

    args = parser.parse_args()
    return {"check": cmd_check, "status": cmd_status, "sync": cmd_sync}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
