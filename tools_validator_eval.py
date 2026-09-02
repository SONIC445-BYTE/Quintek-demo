#!/usr/bin/env python3
"""
Run a validator against a labelled set and report what it actually did.

    python3 tools_validator_eval.py ceiling
        Run the design against ground-truth oracles. Reports the best this
        validator could possibly do if every layer were flawless. Costs nothing
        and is never valid for the gate.

    python3 tools_validator_eval.py run --candidate <spec> --judge <spec>
        Run the design once against real models.

    python3 tools_validator_eval.py layers
        Run each layer alone and in combination against oracles, so the
        contribution of each is visible instead of inferred. Still a ceiling.

    python3 tools_validator_eval.py forecast
        What the experiment set will cost, computed exactly, before anything is
        spent. Layer A is free and deterministic, so the number of items that
        never reach a model is knowable in advance rather than estimated.

    python3 tools_validator_eval.py experiments --candidate <spec> --judge <spec>
        The three measurements that separate "can the validator work" from
        "which model should do it", run in order and recorded:

            ABD    everything except the free-answer judge
            C      the judge alone
            ABCD   the whole validator

        Only Layer A is deterministic; B and D are model calls constrained to
        quote their evidence. ABD is not "the deterministic layers", it is
        "the validator without the layer whose failure mode is agreeing with
        itself".

        --only ABD,C,ABCD (any subset, comma-separated) runs only the named
        experiments instead of all three. The frozen configuration still
        describes all three -- so a later invocation with a different --only,
        or none at all, is not treated as a change of configuration -- but the
        forecast, the budget check, and every recorded run cover only what
        this invocation actually executes. Useful to buy a cheap read on one
        arm (C is 100 judge calls, no Layer A gate ahead of it, and the arm
        most exposed to an unreliable endpoint) before committing to the rest.

VOCABULARY
----------
    --candidate   the model under evaluation. It occupies the grounding and
                  conformance layers.
    --judge       the independent model brought in to answer the item itself
                  and disagree with the key. A different model, enforced.
    --endpoint    where the requests go. Recorded, never a credential.

--max-wall-minutes <N> stops NEW calls once N minutes have elapsed since this
invocation started; a call already in flight finishes. Same treatment as
--max-calls: the arm in progress becomes INCOMPLETE with no delta, not a lower
score. Independent of the call budget -- the endpoint's own measured variance
(0.6s for a model listing, 72.9s and separately 180.8s for near-identical
completions) means every call-count ceiling can be respected and the run can
still take hours. No default; unset prints NO WALL-CLOCK CEILING SET rather
than running unbounded and silently.

There is deliberately no --provider. "Provider" is the adapter that builds a
model, and using the same word for "the model being evaluated" makes every run
record ambiguous about the one thing it exists to say. Passing --provider is an
error naming its replacement rather than a silent alias, because a silent alias
in a permanent record is worse than a break.

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

from validator import (ablation, analysis, budget as budget_mod, forecast as forecast_mod,
                       freeze as freeze_mod, metrics, pipeline, runs, scripted, wallclock)
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
    print(render(f"RUN: candidate {ground.model} | judge {judge_provider.model}",
                 devset.cases, verdicts, outages, oracle_used=used_oracle, config=config))
    kind = runs.KIND_CEILING if used_oracle else (
        runs.KIND_HOLDOUT if "holdout" in str(devset.root) else runs.KIND_DEVELOPMENT)
    path = _record(devset, verdicts, outages, config,
                   [runs.describe_provider("grounding", ground,
                                            seat=runs.SEAT_CANDIDATE,
                                            endpoint=args.endpoint),
                    runs.describe_provider("judge", judge_provider,
                                           seat=runs.SEAT_JUDGE,
                                           endpoint=args.endpoint),
                    runs.describe_provider("conformance", conform,
                                           seat=runs.SEAT_CANDIDATE,
                                           endpoint=args.endpoint)],
                   kind=kind, note=args.note)
    print(f"recorded as a {kind.upper()} run in {path}")
    _write(args, devset, verdicts, outages, config, oracle_used=used_oracle)
    return 0


def _record(devset, verdicts, outages, config, providers, *, kind, note="", freeze="",
            runs_dir=None, budget=None, measurement_unit=""):
    """Write the run to the record. A ceiling and a measurement are different kinds."""
    from validator.holdout import corpus_hash
    matrix = score(devset.cases, verdicts)
    gate = metrics.gate(matrix)
    expected = len(devset.arms)
    decided = matrix.total
    run = runs.Run(
        at=runs.now(), kind=kind, corpus=str(devset.root),
        corpus_hash=corpus_hash(devset.root),
        validator_version=pipeline.VALIDATOR_VERSION, config=config.label(),
        providers=providers, counts=matrix.as_dict()["counts"],
        sensitivity=matrix.sensitivity, specificity=matrix.specificity,
        gate=("withheld (oracle run)" if kind == runs.KIND_CEILING else gate.outcome),
        outages=len(outages), analysis=analysis.report(devset.cases, verdicts), note=note,
        freeze=freeze, budget=dict(budget or {}),
        measurement_unit=measurement_unit,
        items_expected=expected, items_decided=decided,
        completeness=(ablation.COMPLETE if not outages and decided >= expected
                      else ablation.INCOMPLETE))
    return runs.record(run, runs_dir=runs_dir or runs.RUNS_DIR)


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
    ("1  A+B+D  validator without the judge", "ABD",
     dict(structural=True, grounding=True, judge=False, conformance=True)),
    ("2  C      the independent judge alone", "C",
     dict(structural=False, grounding=False, judge=True, conformance=False)),
    ("3  ABCD   the whole validator", "ABCD",
     dict(structural=True, grounding=True, judge=True, conformance=True)),
)


def _select_experiments(only: str | None):
    """
    The experiments this invocation will execute, in EXPERIMENTS order.

    Order is fixed regardless of how --only lists them: execution order is
    "cheapest / least judge-dependent first", and letting the CLI argument
    order override that would make --only a way to accidentally reorder a run
    that was ordered on purpose.
    """
    if not only or not only.strip():
        return list(EXPERIMENTS)
    requested = {name.strip().upper() for name in only.split(",") if name.strip()}
    by_code = {layers.upper(): (title, layers, flags) for title, layers, flags in EXPERIMENTS}
    unknown = sorted(requested - set(by_code))
    if unknown:
        raise ValueError(
            f"--only names {unknown} which {'is' if len(unknown) == 1 else 'are'} not "
            f"among this set's experiments: {', '.join(sorted(by_code))}")
    return [by_code[layers.upper()] for _t, layers, _f in EXPERIMENTS
            if layers.upper() in requested]


def _withdrawn_seats(args) -> list[dict]:
    """
    Seats whose model the provider has withdrawn since the set was frozen.

    Consults `benchmark.discovery`'s registry when one exists on disk and says
    nothing when it does not -- a missing registry means discovery has not run
    here, which is not evidence that the models are fine. The check is
    deliberately one-directional: it can stop a run, never start one, and it
    never proposes a replacement.
    """
    try:
        from benchmark.discovery import DynamicModelRegistry, DEFAULT_REGISTRY_PATH
    except ImportError:
        return []
    if not Path(DEFAULT_REGISTRY_PATH).exists():
        return []
    registry = DynamicModelRegistry(DEFAULT_REGISTRY_PATH)
    seats = []
    for seat, spec in (("candidate", args.candidate), ("judge", args.judge)):
        provider, _, model_id = (spec or "").partition(":")
        if model_id:
            seats.append({"seat": seat, "provider": provider.strip(),
                          "model": model_id.strip()})
    return [row for row in registry.blocked_experiment_models(seats) if row["terminal"]]


def run_experiments(args):
    """
    The three measurements, in order, against one frozen configuration.

    Run together and recorded together because the number that matters is the
    difference between them. "Every layer earns its place" was asserted from a
    ceiling; this is where it is confirmed or withdrawn.
    """
    from benchmark.providers.registry import build_provider
    from validator.holdout import corpus_hash

    try:
        selected = _select_experiments(args.only)
    except ValueError as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return 2

    devset = load(args.corpus)
    ground = build_provider(parse_seat(args.candidate, endpoint=args.endpoint))
    judge_provider = build_provider(parse_seat(args.judge, endpoint=args.endpoint))
    if getattr(ground, "model", None) == getattr(judge_provider, "model", None):
        print("refusing to run: the candidate and the judge are the same model. Layer C "
              "exists to supply a judgement the candidate did not produce, and a second "
              "opinion from the same weights is the first opinion again.", file=sys.stderr)
        return 2
    blocked = _withdrawn_seats(args)
    if blocked:
        print("refusing to start: EXPERIMENT BLOCKED -- a model this set is frozen "
              "against can no longer be called.", file=sys.stderr)
        for row in blocked:
            print(f"  {row['seat'] or 'seat'}: {row['key']} is {row['availability']}",
                  file=sys.stderr)
            if row["detail"]:
                print(f"      {row['detail'][:180]}", file=sys.stderr)
        print("\nThe frozen configuration is what makes the runs in a set comparable, "
              "so nothing here will pick a replacement for you: an arm run against a "
              "substituted model is not the arm the other rows were measured with. "
              "Establishing a new pairing is an explicit decision -- rerun with "
              "--refreeze and a --note saying which models and why.", file=sys.stderr)
        return 2

    conform = ground
    seats = {"grounding": (ground, runs.SEAT_CANDIDATE),
             "judge": (judge_provider, runs.SEAT_JUDGE),
             "conformance": (conform, runs.SEAT_CANDIDATE)}
    provider_records = [runs.describe_provider(role, prov, seat=seat,
                                               endpoint=args.endpoint,
                                               credential_ref=args.credential_env)
                        for role, (prov, seat) in seats.items()]
    spends_money = any(p.is_model and not p.is_oracle for p in provider_records)

    plan = forecast_mod.plan(devset, selected, max_retries=args.max_retries,
                             max_calls=args.max_calls,
                             max_judge_calls=args.max_judge_calls,
                             max_wall_minutes=args.max_wall_minutes)
    print(forecast_mod.render(plan))
    if len(selected) < len(EXPERIMENTS):
        print(f"(scoped to {', '.join(layers for _t, layers, _f in selected)} by --only; "
              f"the frozen configuration still covers all of "
              f"{', '.join(layers for _t, layers, _f in EXPERIMENTS)})")
    print()
    if plan["verdict"] == forecast_mod.IMPOSSIBLE:
        print("refusing to start: the budget is below what the measurement needs, so "
              "every arm would stop early and no delta could be computed.",
              file=sys.stderr)
        return 2
    if spends_money:
        if args.max_calls is None or args.max_judge_calls is None:
            print("refusing to start: a run using real models needs both --max-calls and "
                  "--max-judge-calls. Discovering the cost halfway through is the thing "
                  "the forecast above exists to prevent.", file=sys.stderr)
            return 2
        if not args.confirm_spend:
            print("refusing to start: this run will send requests to a real model. Read "
                  "the forecast above, then pass --confirm-spend.", file=sys.stderr)
            return 2

    spend = budget_mod.Budget(max_calls=args.max_calls,
                              max_judge_calls=args.max_judge_calls)
    clock = (wallclock.WallClock(max_minutes=args.max_wall_minutes)
             if args.max_wall_minutes is not None else None)
    boundaries = set()
    try:
        for prov, seat in ((ground, runs.SEAT_CANDIDATE),
                           (judge_provider, runs.SEAT_JUDGE)):
            _, boundary = budget_mod.meter(prov, spend, seat, clock=clock)
            boundaries.add(boundary)
    except budget_mod.UnmeterableProvider as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return 2
    # One unit for the whole set. A mixed set would put two incomparable
    # numbers under one column heading.
    unit = (budget_mod.CANONICAL_UNIT if boundaries == {budget_mod.CANONICAL_UNIT}
            else budget_mod.BOUNDARY_LOGICAL)

    current = freeze_mod.build(
        corpus=str(devset.root), corpus_hash=corpus_hash(devset.root),
        models=[freeze_mod.describe_model(role, prov, seat=seat,
                                          endpoint=args.endpoint,
                                          temperature=args.temperature,
                                          credential_ref=args.credential_env)
                for role, (prov, seat) in seats.items()],
        experiments=[{"name": title, "layers": layers, "config": flags}
                     for title, layers, flags in EXPERIMENTS],
        sampling={"temperature": args.temperature},
        extra={"retry_max_retries": args.max_retries,
               "budget_max_calls": args.max_calls,
               "budget_max_judge_calls": args.max_judge_calls,
               "max_wall_minutes": args.max_wall_minutes},
        note=args.note, created_at=runs.now())
    freeze_path = freeze_mod.path_for(str(devset.root),
                                      args.freeze_dir or freeze_mod.FREEZE_DIR)

    if args.refreeze:
        if not args.note.strip():
            print("refusing to refreeze without --note saying what changed and why",
                  file=sys.stderr)
            return 2
        Path(freeze_path).unlink(missing_ok=True)
    try:
        in_force = freeze_mod.assert_unchanged(current, freeze_path)
    except freeze_mod.FreezeViolation as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not Path(freeze_path).exists():
        freeze_mod.write(current, freeze_path)
        print(f"configuration frozen as {in_force.digest()[:12]} in {freeze_path}\n")
    else:
        print(f"running under frozen configuration {in_force.digest()[:12]}\n")

    arms = []
    for title, layers, flags in selected:
        config = pipeline.Config(**flags)
        verdicts, outages = evaluate(devset.cases, grounding_provider=ground,
                                     judge_provider=judge_provider,
                                     conformance_provider=conform, config=config)
        matrix = score(devset.cases, verdicts)
        edge = analysis.edge_behaviour(devset.cases, verdicts)
        used_fake = any(p.is_oracle or not p.is_model for p in provider_records)
        _record(devset, verdicts, outages, config, provider_records,
                kind=(runs.KIND_CEILING if used_fake else runs.KIND_DEVELOPMENT),
                note=f"{title}; {args.note}".strip("; "),
                freeze=in_force.digest(), runs_dir=args.runs_dir or runs.RUNS_DIR,
                budget=spend.as_dict(), measurement_unit=unit)
        arms.append(ablation.Arm(
            name=title, layers=layers, matrix=matrix, outages=len(outages),
            items_expected=len(devset.arms), items_decided=matrix.total,
            edge_abstention=edge["abstention_rate"],
            analysis=analysis.report(devset.cases, verdicts)))
        print(render(title, devset.cases, verdicts, outages,
                     oracle_used=used_fake, config=config))
        print()
        # The arm that just ran is recorded and reported above regardless --
        # it may itself be INCOMPLETE if the ceiling was crossed partway
        # through it. This only decides whether the NEXT experiment in this
        # invocation gets to start at all; the meter would refuse every one of
        # its calls anyway, but starting it just to watch it fail immediately
        # is noise this avoids.
        if clock is not None and clock.max_minutes is not None \
                and clock.elapsed_minutes >= clock.max_minutes \
                and (title, layers, flags) != selected[-1]:
            print(f"wall-clock ceiling reached at {clock.elapsed_minutes:.1f} minute(s); "
                  "not starting the remaining experiment(s) in this invocation.")
            break

    data = ablation.report(arms,
                           judge_model=str(getattr(judge_provider, "model", "")),
                           candidate_model=str(getattr(ground, "model", "")))
    print("=" * 78)
    print(ablation.render(data))
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(
            {"freeze": in_force.as_dict(), "ablation": data}, indent=2, default=str),
            encoding="utf-8")
        print(f"\nwritten to {args.out}")
    return 0


def parse_seat(spec: str, *, endpoint: str = "") -> dict:
    """
    Turn `provider:model_id` into a provider spec.

    The two seats are usually different models from the SAME provider -- 8B in
    the candidate seat, 70B as judge, both on one endpoint -- and a bare
    provider name cannot express that. Splitting on the first colon only, so a
    model id containing slashes or further colons survives intact.

    Never carries a credential: the NVIDIA builder reads its key from the
    environment by name, which is why the key can stay out of the spec, out of
    the freeze manifest and out of the run record.
    """
    spec = (spec or "").strip()
    if not spec:
        raise ValueError("a seat needs a provider, optionally with a model id")
    provider, _, model_id = spec.partition(":")
    out: dict = {"provider": provider.strip()}
    if model_id.strip():
        out["model_id"] = model_id.strip()
    if endpoint:
        out["base_url"] = endpoint
    return out


def _budget_args(sub_parser):
    sub_parser.add_argument("--max-calls", type=int, default=None,
                            help="hard ceiling on OUTBOUND ATTEMPTS, the unit the meter "
                                 "counts; retries are included")
    sub_parser.add_argument("--max-judge-calls", type=int, default=None,
                            help="hard ceiling on outbound attempts in the judge seat")
    sub_parser.add_argument("--credential-env", default="",
                            help="name of the environment variable holding the key. "
                                 "Recorded as credential_ref; the value never is")
    sub_parser.add_argument("--confirm-spend", action="store_true",
                            help="required before a run using real models makes any "
                                 "request")


def run_forecast(args):
    devset = load(args.corpus)
    data = forecast_mod.plan(devset, EXPERIMENTS, max_retries=args.max_retries,
                             max_calls=args.max_calls,
                             max_judge_calls=args.max_judge_calls)
    print(forecast_mod.render(data))
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"\nwritten to {args.out}")
    return 0


def _seats(sub_parser):
    """The two experimental roles, plus the endpoint. Never --provider."""
    sub_parser.add_argument("--candidate", required=True,
                            help="the model under evaluation, as provider:model_id; "
                                 "occupies the grounding and conformance layers")
    sub_parser.add_argument("--judge", required=True,
                            help="the independent model for Layer C, as "
                                 "provider:model_id; must differ from the candidate")
    sub_parser.add_argument("--endpoint", default="",
                            help="recorded in the run record and the freeze manifest; "
                                 "never a credential")
    sub_parser.add_argument("--provider", default=None, help=argparse.SUPPRESS)


#: What a chat endpoint has to end with. `NVIDIAProvider` POSTs to `base_url`
#: verbatim, so a base that stops at `/v1` sends every request to a path that
#: does not accept POST. This repository's own documented example did exactly
#: that, which would have spent an entire 2295-attempt budget collecting 404s
#: and reported them as a validator that could not reach a model.
COMPLETIONS_SUFFIX = "/chat/completions"


def _reject_bad_endpoint(args) -> bool:
    """
    Refuse an endpoint that is not a completions URL, rather than rewriting it.

    Silently appending a path would be friendlier and worse: `--endpoint` is
    recorded in the freeze manifest as where the requests went, and a manifest
    that names one URL while the run used another is a provenance record that
    lies. One line for the operator to correct beats a guess this tool cannot
    take back.
    """
    endpoint = (getattr(args, "endpoint", "") or "").strip()
    if not endpoint or endpoint.rstrip("/").endswith(COMPLETIONS_SUFFIX):
        return False
    print(f"refusing to start: --endpoint {endpoint!r} is not a completions URL. "
          f"The provider POSTs to it verbatim, so a base that stops short would "
          f"return 404 for every call in the budget. Pass "
          f"{endpoint.rstrip('/') + COMPLETIONS_SUFFIX!r}, or omit --endpoint to "
          f"use the adapter's default.", file=sys.stderr)
    return True


def _reject_provider(args):
    if getattr(args, "provider", None) is not None:
        print("--provider has been removed. 'Provider' is the adapter that builds a "
              "model; using it to mean 'the model under evaluation' makes every run "
              "record ambiguous about the one thing it exists to say. Pass --candidate "
              "for the model being evaluated and --judge for the independent model.",
              file=sys.stderr)
        return True
    return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--corpus", default="corpus/validator_dev")
    parser.add_argument("--out", default="")
    parser.add_argument("--runs-dir", default="",
                        help="where run records are written; a separate directory keeps "
                             "an experiment set isolated from earlier ones")
    parser.add_argument("--freeze-dir", default="")
    sub = parser.add_subparsers(dest="mode", required=True)
    ceiling = sub.add_parser("ceiling")
    ceiling.add_argument("--spend-a-look", default="",
                         help="reason for reading the holdout; recorded in its ledger")
    layers = sub.add_parser("layers")
    layers.add_argument("--spend-a-look", default="")
    real = sub.add_parser("run")
    _seats(real)
    real.add_argument("--note", default="")

    forecast_cmd = sub.add_parser("forecast")
    forecast_cmd.add_argument("--max-calls", type=int, default=None)
    forecast_cmd.add_argument("--max-judge-calls", type=int, default=None)
    forecast_cmd.add_argument("--max-retries", type=int, default=2)

    experiments = sub.add_parser("experiments")
    _seats(experiments)
    _budget_args(experiments)
    experiments.add_argument("--max-retries", type=int, default=2)
    experiments.add_argument("--max-wall-minutes", type=float, default=None,
                             help="stop starting new calls once this many minutes have "
                                  "elapsed since the invocation started; a call already "
                                  "in flight finishes. No default -- unset prints NO "
                                  "WALL-CLOCK CEILING SET rather than running unbounded")
    experiments.add_argument("--only", default=None,
                             help="comma-separated subset of this set's experiments "
                                  "(ABD, C, ABCD) to run instead of all three; the "
                                  "frozen configuration still covers all three")
    experiments.add_argument("--note", default="")
    experiments.add_argument("--temperature", type=float, default=0.0)
    experiments.add_argument("--refreeze", action="store_true",
                             help="start a new experiment set, discarding the frozen "
                                  "configuration; requires --note")
    args = parser.parse_args(argv)
    if _reject_provider(args):
        return 2
    if _reject_bad_endpoint(args):
        return 2
    return {"ceiling": run_ceiling, "layers": run_layers, "run": run_real,
            "experiments": run_experiments,
            "forecast": run_forecast}[args.mode](args)


if __name__ == "__main__":
    raise SystemExit(main())
