"""
What the experiment set will cost, computed exactly, before anything is spent.

WHY THIS IS EXACT AND NOT AN ESTIMATE
-------------------------------------
Layer A is deterministic and free, so how many items it stops before any model
is consulted is knowable in advance by running it. Every other layer makes a
fixed number of requests per item it receives. The planned call count is
therefore arithmetic, not a guess, and it is worth having on screen before a
key is exported rather than discovered halfway through a bill.

PLANNED IS NOT THE SAME AS SPENDABLE
------------------------------------
The frozen configuration includes a retry policy. A logical call that fails
twice and succeeds on the third attempt sends three requests. So the forecast
reports two numbers that differ by a factor of `1 + max_retries`:

    PLANNED           what the measurement requires
    MAX SPENDABLE     what can leave the machine if every call retries fully

A budget compared against the planned figure looks comfortable and is not. The
verdict below compares against the spendable figure, because that is the one
that can actually be exhausted, and it is counted in the same unit the meter
counts -- outbound attempts.
"""

from __future__ import annotations

from benchmark.corpus import QUESTION_TYPES
from validator import structural
from validator.budget import SEAT_CANDIDATE, SEAT_JUDGE

WITHIN = "WITHIN BUDGET"
WILL_EXCEED = "WILL EXCEED THE CONFIGURED BUDGET"
NO_BUDGET = "NO BUDGET SET"
IMPOSSIBLE = "BUDGET TOO SMALL FOR THE MEASUREMENT"

# Requests each layer makes per item it is handed. Grounding asks twice: once
# about the key, once about the explanation, deliberately separated so an
# answer to one cannot contaminate the other.
CALLS_GROUNDING_KEY = 1
CALLS_GROUNDING_EXPLANATION = 1
CALLS_JUDGE = 1
CALLS_CONFORMANCE = 1

# Which seat pays for each layer.
SEAT_OF_LAYER = {"grounding": SEAT_CANDIDATE, "conformance": SEAT_CANDIDATE,
                 "judge": SEAT_JUDGE}


def _survivors(devset, *, require_source: bool, require_reference: bool) -> int:
    """Items that get past Layer A. Free to compute; no model is consulted."""
    return sum(
        1 for case in devset.cases
        if structural.check(case.item.as_dict(), question_types=QUESTION_TYPES,
                            require_source=require_source,
                            require_verifiable_reference=require_reference).ok)


def plan(devset, experiments, *, max_retries: int = 2,
         max_calls: int | None = None, max_judge_calls: int | None = None,
         check_explanation: bool = True) -> dict:
    """
    `experiments` is a sequence of (name, layers, config-flag dict).
    """
    total_items = len(devset.cases)
    survivors = _survivors(devset, require_source=True, require_reference=True)
    grounding_calls = CALLS_GROUNDING_KEY + (
        CALLS_GROUNDING_EXPLANATION if check_explanation else 0)

    rows = []
    planned = {SEAT_CANDIDATE: 0, SEAT_JUDGE: 0}
    for name, layers, flags in experiments:
        # Layer A only gates the layers that follow it when it is switched on.
        reaching = survivors if flags.get("structural") else total_items
        by_layer = {"structural": 0}
        if flags.get("grounding"):
            by_layer["grounding"] = reaching * grounding_calls
        if flags.get("judge"):
            by_layer["judge"] = reaching * CALLS_JUDGE
        if flags.get("conformance"):
            by_layer["conformance"] = reaching * CALLS_CONFORMANCE
        seats = {SEAT_CANDIDATE: 0, SEAT_JUDGE: 0}
        for layer, count in by_layer.items():
            if layer in SEAT_OF_LAYER:
                seats[SEAT_OF_LAYER[layer]] += count
        for seat, count in seats.items():
            planned[seat] += count
        rows.append({"name": name, "layers": layers, "items_reaching_model_layers": reaching,
                     "by_layer": by_layer, "candidate": seats[SEAT_CANDIDATE],
                     "judge": seats[SEAT_JUDGE],
                     "total": seats[SEAT_CANDIDATE] + seats[SEAT_JUDGE]})

    attempts = 1 + max(0, int(max_retries))
    spendable = {seat: count * attempts for seat, count in planned.items()}
    spendable["total"] = sum(spendable.values())
    planned_total = planned[SEAT_CANDIDATE] + planned[SEAT_JUDGE]

    # A budget below the PLANNED count cannot complete the set even if every
    # call succeeds first try. That is different from a budget below the
    # worst case, which merely might stop early, and the two deserve different
    # answers: refuse the first, warn about the second.
    impossible = []
    if max_calls is not None and planned_total > max_calls:
        impossible.append(
            f"total: the measurement needs {planned_total} calls and the budget is "
            f"{max_calls}. This set cannot complete even with no retries at all.")
    if max_judge_calls is not None and planned[SEAT_JUDGE] > max_judge_calls:
        impossible.append(
            f"judge: the measurement needs {planned[SEAT_JUDGE]} judge calls and the "
            f"budget is {max_judge_calls}. The judged arms cannot complete.")

    exceeds = []
    if max_calls is not None and spendable["total"] > max_calls:
        exceeds.append(
            f"total: up to {spendable['total']} outbound attempts against a budget of "
            f"{max_calls}")
    if max_judge_calls is not None and spendable[SEAT_JUDGE] > max_judge_calls:
        exceeds.append(
            f"judge: up to {spendable[SEAT_JUDGE]} outbound attempts against a budget of "
            f"{max_judge_calls}")
    if max_calls is None and max_judge_calls is None:
        verdict = NO_BUDGET
    else:
        verdict = IMPOSSIBLE if impossible else (WILL_EXCEED if exceeds else WITHIN)

    return {
        "corpus": str(devset.root), "items": total_items,
        "stopped_by_layer_a": total_items - survivors,
        "reach_model_layers": survivors,
        "calls_per_item": {"grounding": grounding_calls, "judge": CALLS_JUDGE,
                           "conformance": CALLS_CONFORMANCE, "structural": 0},
        "experiments": rows,
        "planned": {**planned, "total": planned_total},
        "retry": {"max_retries": max_retries, "attempts_per_logical_call": attempts,
                  "note": "the retry policy is part of the frozen configuration, so this "
                          "multiplier is fixed for the whole experiment set"},
        "max_spendable": spendable,
        "budget": {"max_calls": max_calls, "max_judge_calls": max_judge_calls,
                   "unit": "outbound attempts"},
        "verdict": verdict, "exceeds": exceeds, "impossible": impossible,
    }


def render(data: dict) -> str:
    lines = ["EXPERIMENT FORECAST", ""]
    lines.append(f"corpus                         {data['corpus']}")
    lines.append(f"items                          {data['items']}")
    lines.append(f"stopped by Layer A, free       {data['stopped_by_layer_a']}")
    lines.append(f"reaching the model layers      {data['reach_model_layers']}")
    lines.append("")
    for row in data["experiments"]:
        lines.append(f"{row['name']}")
        # Layer names and seat names collide on the word "judge", so the seat
        # lines say so. A reader who mistakes one for the other reads the
        # 70B's exposure off the wrong row.
        for layer in ("structural", "grounding", "judge", "conformance"):
            if layer in row["by_layer"]:
                lines.append(f"  layer {layer:<22}{row['by_layer'][layer]:>7}")
        lines.append(f"  {'-> candidate seat':<28}{row['candidate']:>7}")
        lines.append(f"  {'-> judge seat':<28}{row['judge']:>7}")
        lines.append(f"  {'-> total':<28}{row['total']:>7}")
        lines.append("")
    planned = data["planned"]
    lines.append(f"{'TOTAL PLANNED (logical calls)':<32}{planned['total']:>7}")
    lines.append(f"  {'candidate':<30}{planned['candidate']:>7}")
    lines.append(f"  {'judge':<30}{planned['judge']:>7}")
    lines.append("")
    retry = data["retry"]
    lines.append(f"frozen retry policy: max_retries={retry['max_retries']}, so up to "
                 f"{retry['attempts_per_logical_call']} outbound attempts per logical call")
    spend = data["max_spendable"]
    lines.append(f"{'MAXIMUM SPENDABLE (attempts)':<32}{spend['total']:>7}")
    lines.append(f"  {'candidate':<30}{spend['candidate']:>7}")
    lines.append(f"  {'judge':<30}{spend['judge']:>7}")
    lines.append("")
    budget = data["budget"]
    lines.append("CONFIGURED HARD BUDGET (outbound attempts)")
    lines.append(f"  {'--max-calls':<30}"
                 f"{budget['max_calls'] if budget['max_calls'] is not None else 'not set':>7}")
    lines.append(f"  {'--max-judge-calls':<30}"
                 f"{budget['max_judge_calls'] if budget['max_judge_calls'] is not None else 'not set':>7}")
    lines.append("")
    lines.append(f"RESULT: {data['verdict']}")
    for reason in data.get("impossible", []):
        lines.append(f"  - {reason}")
    for reason in data["exceeds"]:
        lines.append(f"  - {reason}")
    if data["verdict"] == WILL_EXCEED:
        lines.append("  The measurement fits; the worst case does not. The run may stop "
                     "early if retries fire, and a stopped arm is INCOMPLETE with no "
                     "delta -- not a lower score.")
    if data["verdict"] == NO_BUDGET:
        lines.append("  Nothing will stop this run early. Set --max-calls and "
                     "--max-judge-calls if that is not what you want.")
    return "\n".join(lines)
