#!/usr/bin/env python3
"""
Answer the question the pricing page cannot: given ₹X of revenue, what is the
maximum AI compute Quintek can safely consume?

    python3 tools_compute_budget.py                 # per-plan ceilings
    python3 tools_compute_budget.py --revenue 100000 --paying 250
    python3 tools_compute_budget.py --json

Everything printed is derived from files in this repository:

    configs/plans.json              the prices and allowances actually shipped
    configs/compute_profile.json    the token profile, MEASURED from a real run
    discovery/shortlists.json       model prices, OBSERVED from the providers

Where a number was assumed rather than measured, the output says so on the
line where it is used. A forecast whose assumptions are invisible is not a
forecast, it is a wish.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from billing.budget import BudgetPolicy, compute_budget, plan_budget, runway_days
from billing.economics import FeeModel
from billing.costs import ModelPrice
from billing.money import MICRO, Money, micro_to_money, token_cost_micro
from billing.plans import PlanStore

PROFILE = Path("configs/compute_profile.json")
SHORTLISTS = Path("discovery/shortlists.json")


def load_profile(path: Path = PROFILE) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def policy_from(profile: dict) -> BudgetPolicy:
    p = profile["policy"]
    return BudgetPolicy(
        gst_bps=p["gst_bps"], price_includes_gst=p["price_includes_gst"],
        fees=FeeModel(percent_bps=p["gateway_percent_bps"],
                      gst_on_fee_bps=p["gateway_gst_on_fee_bps"]),
        refund_reserve_bps=p["refund_reserve_bps"],
        fixed_costs_minor=profile.get("fixed_costs_minor_per_month", 0),
        target_contribution_bps=p["target_contribution_bps"],
        target_contribution_minor=p["target_contribution_minor"],
        cover_free_tier=p["cover_free_tier"])


def price_per_million_micro(usd_per_million: float, usd_to_inr_paise: int) -> int:
    """
    USD per million tokens -> INR micro-minor units per million tokens.

    Delegated to `ModelPrice.from_usd_per_million` rather than reimplemented.
    Writing this conversion a second time is how it acquires a factor of a
    hundred, which is precisely what happened before this line existed: the
    budget reported six billion affordable questions and the error was
    invisible because every figure downstream stayed internally consistent.
    """
    price = ModelPrice.from_usd_per_million(
        "", "", usd_per_million, usd_per_million, usd_to_inr_paise=usd_to_inr_paise)
    return price.input_per_million_micro


def cost_per_accepted_micro(profile: dict, gen_in_usd: float, gen_out_usd: float,
                            val_in_usd: float, val_out_usd: float) -> int:
    """
    What one ACCEPTED question costs, in micro minor units.

    Generation plus validation, divided by the acceptance rate -- because the
    rejected attempts were paid for too, and the price of a plan has to cover
    them.
    """
    fx = profile["fx"]["usd_to_inr_paise"]
    tp = profile["token_profile"]
    gen, val = tp["generation"], tp["validation"]

    produced_micro = (
        token_cost_micro(gen["input_tokens"], price_per_million_micro(gen_in_usd, fx))
        + token_cost_micro(gen["output_tokens"], price_per_million_micro(gen_out_usd, fx))
        + token_cost_micro(val["input_tokens"], price_per_million_micro(val_in_usd, fx))
        + token_cost_micro(val["output_tokens"], price_per_million_micro(val_out_usd, fx)))

    accepted_per_100 = profile["acceptance"]["accepted_per_100_produced"]
    if accepted_per_100 <= 0:
        return 0
    # Round up: work paid for and thrown away is still paid for.
    return -(-produced_micro * 100 // accepted_per_100)


def candidate_costs(profile: dict, path: Path = SHORTLISTS, *,
                    paid_only: bool = False) -> list[dict]:
    """
    Cost per accepted question for each shortlisted generator, validated by the
    cheapest shortlisted validator that is NOT the same model.

    The validator must differ from the generator; a model marking its own work
    is not a check, so the pairing is enforced here rather than assumed.

    `paid_only` excludes zero-priced models from BOTH sides. Excluding them
    from one side only produces the worst of both worlds: a pairing that looks
    priced, costs almost nothing, and quietly depends on a free tier's rate
    limit to validate every question the product sells.
    """
    if not path.exists():
        return []
    lists = json.loads(path.read_text(encoding="utf-8"))

    def priced(models):
        return [m for m in models
                if not paid_only or m["price_in_per_m"] or m["price_out_per_m"]]

    validators = sorted(priced(lists.get("validation", [])),
                        key=lambda m: (m["price_in_per_m"], m["price_out_per_m"]))
    out = []
    for gen in priced(lists.get("generation", [])):
        partner = next((v for v in validators if v["model_id"] != gen["model_id"]), None)
        if partner is None:
            continue
        micro = cost_per_accepted_micro(
            profile, gen["price_in_per_m"], gen["price_out_per_m"],
            partner["price_in_per_m"], partner["price_out_per_m"])
        out.append({
            "generator": gen["model_id"], "validator": partner["model_id"],
            "cost_per_accepted_micro": micro,
            "cost_per_accepted_display": micro_to_money(micro).format(),
            "cost_per_500_display": micro_to_money(micro * 500).format(),
        })
    return sorted(out, key=lambda r: r["cost_per_accepted_micro"])


def fine(micro: int) -> str:
    """
    Format an amount that is smaller than a paise.

    Per-question costs live in thousandths of a paise, and `Money.format`
    rounds every one of them to ₹0.00 -- which makes a model that costs eight
    times another look identical to it. Integer formatting only; a float here
    would defeat the point of the whole money module.
    """
    if micro >= 100 * MICRO:          # a rupee or more: normal formatting
        return micro_to_money(micro).format()
    thousandths = micro // 1_000
    return f"{thousandths // 1_000}.{thousandths % 1_000:03d}p"


def seeded_plans() -> PlanStore:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(Path("billing/schema.sql").read_text(encoding="utf-8"))
    store = PlanStore(conn)
    store.seed_from_config()
    return store


def plan_table(profile: dict) -> list[dict]:
    policy = policy_from(profile)
    rows = []
    for plan in seeded_plans().all_active():
        if plan.price_minor <= 0:
            continue
        budget = plan_budget(plan, policy)
        rows.append(budget.as_dict())
    return rows


def portfolio(profile: dict, revenue_major: int, *, paying_users: int,
              allowance_per_user: int = 5_000, period_days: int = 30) -> dict:
    """
    The headline answer, for a stated monthly RECOGNISED revenue.

    Free-tier commitment is scaled off the paying-user count using the assumed
    ratio in the profile, and is labelled as assumed wherever it appears.
    """
    policy = policy_from(profile)
    revenue = Money(revenue_major * 100)

    ft = profile["free_tier"]
    free_users = paying_users * ft["assumed_free_users_per_paying_user"]
    free_questions = free_users * ft["assumed_questions_consumed_per_free_user"]

    # Deliberately NOT the cheapest overall. The cheapest overall is always a
    # `:free` tier, and a free tier is a rate limit with marketing attached --
    # it cannot carry a paid product's load, and budgeting against ₹0 would
    # make every plan look infinitely profitable.
    cheapest = candidate_costs(profile, paid_only=True)
    unit = cheapest[0]["cost_per_accepted_micro"] if cheapest else None

    free_commitment = (micro_to_money(free_questions * unit)
                       if unit is not None else None)

    budget = compute_budget(revenue, policy,
                            paid_liability=Money.zero(),
                            free_commitment=free_commitment,
                            period_days=period_days)

    buys = budget.ceiling_micro // unit if unit else None

    # The inverse, and the number the router actually needs: Quintek has SOLD
    # this many questions this month, so what may one of them cost?
    committed = paying_users * allowance_per_user + free_questions
    verdict = budget.verdict(unit, committed)

    return {
        "revenue_display": revenue.format(),
        "paying_users": paying_users,
        "assumed_free_users": free_users,
        "assumed_free_questions": free_questions,
        "budget": budget.as_dict(),
        "cheapest_pairing": cheapest[0] if cheapest else None,
        "questions_affordable_this_month": buys,
        "committed_questions": committed,
        "allowance_per_user_assumed": allowance_per_user,
        "verdict": verdict,
        "note": ("paid liability is set to zero here because the input is a"
                 " revenue figure rather than a live database; use"
                 " BudgetService against a real database for the true number"),
    }


def _print_report(profile: dict, revenue_major: int | None, paying: int,
                  allowance: int = 5_000) -> None:
    tp = profile["token_profile"]
    acc = profile["acceptance"]
    print("QUINTEK COMPUTE BUDGET")
    print("=" * 78)
    print("\nINPUTS")
    print(f"  token profile   MEASURED  {tp['generation']['input_tokens']} in /"
          f" {tp['generation']['output_tokens']} out  (generation)")
    print(f"                            {tp['validation']['input_tokens']} in /"
          f" {tp['validation']['output_tokens']} out  (validation)")
    print(f"                            source: {tp['source']}")
    print(f"  acceptance      ASSUMED   {acc['accepted_per_100_produced']}"
          " of every 100 produced questions are accepted")
    print(f"  fx              ASSUMED   ₹{profile['fx']['usd_to_inr_paise'] / 100:.2f}"
          " per USD")

    print("\nCOST PER ACCEPTED QUESTION  (observed provider prices)")
    print("-" * 78)
    rows = candidate_costs(profile)
    if not rows:
        print("  no shortlist available -- run the discovery pass first")
    free = [r for r in rows if r["cost_per_accepted_micro"] == 0]
    paid = candidate_costs(profile, paid_only=True)
    for row in paid[:8]:
        print(f"  {fine(row['cost_per_accepted_micro']):>10}  per accepted   "
              f"{micro_to_money(row['cost_per_accepted_micro'] * 500).format():>12}"
              f" per 500   {row['generator'][:38]}")
    if free:
        print(f"\n  ({len(free)} confirmed candidates are priced at zero -- free"
              " tiers. They are excluded from")
        print("   the budget below: a free tier is a rate limit, not a supply"
              " Quintek can plan on.)")

    print("\nWHAT EACH PLAN CAN AFFORD PER QUESTION")
    print("-" * 78)
    paid_pairings = candidate_costs(profile, paid_only=True)
    cheapest_paid = paid_pairings[0] if paid_pairings else None
    print(f"  {'plan':<20}{'revenue/mo':>12}{'ceiling':>12}{'allowance':>11}"
          f"{'max/question':>14}{'actual':>10}{'':>3}")
    for row in plan_table(profile):
        allowed = row["max_cost_per_accepted_micro"]
        actual = cheapest_paid["cost_per_accepted_micro"] if cheapest_paid else None
        if allowed is None or actual is None:
            mark, shown = "?", "—"
        else:
            mark = "OK" if actual * 100 <= allowed * 85 else (
                "TIGHT" if actual <= allowed else "OVER")
            shown = fine(actual)
        print(f"  {row['plan_id']:<20}{row['monthly_revenue_display']:>12}"
              f"{row['compute_ceiling_display']:>12}"
              f"{row['monthly_allowance']:>11}"
              f"{fine(allowed) if allowed is not None else '—':>14}"
              f"{shown:>10}  {mark}")
    if cheapest_paid:
        print(f"\n  'actual' is the cheapest CONFIRMED paid pairing:"
              f" {cheapest_paid['generator']}")
        print(f"  validated by {cheapest_paid['validator']}")

    if revenue_major is not None:
        result = portfolio(profile, revenue_major, paying_users=paying,
                           allowance_per_user=allowance)
        budget = result["budget"]
        print(f"\nGIVEN {result['revenue_display']} OF RECOGNISED MONTHLY REVENUE")
        print("-" * 78)
        for d in budget["deductions"]:
            print(f"  -{d['amount_display']:>12}   {d['name']:<28} ({d['basis']})")
        print(f"  ={budget['ceiling_display']:>12}   MAXIMUM PROVIDER SPEND"
              f"   ({budget['per_day_display']}/day)")
        if result["questions_affordable_this_month"] is not None:
            print(f"\n  That buys {result['questions_affordable_this_month']:,}"
                  " accepted questions at the cheapest confirmed paid pairing")
            print(f"  ({result['cheapest_pairing']['generator']})")

        verdict = result["verdict"]
        allowed = verdict["allowed_per_question_micro"]
        print("\n  THE ROUTER'S CEILING")
        print(f"  {result['paying_users']:,} paying users x"
              f" {result['allowance_per_user_assumed']:,} questions +"
              f" {result['assumed_free_questions']:,} free")
        print(f"  = {result['committed_questions']:,} questions sold this month")
        if allowed is None:
            print("  -> no ceiling can be derived: nothing is committed")
        else:
            print(f"  -> spend at most {fine(allowed)} per ACCEPTED question")
            print(f"     cheapest confirmed pairing costs"
                  f" {fine(verdict['measured_per_question_micro'])}"
                  f"  [{verdict['verdict']}]")
            if verdict["headroom_bps"] is not None:
                print(f"     headroom: {verdict['headroom_bps'] // 100}%")
        for warning in budget["warnings"]:
            print(f"\n  WARNING: {warning}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revenue", type=int, default=None,
                        help="recognised monthly revenue in rupees")
    parser.add_argument("--paying", type=int, default=100,
                        help="paying users, used to scale the free-tier commitment")
    parser.add_argument("--allowance", type=int, default=5_000,
                        help="questions per paying user per month (plan mix stand-in)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    profile = load_profile()
    if args.json:
        payload = {"plans": plan_table(profile),
                   "candidates": candidate_costs(profile),
                   "paid_candidates": candidate_costs(profile, paid_only=True)}
        if args.revenue is not None:
            payload["portfolio"] = portfolio(profile, args.revenue,
                                             paying_users=args.paying,
                                             allowance_per_user=args.allowance)
        print(json.dumps(payload, indent=2))
        return
    _print_report(profile, args.revenue, args.paying, args.allowance)


if __name__ == "__main__":
    main()
