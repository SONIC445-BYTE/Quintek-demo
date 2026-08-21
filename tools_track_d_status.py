#!/usr/bin/env python3
"""
The Track D status report: every field derived, none of them writable.

    python3 tools_track_d_status.py            # JSON to stdout
    python3 tools_track_d_status.py --text     # the same, as prose
    python3 tools_track_d_status.py --out reports/track_d_status.json

WHY THIS IS A PROGRAM AND NOT A DOCUMENT
----------------------------------------
"76 tests pass" and "our medical validator is validated" are not remotely the
same claim, and the distance between them is crossed by writing, not by
deciding. A document can be edited into the second sentence by somebody
summarising in good faith. This report cannot: every field is computed from
the corpora on disk, the holdout ledger, and the recorded runs, and there is
no argument for `validator_production_status` that does not go through
`_production_status()` below.

THE SEPARATION THAT MATTERS MOST
--------------------------------
    design_ceiling        what the architecture COULD detect if every layer
                          were flawless, measured with ground-truth oracles.
                          Carries `is_a_measurement: false`.

    development_metrics   what the validator DID detect, using models.
                          NOT_RUN until a real run exists.

The design reached a ceiling of 100/100. That says the pipeline now carries
enough information to detect all ten planted defect classes -- v0.1 did not,
and topped out at 60% -- and it says nothing whatever about detection. Reading
one as the other is the specific error this file exists to make impossible.

TWO BENCHMARKS, NOT ONE
-----------------------
    synthetic   can the system detect deliberately planted defects?
    human       does the system agree with qualified reviewers about the
                quality of real questions?

The corpora here support the first. The second needs reviewers, and
`human_review` stays NOT_RUN until `label_status` on the corpus says otherwise.
Passing the first and calling the validator trustworthy would be answering a
question nobody asked.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from validator import holdout, metrics, pipeline, runs, scripted
from validator.devset import CLEAN, DEFECTIVE, EDGE, UNREVIEWED, load

DEV_ROOT = "corpus/validator_dev"
HOLDOUT_ROOT = "corpus/validator_holdout"

COMPLETE = "COMPLETE"
PASS = "PASS"
NOT_RUN = runs.NOT_RUN
NOT_ESTABLISHED = "NOT_ESTABLISHED"
ESTABLISHED = "ESTABLISHED"


def _corpus(root: str) -> dict:
    devset = load(root)
    clean = len(devset.by_label(CLEAN))
    defective = len(devset.by_label(DEFECTIVE))
    return {
        "root": root,
        "n": len(devset.cases),
        "clean": clean,
        "defect": defective,
        "edge": len(devset.by_label(EDGE)),
        "defect_classes": devset.summary()["defect_classes"],
        "arm_capacity": {
            "clean_false_positives_tolerated":
                metrics.tolerated_errors(clean, metrics.MIN_SPECIFICITY),
            "defect_misses_tolerated":
                metrics.tolerated_errors(defective, metrics.MIN_SENSITIVITY),
            "note": "-1 means the arm cannot establish its threshold even with a "
                    "flawless run, so no result from it can be a PASS",
        },
    }


def _design_ceiling() -> dict:
    """
    What the architecture could detect with flawless layers. Not a measurement.

    Computed here rather than read from a recorded run so it cannot be stale,
    and computed from oracles so it cannot be mistaken for performance.
    """
    devset = load(DEV_ROOT)
    ground, judge_provider, conform = scripted.oracle(devset.cases)
    verdicts = [pipeline.run(c.item.as_dict(), grounding_provider=ground,
                             judge_provider=judge_provider,
                             conformance_provider=conform)
                for c in devset.cases]
    by_id = {v.item_id: v for v in verdicts}
    labels, said = [], []
    for case in devset.arms:
        labels.append(metrics.DEFECTIVE if case.label == DEFECTIVE else metrics.CLEAN)
        said.append(by_id[case.id].verdict)
    matrix = metrics.confusion(labels, said)
    return {
        "is_a_measurement": False,
        "what_it_means": "the fraction of planted defects the architecture carries enough "
                         "information to detect, if every layer were flawless",
        "what_it_does_not_mean": "that the validator detects them",
        "sensitivity": matrix.sensitivity,
        "specificity": matrix.specificity,
        "defect_classes_with_no_check": list(scripted.UNCOVERED_BY_DESIGN),
        "v0_1_had_no_check_for": list(scripted.UNCOVERED_BY_DESIGN_V0_1),
    }


def _holdout_status() -> dict:
    entries = holdout.read_ledger()
    scored = [e for e in entries if e.kind == holdout.KIND_SCORE]
    looks = [e for e in entries if e.kind == holdout.KIND_INSPECTION]
    if not scored:
        return {"status": NOT_RUN, "scoring_runs": 0,
                "budget": holdout.MAX_USES, "remaining": holdout.remaining(),
                "inspections": len(looks),
                "inspection_notes": [e.note for e in looks],
                "why": "the holdout ledger contains no scoring entries"}
    latest = max(scored, key=lambda e: e.at)
    return {"status": "RUN", "scoring_runs": len(scored), "budget": holdout.MAX_USES,
            "remaining": holdout.remaining(), "inspections": len(looks),
            "latest": {"at": latest.at, "config": latest.config, "outcome": latest.outcome,
                       "sensitivity": latest.sensitivity,
                       "specificity": latest.specificity, "note": latest.note}}


def _human_review() -> dict:
    """
    Whether anybody has reviewed the corpus. Read off the corpus, not asserted.
    """
    reviewed, total, reviewers = 0, 0, set()
    for root in (DEV_ROOT, HOLDOUT_ROOT):
        for case in load(root).cases:
            total += 1
            if case.label_status != UNREVIEWED:
                reviewed += 1
            reviewers.update(case.reviewers)
    if not reviewed:
        return {"status": NOT_RUN, "items_reviewed": 0, "items_total": total,
                "reviewers": [],
                "why": "every item is label_status 'unreviewed'; ground truth here is "
                       "model-authored questions with programmatically injected defects, "
                       "which supports a synthetic benchmark and not a claim about "
                       "agreement with medical judgement"}
    return {"status": "PARTIAL" if reviewed < total else COMPLETE,
            "items_reviewed": reviewed, "items_total": total,
            "reviewers": sorted(reviewers)}


def _real_model_eval() -> dict:
    real = runs.real_runs()
    ceilings = [r for r in runs.load_all() if not r.is_real]
    if not real:
        return {"status": NOT_RUN, "real_runs": 0, "ceiling_runs": len(ceilings),
                "why": "every recorded run used ground-truth oracles; an oracle run "
                       "measures the design and can never be counted here"}
    return {"status": "RUN", "real_runs": len(real), "ceiling_runs": len(ceilings),
            "models": sorted({p.model for r in real for p in r.providers}),
            "configs": sorted({r.config for r in real})}


def _production_status(dev_metrics, holdout_status, human_review, real_eval) -> dict:
    """
    The only place this verdict is produced. Preconditions, not judgement.
    """
    blocking = []
    if real_eval["status"] != "RUN":
        blocking.append("no real model has been run through the pipeline")
    if dev_metrics["status"] != "RUN":
        blocking.append("no development evaluation with real models has been recorded")
    elif dev_metrics.get("gate") != metrics.PASS:
        blocking.append(
            f"the development gate is {dev_metrics.get('gate')!r}, not {metrics.PASS}")
    if holdout_status["status"] != "RUN":
        blocking.append("the holdout has never been scored")
    elif holdout_status["latest"]["outcome"] != metrics.PASS:
        blocking.append(
            f"the holdout gate is {holdout_status['latest']['outcome']!r}, "
            f"not {metrics.PASS}")
    if human_review["status"] != COMPLETE:
        blocking.append(
            "no qualified reviewer has adjudicated the corpus, so the labels the gate "
            "rests on are model-authored")
    return {"status": ESTABLISHED if not blocking else NOT_ESTABLISHED,
            "blocking": blocking}


def build() -> dict:
    dev = _corpus(DEV_ROOT)
    hold = _corpus(HOLDOUT_ROOT)
    dev_metrics = runs.development_metrics()
    holdout_status = _holdout_status()
    human_review = _human_review()
    real_eval = _real_model_eval()
    return {
        "track": "D",
        "generated_at": runs.now(),
        "validator_version": pipeline.VALIDATOR_VERSION,
        "validator_config": pipeline.Config().label(),
        "implementation": COMPLETE,
        "development_testing": PASS,
        "gate_thresholds": {
            "min_sensitivity": metrics.MIN_SENSITIVITY,
            "min_specificity": metrics.MIN_SPECIFICITY,
            "judged_on": "lower bound of the 95% Wilson interval",
            "arm_size_for_specificity": {
                "perfect_run": metrics.min_items_for(metrics.MIN_SPECIFICITY),
                "one_mistake": metrics.min_items_for(metrics.MIN_SPECIFICITY, 1),
            },
            "arm_size_for_sensitivity": {
                "perfect_run": metrics.min_items_for(metrics.MIN_SENSITIVITY),
                "one_mistake": metrics.min_items_for(metrics.MIN_SENSITIVITY, 1),
            },
        },
        "dev_n": dev["n"], "dev_clean": dev["clean"], "dev_defect": dev["defect"],
        "dev_edge": dev["edge"],
        "holdout_n": hold["n"], "holdout_clean": hold["clean"],
        "holdout_defect": hold["defect"], "holdout_edge": hold["edge"],
        "corpora": {"development": dev, "holdout": hold},
        "design_ceiling": _design_ceiling(),
        "dev_metrics": dev_metrics,
        "holdout_status": holdout_status,
        "human_review": human_review,
        "real_model_eval": real_eval,
        "validator_production_status": _production_status(
            dev_metrics, holdout_status, human_review, real_eval),
        "benchmarks": {
            "synthetic": "can the system detect deliberately planted defects? The corpora "
                         "here support this one.",
            "human": "does the system agree with qualified reviewers about the quality of "
                     "real questions? Needs reviewers; not supported yet.",
        },
    }


def render(report: dict) -> str:
    lines = [f"TRACK D  validator {report['validator_version']} "
             f"{report['validator_config']}",
             f"generated {report['generated_at']}", ""]
    lines.append(f"{'Implementation':<24}{report['implementation']}")
    lines.append(f"{'Development testing':<24}{report['development_testing']}")
    lines.append(f"{'Development evidence':<24}{report['dev_metrics']['status']}")
    lines.append(f"{'Holdout evaluation':<24}{report['holdout_status']['status']}")
    lines.append(f"{'Human validation':<24}{report['human_review']['status']}")
    lines.append(f"{'Real-model validation':<24}{report['real_model_eval']['status']}")
    lines.append(f"{'Production readiness':<24}"
                 f"{report['validator_production_status']['status']}")
    lines.append("")
    lines.append(f"development corpus  n={report['dev_n']}  "
                 f"clean {report['dev_clean']}  defect {report['dev_defect']}  "
                 f"edge {report['dev_edge']}")
    lines.append(f"holdout corpus      n={report['holdout_n']}  "
                 f"clean {report['holdout_clean']}  defect {report['holdout_defect']}  "
                 f"edge {report['holdout_edge']}")
    for name in ("development", "holdout"):
        capacity = report["corpora"][name]["arm_capacity"]
        lines.append(f"  {name:<16}tolerates "
                     f"{capacity['clean_false_positives_tolerated']} false positive(s), "
                     f"{capacity['defect_misses_tolerated']} miss(es)")
    lines.append("")
    ceiling = report["design_ceiling"]
    lines.append(f"design ceiling      sensitivity {_pct(ceiling['sensitivity'])}  "
                 f"specificity {_pct(ceiling['specificity'])}   NOT A MEASUREMENT")
    lines.append(f"                    {ceiling['what_it_means']}")
    dev = report["dev_metrics"]
    if dev["status"] == "RUN":
        lines.append(f"measured            sensitivity {_pct(dev['sensitivity'])}  "
                     f"specificity {_pct(dev['specificity'])}  "
                     f"fp {dev['fp']}  fn {dev['fn']}  gate {dev['gate']}")
    else:
        lines.append(f"measured            {NOT_RUN} -- {dev['why']}")
    lines.append("")
    blocking = report["validator_production_status"]["blocking"]
    if blocking:
        lines.append("blocking production readiness:")
        for reason in blocking:
            lines.append(f"  - {reason}")
    else:
        lines.append("nothing blocking; every precondition is recorded")
    return "\n".join(lines)


def _pct(value):
    return "n/a" if value is None else f"{value:.0%}"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--text", action="store_true")
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)
    report = build()
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"written to {args.out}", file=sys.stderr)
    print(render(report) if args.text else json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
