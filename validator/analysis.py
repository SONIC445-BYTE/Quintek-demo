"""
Error analysis: what the validator got wrong, and which layer got it wrong.

A sensitivity of 0.55 tells you the validator is not usable. It does not tell
you what to change. These functions answer the two questions that do:

    FALSE NEGATIVES  which defect classes walked past, and which layer had the
                     information to catch them and did not
    FALSE POSITIVES  which clean items were flagged, by which layer, under
                     which check

Grouped by check rather than by item, because the fix is per-check. Nine clean
items rejected by one over-eager check is a prompt edit; nine rejected by nine
different checks is a validator that does not work.

THE MATCHED-PAIR STATISTIC
--------------------------
Every defective item in the development set was made by editing one clean item.
`matched_pairs` asks, for each such pair, whether the validator flagged the
defective twin AND passed the clean one. That is a stricter and more honest
statistic than sensitivity and specificity computed separately: a validator
that flags both members of every pair scores 100 per cent sensitivity and
discriminates nothing, and the pair statistic is the one that says so.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from validator import gating
from validator.devset import CLEAN, DEFECTIVE, EDGE
from validator.metrics import ABSTAINED, FLAGGED, PASSED

# A verdict a defective item should have received but did not, grouped by the
# layer that was in a position to notice. This is a COVERAGE statement: every
# configured layer ran and none of them was built to see this defect.
NOBODY_LOOKED = "no layer ran that could have seen this"

# An item that produced no verdict because a layer RAISED. Distinct from the
# above and previously conflated with it: `edge_behaviour` assigned
# NOBODY_LOOKED whenever a verdict was missing, which reported an outage --
# a layer that broke -- as a gap in what the design checks. They are opposite
# findings. One says the validator looked and has no check; the other says it
# never got to look.
OUTAGE = "outage: the item could not be validated"


def _index(verdicts):
    return {v.item_id: v for v in verdicts}


def false_positives(cases, verdicts) -> dict:
    """Clean items the validator flagged, grouped by the check that flagged them."""
    by_id = _index(verdicts)
    by_check: dict[tuple[str, str], list[dict]] = defaultdict(list)
    per_item: dict[str, list[str]] = {}
    total = 0
    for case in cases:
        if case.label != CLEAN:
            continue
        verdict = by_id.get(case.id)
        if verdict is None or verdict.verdict != FLAGGED:
            continue
        total += 1
        per_item[case.id] = [f"{layer}/{check}" for layer, check in verdict.flags]
        for layer, check in verdict.flags:
            by_check[(layer, check)].append(
                {"id": case.id, "subject": case.item.subject,
                 "detail": [d for d in verdict.detail if d.startswith(f"[{layer}")]})
    ranked = sorted(by_check.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    return {
        "count": total,
        "by_check": [{"layer": layer, "check": check, "items": len(items),
                      # Every id, not a sample. `items` counts hits and one
                      # item can be hit by several checks, so the counts sum to
                      # more than `count` and the per-check sets OVERLAP.
                      "item_ids": [i["id"] for i in items],
                      "examples": items[:3]} for (layer, check), items in ranked],
        # The union, per item, which is what the counts above cannot give.
        #
        # The D018 record stored three examples per check and nothing else, so
        # afterwards "how many of the 19 false positives would disappear if
        # these two checks were fixed" was unanswerable: 23 check-hits across
        # 19 items means at least 4 carry more than one flag, and which 4 was
        # not recoverable. The adjudication that followed had to reconstruct
        # the set from the run journal, and could only recover 16 of 17.
        #
        # An analysis that cannot say which items a fix would move is an
        # analysis that cannot price the fix.
        "items": [
            {"id": case_id, "checks": checks}
            for case_id, checks in sorted(per_item.items())
        ],
        "multi_check_items": sum(1 for checks in per_item.values() if len(checks) > 1),
        "check_hits": sum(len(checks) for checks in per_item.values()),
        "worst_check": (f"{ranked[0][0][0]}/{ranked[0][0][1]}" if ranked else ""),
        "concentrated": bool(ranked) and len(ranked[0][1]) >= max(1, total // 2),
    }


def false_negatives(cases, verdicts) -> dict:
    """Defective items the validator let through, grouped by defect class."""
    by_id = _index(verdicts)
    by_class: dict[str, list[dict]] = defaultdict(list)
    total = 0
    for case in cases:
        if case.label != DEFECTIVE:
            continue
        verdict = by_id.get(case.id)
        if verdict is None or verdict.verdict == FLAGGED:
            continue
        total += 1
        by_class[case.defect_class].append(
            {"id": case.id, "derived_from": case.derived_from,
             "mutation": case.mutation,
             # `verdict` cannot be None here: the loop above skips those.
             "verdict": verdict.verdict,
             "layers_run": list(verdict.layers_run),
             "note": case.item.defect_note})
    ranked = sorted(by_class.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    caught = Counter()
    for case in cases:
        if case.label != DEFECTIVE:
            continue
        verdict = by_id.get(case.id)
        if verdict is not None and verdict.verdict == FLAGGED:
            caught[case.defect_class] += 1
    return {
        "count": total,
        "by_defect_class": [
            {"defect_class": name, "missed": len(items),
             "caught": caught.get(name, 0),
             "examples": items[:3]} for name, items in ranked],
        "classes_never_caught": sorted(
            name for name, items in by_class.items() if not caught.get(name)),
    }


def abstentions(cases, verdicts) -> dict:
    """Where the validator declined to form a view, and on which arm."""
    by_id = _index(verdicts)
    rows = Counter()
    checks = Counter()
    for case in cases:
        verdict = by_id.get(case.id)
        if verdict is None or verdict.verdict != ABSTAINED:
            continue
        rows[case.label] += 1
        for layer, check in verdict.abstentions:
            checks[f"{layer}/{check}"] += 1
    return {"by_label": dict(sorted(rows.items())),
            "by_check": dict(sorted(checks.items(), key=lambda kv: (-kv[1], kv[0])))}


def edge_behaviour(cases, verdicts, outages) -> dict:
    """
    How the validator behaved where competent reviewers disagree.

    Nothing here is right or wrong, which is why it is reported separately from
    both arms. What it shows is calibration: a validator that passes or flags
    every edge case with no abstentions is confident about material people
    argue over, and that confidence has to come from somewhere.
    """
    by_id = _index(verdicts)
    lost = {o.get("id"): o for o in outages}
    counts = Counter()
    rows = []
    for case in cases:
        if case.label != EDGE:
            continue
        verdict = by_id.get(case.id)
        if verdict is not None:
            outcome = verdict.verdict
        elif case.id in lost:
            outcome = OUTAGE
        else:
            # No verdict and no outage record. `pipeline.run` either returns a
            # Verdict or raises, so this should be unreachable; it is reported
            # as its own thing rather than folded into either real outcome.
            outcome = "no verdict and no outage recorded"
        counts[outcome] += 1
        row = {"id": case.id, "verdict": outcome,
               "checks": [f"{lay}/{chk}" for lay, chk in (verdict.flags if verdict else ())],
               "why_edge": case.edge_reason}
        if outcome == OUTAGE:
            failure = lost[case.id]
            row["outage"] = {"layer": failure.get("layer"), "mode": failure.get("mode"),
                             "model_was_called": failure.get("model_was_called")}
        rows.append(row)
    decided = counts[FLAGGED] + counts[PASSED]
    total = sum(counts.values())
    return {"total": total, "by_verdict": dict(sorted(counts.items())),
            "abstention_rate": (round(counts[ABSTAINED] / total, 3) if total else None),
            "decided_without_hesitation": decided,
            "items": rows}


def matched_pairs(cases, verdicts) -> dict:
    """
    Per clean/defective pair: did the validator tell the twins apart?

    Four outcomes, and only one of them is the validator working:

        discriminated  flagged the defective twin, passed the clean one
        both_flagged   flagged both -- catches the defect, rejects the good item
        both_passed    flagged neither -- blind to the edit
        inverted       flagged the clean twin and passed the defective one
    """
    by_id = _index(verdicts)
    by_case = {c.id: c for c in cases}
    outcomes = Counter()
    rows = []
    for case in cases:
        if case.label != DEFECTIVE or not case.derived_from:
            continue
        twin = by_case.get(case.derived_from)
        if twin is None:
            continue
        bad, good = by_id.get(case.id), by_id.get(twin.id)
        if bad is None or good is None:
            continue
        bad_flagged = bad.verdict == FLAGGED
        good_flagged = good.verdict == FLAGGED
        if bad_flagged and not good_flagged:
            outcome = "discriminated"
        elif bad_flagged and good_flagged:
            outcome = "both_flagged"
        elif not bad_flagged and not good_flagged:
            outcome = "both_passed"
        else:
            outcome = "inverted"
        outcomes[outcome] += 1
        rows.append({"defective": case.id, "clean": twin.id,
                     "defect_class": case.defect_class, "outcome": outcome})
    total = sum(outcomes.values())
    return {"pairs": total, "by_outcome": dict(sorted(outcomes.items())),
            "discrimination_rate": (round(outcomes["discriminated"] / total, 3)
                                    if total else None),
            "items": rows}


def non_gating_findings(cases, verdicts) -> dict:
    """
    What ran, reported, and decided nothing.

    Separate from `false_positives` on purpose. These items were NOT counted
    against the validator, because the corpus has no gold that would make the
    finding right or wrong -- but the check did run and did report, and a
    record that dropped that would be hiding a measurement rather than
    declining to use it.
    """
    by_id = _index(verdicts)
    by_check: dict[str, list[dict]] = defaultdict(list)
    for case in cases:
        verdict = by_id.get(case.id)
        if verdict is None:
            continue
        for layer, check in getattr(verdict, "non_gating", ()):
            by_check[f"{layer}/{check}"].append(
                {"id": case.id, "label": case.label, "verdict": verdict.verdict})
    return {
        "count": sum(len(v) for v in by_check.values()),
        "by_check": [
            {"check": check, "items": len(items),
             "item_ids": [i["id"] for i in items],
             "by_label": dict(Counter(i["label"] for i in items)),
             "why": gating.reason(check.split("/", 1)[-1])}
            for check, items in sorted(by_check.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        ],
        "note": ("these findings were reported and excluded from both rates. Excluding "
                 "them does not make a candidate qualified -- it removes a number that "
                 "was never evidence."),
    }


def report(cases, verdicts, outages) -> dict:
    """
    `outages` is required, not optional. An analysis that does not know which
    items were lost cannot tell a coverage gap from a broken layer, and
    defaulting it to empty would restore exactly the mislabelling this
    argument exists to remove.
    """
    return {"false_positives": false_positives(cases, verdicts),
            "false_negatives": false_negatives(cases, verdicts),
            "abstentions": abstentions(cases, verdicts),
            "edge_behaviour": edge_behaviour(cases, verdicts, outages),
            "matched_pairs": matched_pairs(cases, verdicts),
            "non_gating": non_gating_findings(cases, verdicts)}


def render(data: dict) -> str:
    """The same content as prose, for a terminal and for a commit message."""
    lines = []
    fp, fn = data["false_positives"], data["false_negatives"]

    lines.append(f"FALSE POSITIVES: {fp['count']} clean item(s) flagged")
    if not fp["by_check"]:
        lines.append("  none")
    for row in fp["by_check"]:
        lines.append(f"  {row['items']:>3}  {row['layer']}/{row['check']}")
    if fp["concentrated"] and fp["worst_check"]:
        lines.append(f"  -> concentrated in {fp['worst_check']}; that check is the fix, "
                     "not the validator")

    lines.append("")
    lines.append(f"FALSE NEGATIVES: {fn['count']} defective item(s) passed")
    if not fn["by_defect_class"]:
        lines.append("  none")
    for row in fn["by_defect_class"]:
        total = row["missed"] + row["caught"]
        lines.append(f"  {row['missed']:>3}/{total}  {row['defect_class']}")
    if fn["classes_never_caught"]:
        lines.append("  -> never caught at all: " + ", ".join(fn["classes_never_caught"]))

    pairs = data["matched_pairs"]
    lines.append("")
    lines.append(f"MATCHED PAIRS: {pairs['pairs']} clean/defective pairs")
    for name, count in pairs["by_outcome"].items():
        lines.append(f"  {count:>3}  {name}")
    if pairs["discrimination_rate"] is not None:
        lines.append(f"  discrimination rate {pairs['discrimination_rate']:.0%}")

    edge = data["edge_behaviour"]
    lines.append("")
    lines.append(f"EDGE CASES: {edge['total']} items where reviewers disagree "
                 "(scored in neither arm)")
    for name, count in edge["by_verdict"].items():
        lines.append(f"  {count:>3}  {name}")
    if edge["abstention_rate"] is not None:
        lines.append(f"  abstained on {edge['abstention_rate']:.0%} of them")

    abst = data["abstentions"]
    if abst["by_check"]:
        lines.append("")
        lines.append("ABSTENTIONS across the whole set")
        for name, count in abst["by_check"].items():
            lines.append(f"  {count:>3}  {name}")
    return "\n".join(lines)
