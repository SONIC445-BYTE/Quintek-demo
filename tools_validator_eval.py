#!/usr/bin/env python3
"""
Run a validator against a labelled set and report what it actually did.

    python3 tools_validator_eval.py ceiling
        Run the design against ground-truth oracles. Reports the best this
        validator could possibly do if every layer were flawless. Costs nothing
        and is never valid for the gate.

    python3 tools_validator_eval.py run --provider <name> [--judge <name>]
        Run the design against real providers from the provider registry.

    python3 tools_validator_eval.py layers
        Run each layer alone and in combination against oracles, so the
        contribution of each is visible instead of inferred. Still a ceiling.

    python3 tools_validator_eval.py experiments --provider <name> --judge <name>
        The three measurements that separate "can the validator work" from
        "which model should do it", run in order and recorded:

            1  A+B+D   everything except the free-answer judge
            2  C       the judge alone
            3  A+B+C+D the whole validator

        Only Layer A is deterministic; B and D are model calls constrained to
        quote their evidence. Experiment 1 is not "the deterministic layers",
        it is "the validator without the layer whose failure mode is
        agreeing with itself".

Every mode prints the confusion matrix, the two-armed gate, and the error
analysis. A run that used an oracle prints INVALID FOR GATING at the top and
the gate outcome is withheld -- not footnoted, withheld -- because a ceiling
figure quoted as a result is exactly how a validator nobody measured gets
described as validated.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from validator import analysis, metrics, pipeline, runs, scripted
from validator.devset import CLEAN, DEFECTIVE, load
from validator.conformance import ConformanceUnavailable
from validator.grounding import GroundingUnavailable
from validator.judge import JudgeUnavailable

ARM_LABEL = {CLEAN: metrics.CLEAN, DEFECTIVE: metrics.DEFECTIVE}


def evaluate(cases, *, grounding_provider, judge_provider, config,
             conformance_provider=None):
    """Run the pipeline over every case. An outage is recorded, never smoothed."""
    verdicts, outages = [], []
    for case in cases:
        try:
            verdicts.append(pipeline.run(case.item.as_dict(),
                                         grounding_provider=grounding_provider,
                                         judge_provider=judge_provider,
                                         conformance_provider=conformance_provider,
                                         config=config))
        except (GroundingUnavailable, JudgeUnavailable, ConformanceUnavailable) as exc:
            outages.append({"id": case.id, "error": str(exc)})
    return verdicts, outages


def score(cases, verdicts):
    by_id = {v.item_id: v for v in verdicts}
    labels, said = [], []
    for case in cases:
        if not case.in_arm:
            continue
        verdict = by_id.get(case.id)
        if verdict is None:
            continue
        labels.append(ARM_LABEL[case.label])
        said.append(verdict.verdict)
    return metrics.confusion(labels, said)


def render(title, cases, verdicts, outages, *, oracle_used, config):
    matrix = score(cases, verdicts)
    gate = metrics.gate(matrix)
    out = [f"== {title}  ({config.label()})", ""]
    if oracle_used:
        out += ["INVALID FOR GATING",
                "  This run used a ground-truth oracle in place of a model. The numbers below",
                "  describe the design's ceiling, not any validator's performance.", ""]
    if outages:
        out += [f"OUTAGES: {len(outages)} item(s) could not be validated",
                *(f"  {o['id']}: {o['error'][:140]}" for o in outages[:5]), ""]
    out += [f"decided in arms: {matrix.decided} of {matrix.total} "
            f"({matrix.abstained_defective + matrix.abstained_clean} abstained)",
            f"  sensitivity  {_pct(matrix.sensitivity)}  ci {_ci(matrix.sensitivity_ci)}",
            f"  specificity  {_pct(matrix.specificity)}  ci {_ci(matrix.specificity_ci)}",
            f"  false flags  {_pct(matrix.false_flag_rate)}", ""]
    if oracle_used:
        out += ["gate: withheld (oracle run)", ""]
    else:
        out += [f"gate: {gate.outcome}", *(f"  - {r}" for r in gate.reasons), ""]
    out += [analysis.render(analysis.report(cases, verdicts))]
    return "\n".join(x for x in out if x is not None)


def _pct(value):
    return "   n/a" if value is None else f"{value:6.1%}"


def _ci(bounds):
    return "n/a" if not bounds else f"({bounds[0]:.0%}, {bounds[1]:.0%})"


def run_ceiling(args):
    if "holdout" in str(args.corpus) and not args.spend_a_look:
        print("refusing: a ceiling run reads the holdout and reports what the design misses "
              "on it. Nothing is scored, but the result reaches whoever is building the "
              "validator, and a design changed in response to it has been tuned on the "
              "holdout. Pass --spend-a-look to proceed; it is recorded in the ledger.",
              file=sys.stderr)
        return 2
    devset = load(args.corpus)
    if "holdout" in str(args.corpus):
        from validator import holdout as holdout_mod
        holdout_mod.note_inspection(f"ceiling run: {args.spend_a_look}",
                                    root=args.corpus)
    ground, judge_provider, conform = scripted.oracle(devset.cases)
    config = pipeline.Config()
    verdicts, outages = evaluate(devset.cases, grounding_provider=ground,
                                 judge_provider=judge_provider,
                                 conformance_provider=conform, config=config)
    print(render("CEILING: every layer flawless", devset.cases, verdicts, outages,
                 oracle_used=True, config=config))
    print()
    uncovered = scripted.UNCOVERED_BY_DESIGN
    print("Defect classes this design has no check for, by construction: "
          + (", ".join(uncovered) if uncovered else "none"))
    print("  (v0.1 could not see: " + ", ".join(scripted.UNCOVERED_BY_DESIGN_V0_1) + ")")
    path = _record(devset, verdicts, outages, config,
                   [runs.describe_provider(role, prov) for role, prov in
                    (("grounding", ground), ("judge", judge_provider),
                     ("conformance", conform))],
                   kind=runs.KIND_CEILING,
                   note="ground-truth oracles; measures the design, not a validator")
    print(f"recorded as a CEILING run in {path}")
    _write(args, devset, verdicts, outages, config, oracle_used=True)
    return 0


def run_layers(args):
    devset = load(args.corpus)
    ground, judge_provider, conform = scripted.oracle(devset.cases)
    off = dict(structural=False, grounding=False, judge=False, conformance=False)
    combos = [pipeline.Config(**{**off, "structural": True}),
              pipeline.Config(**{**off, "grounding": True}),
              pipeline.Config(**{**off, "judge": True}),
              pipeline.Config(**{**off, "conformance": True}),
              pipeline.Config(**{**off, "structural": True, "grounding": True}),
              pipeline.Config(**{**off, "structural": True, "grounding": True, "judge": True}),
              pipeline.Config()]
    print("Each layer's contribution, measured against ground-truth oracles.")
    print("INVALID FOR GATING: these are ceilings, not results.\n")
    print(f"{'layers':<16}{'sens':>8}{'spec':>8}{'pairs':>8}  {'notes'}")
    for config in combos:
        verdicts, outages = evaluate(devset.cases, grounding_provider=ground,
                                     judge_provider=judge_provider,
                                     conformance_provider=conform, config=config)
        matrix = score(devset.cases, verdicts)
        pairs = analysis.matched_pairs(devset.cases, verdicts)
        rate = pairs["discrimination_rate"]
        note = f"{len(outages)} outage(s)" if outages else ""
        print(f"{config.label():<16}{_pct(matrix.sensitivity):>8}"
              f"{_pct(matrix.specificity):>8}"
              f"{(f'{rate:6.1%}' if rate is not None else '   n/a'):>8}  {note}")
    return 0


def run_real(args):
    from benchmark.providers.registry import build_provider
    devset = load(args.corpus)
    ground = build_provider(args.provider)
    judge_provider = build_provider(args.judge or args.provider)
    conform = ground
    if getattr(ground, "model", None) == getattr(judge_provider, "model", None):
        print("refusing to run: the grounding layer and the judge are the same model. "
              "A second opinion from the same weights is the first opinion again.",
              file=sys.stderr)
        return 2
    config = pipeline.Config()
    verdicts, outages = evaluate(devset.cases, grounding_provider=ground,
                                 judge_provider=judge_provider,
                                 conformance_provider=conform, config=config)
    used_oracle = bool(getattr(ground, "is_oracle", False)
                       or getattr(judge_provider, "is_oracle", False))
    print(render(f"RUN: {ground.model} + judge {judge_provider.model}",
                 devset.cases, verdicts, outages, oracle_used=used_oracle, config=config))
    kind = runs.KIND_CEILING if used_oracle else (
        runs.KIND_HOLDOUT if "holdout" in str(devset.root) else runs.KIND_DEVELOPMENT)
    path = _record(devset, verdicts, outages, config,
                   [runs.describe_provider("grounding", ground),
                    runs.describe_provider("judge", judge_provider),
                    runs.describe_provider("conformance", conform)],
                   kind=kind, note=args.note)
    print(f"recorded as a {kind.upper()} run in {path}")
    _write(args, devset, verdicts, outages, config, oracle_used=used_oracle)
    return 0


def _record(devset, verdicts, outages, config, providers, *, kind, note=""):
    """Write the run to the record. A ceiling and a measurement are different kinds."""
    from validator.holdout import corpus_hash
    matrix = score(devset.cases, verdicts)
    gate = metrics.gate(matrix)
    run = runs.Run(
        at=runs.now(), kind=kind, corpus=str(devset.root),
        corpus_hash=corpus_hash(devset.root),
        validator_version=pipeline.VALIDATOR_VERSION, config=config.label(),
        providers=providers, counts=matrix.as_dict()["counts"],
        sensitivity=matrix.sensitivity, specificity=matrix.specificity,
        gate=("withheld (oracle run)" if kind == runs.KIND_CEILING else gate.outcome),
        outages=len(outages), analysis=analysis.report(devset.cases, verdicts), note=note)
    return runs.record(run)


def _write(args, devset, verdicts, outages, config, *, oracle_used):
    if not args.out:
        return
    matrix = score(devset.cases, verdicts)
    gate = metrics.gate(matrix)
    payload = {
        "corpus": str(devset.root), "summary": devset.summary(),
        "validator": config.label(), "oracle_used": oracle_used,
        "confusion": matrix.as_dict() if hasattr(matrix, "as_dict") else {},
        "gate": None if oracle_used else {"outcome": gate.outcome,
                                          "reasons": list(gate.reasons)},
        "outages": outages,
        "analysis": analysis.report(devset.cases, verdicts),
        "verdicts": [v.as_dict() for v in verdicts],
    }
    Path(args.out).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten to {args.out}")


EXPERIMENTS = (
    ("1  A+B+D  everything except the free-answer judge",
     dict(structural=True, grounding=True, judge=False, conformance=True)),
    ("2  C      the judge alone",
     dict(structural=False, grounding=False, judge=True, conformance=False)),
    ("3  ABCD   the whole validator",
     dict(structural=True, grounding=True, judge=True, conformance=True)),
)


def run_experiments(args):
    """
    The three measurements, in order, against real providers.

    Run together and recorded together because the number that matters is the
    difference between them. "Every layer earns its place" was asserted from a
    ceiling; this is where it is either confirmed or withdrawn.
    """
    from benchmark.providers.registry import build_provider
    devset = load(args.corpus)
    ground = build_provider(args.provider)
    judge_provider = build_provider(args.judge or args.provider)
    if getattr(ground, "model", None) == getattr(judge_provider, "model", None):
        print("refusing to run: the judge is the same model as the grounding layer. "
              "A second opinion from the same weights is the first opinion again.",
              file=sys.stderr)
        return 2
    conform = ground
    records = [runs.describe_provider("grounding", ground),
               runs.describe_provider("judge", judge_provider),
               runs.describe_provider("conformance", conform)]

    rows = []
    for title, flags in EXPERIMENTS:
        config = pipeline.Config(**flags)
        verdicts, outages = evaluate(devset.cases, grounding_provider=ground,
                                     judge_provider=judge_provider,
                                     conformance_provider=conform, config=config)
        matrix = score(devset.cases, verdicts)
        gate = metrics.gate(matrix)
        pairs = analysis.matched_pairs(devset.cases, verdicts)
        _record(devset, verdicts, outages, config, records,
                kind=(runs.KIND_CEILING if any(p.is_oracle for p in records)
                      else runs.KIND_DEVELOPMENT),
                note=f"experiment {title.split()[0]}: {args.note}".strip())
        rows.append((title, matrix, gate, pairs, outages))
        print(render(title, devset.cases, verdicts, outages,
                     oracle_used=any(p.is_oracle for p in records), config=config))
        print()

    print("=" * 72)
    print(f"{'experiment':<46}{'sens':>8}{'spec':>8}{'gate':>10}")
    for title, matrix, gate, _pairs, _out in rows:
        print(f"{title:<46}{_pct(matrix.sensitivity):>8}{_pct(matrix.specificity):>8}"
              f"{gate.outcome:>10}")
    print()
    print("The claim to check here is the third row against the first two. If A+B+D "
          "alone reaches the gate, the judge is cost without contribution; if C alone "
          "reaches it, the other three are. Neither is known until this has run.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--corpus", default="corpus/validator_dev")
    parser.add_argument("--out", default="")
    sub = parser.add_subparsers(dest="mode", required=True)
    ceiling = sub.add_parser("ceiling")
    ceiling.add_argument("--spend-a-look", default="",
                         help="reason for reading the holdout; recorded in its ledger")
    layers = sub.add_parser("layers")
    layers.add_argument("--spend-a-look", default="")
    real = sub.add_parser("run")
    real.add_argument("--provider", required=True)
    real.add_argument("--judge", default="")
    real.add_argument("--note", default="")

    experiments = sub.add_parser("experiments")
    experiments.add_argument("--provider", required=True)
    experiments.add_argument("--judge", default="")
    experiments.add_argument("--note", default="")
    args = parser.parse_args(argv)
    return {"ceiling": run_ceiling, "layers": run_layers, "run": run_real,
            "experiments": run_experiments}[args.mode](args)


if __name__ == "__main__":
    raise SystemExit(main())
