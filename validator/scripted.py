"""
Providers that are not models: replay for tests, and a ceiling oracle.

WHY AN ORACLE EXISTS, AND WHAT IT IS FORBIDDEN TO BE USED FOR
------------------------------------------------------------
The oracle answers every prompt the way a perfect validator would, by reading
the ground-truth labels. It therefore measures nothing about any model, and
`is_oracle` is set on it so a run that used it can be marked unfit for the gate
rather than relying on whoever reads the report to remember.

What it does measure is worth having before spending a rupee on inference:

    THE CEILING. If the layers were flawless, which planted defects would this
    validator design still miss? That number is a property of the design, not of
    the model, and it is knowable in advance. A design whose ceiling is below
    the pass threshold cannot be fixed by choosing a better model, and finding
    that out from a model bill is the expensive way to find it out.

    COURSE CONSISTENCY. A perfect run that does not produce a clean confusion
    matrix means the corpus disagrees with itself.

REPLAY
------
`ReplayProvider` returns a canned reply per request id, and can be told to fail
or to return unparseable text for specific ids, so the outage paths -- the ones
that must never become a PASS -- are exercised deterministically.
"""

from __future__ import annotations

import json
import re

from benchmark.providers.base import BaseProvider, GenerationRequest, GenerationResponse

from validator.devset import CLEAN, DEFECTIVE
from validator.grounding import LETTERS

# Defect classes this validator design has no check for. Kept here rather than
# inferred, so that adding a check means deleting a line from a list that a
# test reads -- and the ceiling moves visibly.
UNCOVERED_BY_DESIGN = ()

# What v0.1 could not see. Kept as history: the ceiling run that produced this
# list is why Layer D exists, and a later design regressing onto it should be
# obvious rather than rediscovered.
UNCOVERED_BY_DESIGN_V0_1 = ("out_of_syllabus", "poor_reasoning", "giveaway", "trivial")


class ReplayProvider(BaseProvider):
    """Returns pre-built replies keyed on request id. Not a model."""

    name = "validator-replay"
    model = "replay"
    model_version = "v0.1"
    model_family = "none"
    is_oracle = False

    def __init__(self, replies: dict[str, dict | str] | None = None, *,
                 errors: set[str] | None = None, garbage: set[str] | None = None,
                 default: dict | None = None, model: str | None = None,
                 model_family: str | None = None):
        self.replies = dict(replies or {})
        self.errors = set(errors or ())
        self.garbage = set(garbage or ())
        self.default = default
        self.seen: list[str] = []
        self.prompts: list[str] = []
        if model:
            self.model = model
        if model_family:
            self.model_family = model_family

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        self.seen.append(request.item_id)
        self.prompts.append(request.prompt)
        if request.item_id in self.errors:
            return GenerationResponse(
                item_id=request.item_id, raw_output="", parsed=None, provider=self.name,
                model=self.model, model_version=self.model_version, latency_ms=0.0,
                error="scripted backend failure")
        if request.item_id in self.garbage:
            raw = "I had a look and it seems fine to me."
        else:
            reply = self.replies.get(request.item_id, self.default)
            if reply is None:
                return GenerationResponse(
                    item_id=request.item_id, raw_output="", parsed=None, provider=self.name,
                    model=self.model, model_version=self.model_version, latency_ms=0.0,
                    error=f"no scripted reply for {request.item_id!r}")
            raw = reply if isinstance(reply, str) else json.dumps(reply)
        return GenerationResponse(
            item_id=request.item_id, raw_output=raw, parsed=None, provider=self.name,
            model=self.model, model_version=self.model_version, latency_ms=0.0)


class OracleProvider(ReplayProvider):
    """A ReplayProvider built from ground truth. Never valid for gating."""

    name = "validator-oracle"
    model = "oracle"
    is_oracle = True


def longest_span(passage: str) -> str:
    """A verbatim span of the passage, for an oracle that must quote its evidence."""
    parts = [p.strip() for p in re.split(r"(?<=[.;])\s+", passage or "") if p.strip()]
    return max(parts, key=len) if parts else (passage or "").strip()


def _changed_option(defective: dict, clean: dict) -> int | None:
    a, b = list(defective.get("options") or []), list(clean.get("options") or [])
    if len(a) != len(b):
        return None
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return i
    return None


def oracle_replies(cases) -> dict[str, dict]:
    """
    Build the reply a flawless validator would give for every case.

    The mapping from defect class to reply is the design's claim about itself:
    it says which layer is supposed to notice which defect. Anything not
    represented here is a defect the design has no answer for, and the run that
    uses these replies is what turns that into a number.
    """
    by_id = {c.id: c for c in cases}
    replies: dict[str, dict] = {}

    for case in cases:
        item = case.item.as_dict()
        passage = case.item.source_passage
        span = longest_span(passage)
        options = case.item.options
        key_letter = LETTERS[case.item.correct_index]

        supported = [key_letter]
        addresses = True
        judge_answer = key_letter
        judge_also: list[str] = []
        judge_answerable = True
        contradicted: list[dict] = []
        gives_a_reason = True
        matches_concept = True
        cognitive_level = "application"
        cued, cue = False, ""

        if case.label == DEFECTIVE:
            twin = by_id.get(case.derived_from)
            twin_item = twin.item.as_dict() if twin else None
            if case.defect_class == "wrong_key" and twin:
                twin_letter = LETTERS[twin.item.correct_index]
                supported = [twin_letter]
                judge_answer = twin_letter
            elif case.defect_class == "two_correct" and twin_item:
                changed = _changed_option(item, twin_item)
                if changed is not None:
                    other = LETTERS[changed]
                    supported = sorted({key_letter, other})
                    judge_also = [other]
            elif case.defect_class == "ungrounded":
                supported, addresses = [], False
            elif case.defect_class == "ambiguous_stem":
                judge_answerable = False
            elif case.defect_class == "hallucinated_fact":
                contradicted = [{"claim": case.item.explanation[:120],
                                 "passage_says": span}]
            elif case.defect_class == "poor_reasoning":
                gives_a_reason = False
            elif case.defect_class == "out_of_syllabus":
                matches_concept = False
            elif case.defect_class == "trivial":
                cognitive_level = "recall"
            elif case.defect_class == "giveaway":
                cued, cue = True, longest_span(case.item.stem)

        replies[f"{case.id}:key"] = {
            "passage_addresses_question": addresses,
            "supported": supported,
            "evidence": {letter: span for letter in supported},
            "reasoning": "oracle",
        }
        replies[f"{case.id}:explanation"] = {"contradicted": contradicted, "absent": [],
                                             "gives_a_reason": gives_a_reason}
        replies[f"{case.id}:conformance"] = {
            "concept_tested": case.item.concept if matches_concept else "a different concept",
            "matches_requested_concept": matches_concept,
            "cognitive_level": cognitive_level,
            "answerable_from_wording_alone": cued, "wording_cue": cue,
            "reasoning": "oracle"}
        replies[f"{case.id}:judge"] = {
            "answer": judge_answer, "confidence": 0.95,
            "also_defensible": judge_also, "answerable": judge_answerable,
            "reasoning": "oracle",
        }
        if len(options) > len(LETTERS):  # pragma: no cover - guarded upstream
            raise ValueError(f"{case.id}: more options than the oracle can label")
        if case.label == CLEAN:
            assert supported == [key_letter]
    return replies


def oracle(cases) -> tuple[OracleProvider, OracleProvider, OracleProvider]:
    """A (grounding, judge, conformance) triple of oracles for a set of cases."""
    replies = oracle_replies(cases)
    return (OracleProvider(replies, model="oracle-grounding"),
            OracleProvider(replies, model="oracle-judge"),
            OracleProvider(replies, model="oracle-conformance"))
