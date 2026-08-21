"""
The layered validator: three checks, in increasing order of cost and decreasing
order of certainty, with the reason for every verdict attributable to a layer.

WHY LAYERED AND NOT ONE PROMPT
------------------------------
The measured single-model validator caught 11 of 20 planted defects and
false-flagged 9 of 10 clean items. A single prompt asked to decide "is this
question good" has to hold structure, grounding and medicine in one judgement,
and when it fails there is nothing to look at afterwards: the output is one
verdict and one paragraph, and neither says which part of the job went wrong.

Splitting the job means every flag names the layer and the check that produced
it. A run with a poor specificity can then be read: if the false flags are all
`independent_answer_differs_from_key`, the judge is the problem and the fix is
the judge. If they are all `multiple_options_supported`, the grounding prompt is
over-reading. That is the difference between a number and a diagnosis.

ORDER, AND WHY IT IS THIS ORDER
-------------------------------
    A  structural   deterministic, free, no false flags
    B  grounding    one or two model calls, checkable against the passage
    C  judge        one model call, most informative, least checkable

A fatal structural finding stops the pipeline. Not as an optimisation -- though
it is one -- but because an item with a key pointing outside its options has no
answer, and asking a model which of the four options the passage supports when
the item keys the fifth produces a confident reply about a question nobody
asked.

HOW THE LAYERS COMBINE
----------------------
    any layer flags                     -> FLAGGED
    no flags, any layer abstained       -> ABSTAINED
    every layer that ran passed         -> PASSED

Abstention is not a hedge. It is the honest outcome when the validator could not
form a view -- an unverifiable quotation, an unsure judge -- and it is scored
separately from both arms precisely so that a validator cannot improve its
apparent accuracy by abstaining on everything it finds hard.

A LAYER THAT WAS CONFIGURED AND THEN FAILED IS AN OUTAGE
--------------------------------------------------------
`run` does not catch GroundingUnavailable or JudgeUnavailable. A validator that
converts its own outage into a PASS puts a validation stamp on an item nothing
looked at. The caller decides what to do about an outage; this module refuses to
decide for it by silently succeeding.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from benchmark.corpus import QUESTION_TYPES

from validator import conformance, grounding, judge, structural
from validator.judge import CONFIDENCE_FLOOR
from validator.metrics import ABSTAINED, FLAGGED, PASSED

VALIDATOR_VERSION = "0.2.0"

LAYER_STRUCTURAL = "structural"
LAYER_GROUNDING = "grounding"
LAYER_JUDGE = "judge"
LAYER_CONFORMANCE = "conformance"
LAYERS = (LAYER_STRUCTURAL, LAYER_GROUNDING, LAYER_JUDGE, LAYER_CONFORMANCE)


@dataclass
class Verdict:
    item_id: str
    verdict: str
    version: str = VALIDATOR_VERSION
    layers_run: tuple[str, ...] = ()
    flags: tuple[tuple[str, str], ...] = ()      # (layer, check)
    abstentions: tuple[tuple[str, str], ...] = ()
    detail: tuple[str, ...] = ()
    calls: int = 0
    results: dict = field(default_factory=dict)

    @property
    def flagged(self) -> bool:
        return self.verdict == FLAGGED

    @property
    def checks(self) -> list[str]:
        return [check for _, check in self.flags]

    def as_dict(self) -> dict:
        return {"item_id": self.item_id, "verdict": self.verdict, "version": self.version,
                "layers_run": list(self.layers_run),
                "flags": [{"layer": lay, "check": chk} for lay, chk in self.flags],
                "abstentions": [{"layer": lay, "check": chk} for lay, chk in self.abstentions],
                "detail": list(self.detail), "calls": self.calls,
                "results": {k: v.as_dict() for k, v in self.results.items()}}


@dataclass
class Config:
    """
    Which layers run. Turning one off is a measurement, not a convenience:
    each configuration is a different validator and gets its own confusion
    matrix, so `label()` names it in a way a report can carry.
    """
    structural: bool = True
    grounding: bool = True
    judge: bool = True
    conformance: bool = True
    check_explanation: bool = True
    show_passage_to_judge: bool = True
    require_source: bool = True
    # Referenced by value, not as judge.CONFIDENCE_FLOOR: the field named `judge`
    # above shadows the module inside this class body.
    confidence_floor: float = CONFIDENCE_FLOOR

    def label(self) -> str:
        on = [name for name, enabled in
              (("A", self.structural), ("B", self.grounding), ("C", self.judge),
               ("D", self.conformance)) if enabled]
        return f"v{VALIDATOR_VERSION}[{''.join(on) or 'none'}]"


def run(item: dict, *, grounding_provider=None, judge_provider=None,
        conformance_provider=None, config: Config | None = None) -> Verdict:
    """
    Validate one item. Raises on an outage in a layer that was asked to run.
    """
    config = config or Config()
    item_id = item.get("id", "item")
    layers: list[str] = []
    flags: list[tuple[str, str]] = []
    abstentions: list[tuple[str, str]] = []
    detail: list[str] = []
    results: dict = {}
    calls = 0

    if config.structural:
        layers.append(LAYER_STRUCTURAL)
        result = structural.check(item, question_types=QUESTION_TYPES,
                                  require_source=config.require_source)
        results[LAYER_STRUCTURAL] = result
        for finding in result.findings:
            if finding.fatal:
                flags.append((LAYER_STRUCTURAL, finding.check))
                detail.append(f"[structural] {finding.detail}")
        if not result.ok:
            # Stop here. Every later layer would be reasoning about a question
            # that cannot be asked.
            return Verdict(item_id, FLAGGED, VALIDATOR_VERSION, tuple(layers), tuple(flags),
                           tuple(abstentions), tuple(detail), calls, results)
        for finding in result.findings:
            detail.append(f"[structural, not fatal] {finding.detail}")

    if config.grounding:
        if grounding_provider is None:
            raise grounding.GroundingUnavailable(
                f"{item_id}: the grounding layer is enabled but no provider was supplied. A "
                "layer that is configured and does not run is an outage, not a skipped step.")
        layers.append(LAYER_GROUNDING)
        result = grounding.check(item, grounding_provider,
                                 check_explanation=config.check_explanation)
        results[LAYER_GROUNDING] = result
        calls += result.calls
        target = flags if result.verdict == FLAGGED else (
            abstentions if result.verdict == ABSTAINED else None)
        if target is not None:
            for check in result.checks:
                target.append((LAYER_GROUNDING, check))
        detail.extend(f"[grounding] {d}" for d in result.detail)

    if config.judge:
        if judge_provider is None:
            raise judge.JudgeUnavailable(
                f"{item_id}: the judge layer is enabled but no provider was supplied. A layer "
                "that is configured and does not run is an outage, not a skipped step.")
        layers.append(LAYER_JUDGE)
        result = judge.check(item, judge_provider,
                             show_passage=config.show_passage_to_judge,
                             confidence_floor=config.confidence_floor)
        results[LAYER_JUDGE] = result
        calls += result.calls
        target = flags if result.verdict == FLAGGED else (
            abstentions if result.verdict == ABSTAINED else None)
        if target is not None:
            for check in result.checks:
                target.append((LAYER_JUDGE, check))
        detail.extend(f"[judge] {d}" for d in result.detail)

    if config.conformance:
        provider = conformance_provider or grounding_provider
        if provider is None:
            raise conformance.ConformanceUnavailable(
                f"{item_id}: the conformance layer is enabled but no provider was supplied. A "
                "layer that is configured and does not run is an outage, not a skipped step.")
        layers.append(LAYER_CONFORMANCE)
        result = conformance.check(item, provider)
        results[LAYER_CONFORMANCE] = result
        calls += result.calls
        target = flags if result.verdict == FLAGGED else (
            abstentions if result.verdict == ABSTAINED else None)
        if target is not None:
            for check in result.checks:
                target.append((LAYER_CONFORMANCE, check))
        detail.extend(f"[conformance] {d}" for d in result.detail)

    if flags:
        verdict = FLAGGED
    elif abstentions:
        verdict = ABSTAINED
    else:
        verdict = PASSED

    return Verdict(item_id, verdict, VALIDATOR_VERSION, tuple(layers), tuple(flags),
                   tuple(abstentions), tuple(detail), calls, results)
