"""
The ablation: what each layer contributed, and what the run is not allowed to say.

TWO CONCLUSIONS, NOT ONE
------------------------
An ablation run against one model produces two answers, and merging them is the
mistake that makes the whole exercise worthless:

    THE EXPERIMENT CONCLUSION   Does the independent judge add information the
                                other layers do not already have? Answered by
                                the difference between A+B+D and A+B+C+D.

    THE MODEL CONCLUSION        Is this particular model fit to be the judge?
                                NOT answered here, and a poor absolute score
                                does not answer it either -- a judge that
                                contributes +14 points of sensitivity while
                                being individually mediocre has answered the
                                experiment question in the affirmative.

`report()` returns them as separate keys and `render()` prints them under
separate headings, with the model conclusion stated as deferred rather than
omitted, because an omitted conclusion is one a reader supplies themselves.

COMPARABILITY
-------------
A run with outages, or one that did not reach every item, is INCOMPLETE. An
incomplete run is reported with its numbers and excluded from every delta,
because subtracting a partial run from a complete one produces a difference
that is mostly the missing items. That is the specific trap waiting for the
70B, which stalled before its control arm finished last time.
"""

from __future__ import annotations

from dataclasses import dataclass, field

COMPLETE = "COMPLETE"
INCOMPLETE = "INCOMPLETE"
NOT_COMPARABLE = "INCOMPLETE -- NOT COMPARABLE"


@dataclass
class Arm:
    """One experiment in the set."""
    name: str
    layers: str
    matrix: object
    outages: int = 0
    items_expected: int = 0
    items_decided: int = 0
    edge_abstention: float | None = None
    analysis: dict = field(default_factory=dict)

    @property
    def completeness(self) -> str:
        if self.outages == 0 and self.items_decided >= self.items_expected:
            return COMPLETE
        return INCOMPLETE

    @property
    def comparable(self) -> bool:
        return self.completeness == COMPLETE

    def as_dict(self) -> dict:
        return {"name": self.name, "layers": self.layers,
                "sensitivity": self.matrix.sensitivity,
                "specificity": self.matrix.specificity,
                "false_positive": self.matrix.false_positive,
                "false_negative": self.matrix.false_negative,
                "abstained": self.matrix.abstained_defective + self.matrix.abstained_clean,
                "outages": self.outages,
                "items_expected": self.items_expected,
                "items_decided": self.items_decided,
                "edge_abstention_rate": self.edge_abstention,
                "completeness": self.completeness}


def _delta(after, before):
    if after is None or before is None:
        return None
    return round(after - before, 4)


def contribution(with_layer: Arm, without_layer: Arm, *, layer: str) -> dict:
    """
    What adding `layer` did, or why the question cannot be answered from these runs.
    """
    if not (with_layer.comparable and without_layer.comparable):
        incomplete = [a.name for a in (with_layer, without_layer) if not a.comparable]
        return {"layer": layer, "status": NOT_COMPARABLE,
                "incomplete_runs": incomplete,
                "why": "a run that did not reach every item cannot be subtracted from one "
                       "that did; the difference would be mostly the missing items"}
    return {
        "layer": layer, "status": COMPLETE,
        "delta_sensitivity": _delta(with_layer.matrix.sensitivity,
                                    without_layer.matrix.sensitivity),
        "delta_specificity": _delta(with_layer.matrix.specificity,
                                    without_layer.matrix.specificity),
        "delta_false_positive": (with_layer.matrix.false_positive
                                 - without_layer.matrix.false_positive),
        "delta_false_negative": (with_layer.matrix.false_negative
                                 - without_layer.matrix.false_negative),
        "with": with_layer.name, "without": without_layer.name,
    }


def report(arms: list[Arm], *, model: str = "") -> dict:
    by_layers = {a.layers: a for a in arms}
    full, no_judge, judge_only = (by_layers.get("ABCD"), by_layers.get("ABD"),
                                  by_layers.get("C"))
    judge_contribution = (contribution(full, no_judge, layer="C")
                          if full and no_judge else None)

    experiment = {"question": "does the independent judge add information the other "
                              "layers do not already have?",
                  "answer": "not determined"}
    if judge_contribution and judge_contribution["status"] == COMPLETE:
        delta = judge_contribution["delta_sensitivity"]
        cost = judge_contribution["delta_false_positive"]
        if delta is None:
            experiment["answer"] = "not determined: sensitivity was undefined in one arm"
        elif delta > 0 and cost <= 0:
            experiment["answer"] = (
                f"yes: adding the judge caught {delta:.0%} more of the planted defects "
                f"and did not increase false positives")
        elif delta > 0:
            experiment["answer"] = (
                f"yes, at a price: adding the judge caught {delta:.0%} more of the planted "
                f"defects and flagged {cost} more clean item(s)")
        elif delta == 0:
            experiment["answer"] = (
                "no: the judge changed nothing the other layers had not already caught, "
                "so on this evidence it is cost without contribution")
        else:
            experiment["answer"] = (
                f"worse than nothing on this evidence: adding the judge caught {abs(delta):.0%} "
                "FEWER defects, which means it is overriding a correct verdict from another "
                "layer")
    elif judge_contribution:
        experiment["answer"] = (
            "not determined: " + judge_contribution["why"])

    return {
        "model": model,
        "arms": [a.as_dict() for a in arms],
        "judge_contribution": judge_contribution,
        "judge_alone": judge_only.as_dict() if judge_only else None,
        "experiment_conclusion": experiment,
        "model_conclusion": {
            "question": f"is {model or 'this model'} fit to serve as the judge?",
            "answer": "DEFERRED",
            "why": "one model measured against one development corpus does not settle "
                   "model selection. That needs the same frozen configuration run against "
                   "the alternative, compared on sensitivity, specificity, false positives, "
                   "false negatives, latency, cost and edge calibration -- and a poor "
                   "absolute score here does not by itself disqualify a model whose "
                   "contribution to the ablation was positive.",
        },
        "comparable": all(a.comparable for a in arms),
    }


def render(data: dict) -> str:
    lines = [f"ABLATION  {data['model'] or 'unnamed model'}", ""]
    lines.append(f"{'experiment':<24}{'sens':>8}{'spec':>8}{'FP':>5}{'FN':>5}"
                 f"{'abst':>6}{'out':>5}  {'status'}")
    for arm in data["arms"]:
        lines.append(
            f"{arm['name']:<24}{_pct(arm['sensitivity']):>8}{_pct(arm['specificity']):>8}"
            f"{arm['false_positive']:>5}{arm['false_negative']:>5}{arm['abstained']:>6}"
            f"{arm['outages']:>5}  {arm['completeness']}")
    lines.append("")

    contrib = data.get("judge_contribution")
    lines.append("INCREMENTAL CONTRIBUTION OF THE INDEPENDENT JUDGE (C)")
    if not contrib:
        lines.append("  not computed: the run set is missing an arm")
    elif contrib["status"] != COMPLETE:
        lines.append(f"  {contrib['status']}: {contrib['why']}")
        lines.append(f"  incomplete: {', '.join(contrib['incomplete_runs'])}")
    else:
        lines.append(f"  delta sensitivity   {_signed_pct(contrib['delta_sensitivity'])}")
        lines.append(f"  delta specificity   {_signed_pct(contrib['delta_specificity'])}")
        lines.append(f"  delta false positive{contrib['delta_false_positive']:+d}")
        lines.append(f"  delta false negative{contrib['delta_false_negative']:+d}")
    lines.append("")
    lines.append("EXPERIMENT CONCLUSION")
    lines.append(f"  {data['experiment_conclusion']['question']}")
    lines.append(f"  -> {data['experiment_conclusion']['answer']}")
    lines.append("")
    lines.append("MODEL CONCLUSION")
    lines.append(f"  {data['model_conclusion']['question']}")
    lines.append(f"  -> {data['model_conclusion']['answer']}")
    lines.append(f"     {data['model_conclusion']['why']}")
    return "\n".join(lines)


def _pct(value):
    return "n/a" if value is None else f"{value:.0%}"


def _signed_pct(value):
    return "n/a" if value is None else f"{value:+.0%}"
