"""
Layer B: is the keyed answer supported by the evidence supplied with the item?

THE QUESTION THIS LAYER IS ALLOWED TO ASK
-----------------------------------------
    Does the supplied passage support the keyed option?

and not

    Does this look like a reasonable medical question?

The difference is the whole design. The second question has no ground truth,
so a model answering it produces an opinion with a confidence attached, and
disagreement between the model and a reviewer cannot be resolved by anyone.
The first question has an answer that a person can check in under a minute by
reading two paragraphs, which is what makes a false flag from this layer
diagnosable instead of arguable.

THE MODEL IS ASKED FOR EVIDENCE, NOT FOR A VERDICT
--------------------------------------------------
The prompt never asks "is this question good". It asks which options the
passage supports, and for the verbatim span that supports each. The verdict is
then computed here, by arithmetic:

    no option supported          -> FLAG, the item is not answerable from its evidence
    the keyed option unsupported -> FLAG, the key contradicts the evidence
    more than one supported      -> FLAG, the item has no single best answer
    exactly one, and it is keyed -> PASS

A model that wants to say "looks fine to me" has nowhere to put it.

THE QUOTE IS CHECKED
--------------------
Every span the model offers as evidence is searched for in the passage. A span
that is not there was invented, and a validator that invents its evidence is
not evidence. When that happens this layer ABSTAINS rather than passing or
flagging: the item may be perfectly good, but this run learned nothing about
it, and recording a guess as a measurement is how a validator's numbers stop
meaning anything.

THE KEY IS WITHHELD
-------------------
The prompt shows the passage, the stem and the options. It does not show the
keyed index, the explanation or the reference. A model told which answer is
intended agrees with it; that agreement is the failure mode this layer exists
to detect, so the information that produces it is not supplied.

The explanation is checked separately, in its own call, against the passage
alone -- which is where a fabricated fact shows up.

WHAT THIS LAYER COSTS
---------------------
Two model calls per item. That is the price of separating "is the key right"
from "is the explanation truthful"; asking both in one call lets an answer to
one contaminate the other, and the two failures need different remedies.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from benchmark.providers.base import GenerationRequest

from validator.metrics import ABSTAINED, FLAGGED, PASSED

PROMPT_VERSION = "grounding/0.1.0"

# Check ids produced by this layer.
NOT_ANSWERABLE_FROM_PASSAGE = "not_answerable_from_passage"
KEY_NOT_SUPPORTED = "key_not_supported_by_passage"
MULTIPLE_OPTIONS_SUPPORTED = "multiple_options_supported"
EXPLANATION_CONTRADICTS_PASSAGE = "explanation_contradicts_passage"
EXPLANATION_UNSUPPORTED_CLAIM = "explanation_claim_absent_from_passage"
EXPLANATION_ASSERTS_WITHOUT_REASON = "explanation_asserts_without_reason"

# Failures of the validator itself, not of the item.
EVIDENCE_NOT_IN_PASSAGE = "evidence_not_in_passage"
REPLY_UNPARSEABLE = "reply_unparseable"

LETTERS = "ABCDEFGH"

SYSTEM = (
    "You check whether a multiple-choice question can be answered from a supplied passage. "
    "You are not asked whether the question is good, whether it is well written, or whether "
    "the subject matter is important. You answer only from the passage in front of you, and "
    "you quote the passage for every claim you make. Reply with one JSON object and nothing else."
)

KEY_PROMPT = """Passage:
\"\"\"
{passage}
\"\"\"

Question: {stem}

Options:
{options}

Using ONLY the passage above, decide which options it supports as correct answers to the
question. Do not use knowledge from outside the passage. If the passage does not address the
question at all, say so.

Reply with one JSON object:
{{"passage_addresses_question": true or false,
  "supported": ["A"],
  "evidence": {{"A": "a span copied word for word from the passage"}},
  "reasoning": "one sentence"}}

"supported" may contain zero, one or several letters. Every letter in "supported" must have an
entry in "evidence", and every span in "evidence" must be copied from the passage exactly."""

EXPLANATION_PROMPT = """Passage:
\"\"\"
{passage}
\"\"\"

Explanation given with the question:
\"\"\"
{explanation}
\"\"\"

Compare the explanation against the passage. List any factual claim in the explanation that the
passage CONTRADICTS, and separately any claim that is simply ABSENT from the passage. A claim
being absent is not by itself an error; only report what the explanation actually asserts.

Then say separately whether the explanation gives a SUBSTANTIVE reason for the answer. It does
not if it only asserts that the answer is correct, restates the question, names the diagnosis
again, or appeals to a book or a chart without saying why.

Reply with one JSON object:
{{"contradicted": [{{"claim": "...", "passage_says": "a span copied word for word from the passage"}}],
  "absent": ["..."],
  "gives_a_reason": true or false}}"""


class GroundingUnavailable(RuntimeError):
    """
    The layer could not run. Never a PASS.

    A configured backend that fails is an outage. Returning "no problem found"
    when nothing was checked is how an unchecked item reaches a learner with a
    validation stamp on it.
    """


@dataclass(frozen=True)
class GroundingResult:
    verdict: str
    checks: tuple[str, ...] = ()
    detail: tuple[str, ...] = ()
    supported: tuple[str, ...] = ()
    evidence: dict = field(default_factory=dict)
    calls: int = 0
    raw: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.verdict == PASSED

    def as_dict(self) -> dict:
        return {"layer": "grounding", "prompt_version": PROMPT_VERSION,
                "verdict": self.verdict, "checks": list(self.checks),
                "detail": list(self.detail), "supported": list(self.supported),
                "evidence": dict(self.evidence), "calls": self.calls}


def extract_json(text: str) -> dict | None:
    """First balanced JSON object in a reply. Models fence and preface regardless."""
    if not text:
        return None
    depth, start = 0, None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    start = None
    return None


def _flatten(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def quote_is_in(passage: str, quote: str) -> bool:
    """
    Whether a span the model offered as evidence actually appears in the passage.

    Whitespace is normalised because a model reflowing a line break is not
    fabricating anything. Nothing else is normalised: a span that has been
    paraphrased, however faithfully, is no longer a quotation, and the point of
    demanding a quotation was to make the evidence checkable without a model.
    """
    quote = _flatten(quote).strip('"“” ')
    if len(quote) < 12:
        return False
    return quote in _flatten(passage)


def format_options(options: list[str]) -> str:
    return "\n".join(f"{LETTERS[i]}. {o}" for i, o in enumerate(options))


def _ask(provider, item_id: str, prompt: str, *, purpose: str) -> tuple[dict, str]:
    request = GenerationRequest(item_id=f"{item_id}:{purpose}", prompt=prompt,
                                system=SYSTEM, temperature=0.0,
                                metadata={"layer": "grounding", "purpose": purpose,
                                          "prompt_version": PROMPT_VERSION})
    response = provider.generate(request)
    if not response.ok:
        raise GroundingUnavailable(
            f"{item_id}: the grounding backend failed ({response.error}). Nothing was checked, "
            "so nothing may be reported as checked.")
    parsed = extract_json(response.raw_output)
    if parsed is None:
        raise GroundingUnavailable(
            f"{item_id}: the grounding backend returned no JSON object for {purpose}. An "
            "unparseable validator is an outage, not a clean item.")
    return parsed, response.raw_output


def check(item: dict, provider, *, check_explanation: bool = True) -> GroundingResult:
    """
    Run Layer B over one item. Raises GroundingUnavailable rather than guessing.
    """
    passage = (item.get("source_passage") or "").strip()
    options = list(item.get("options") or [])
    key = item.get("correct_index")
    item_id = item.get("id", "item")

    if not passage:
        raise GroundingUnavailable(
            f"{item_id}: no source passage. This layer checks the key against the evidence "
            "supplied with the item; with no evidence there is nothing to check against, and "
            "the absence is a structural finding, not a grounding one.")
    if not options or not isinstance(key, int) or isinstance(key, bool) \
            or not 0 <= key < len(options):
        raise GroundingUnavailable(
            f"{item_id}: the options or the key are malformed. Layer A decides that; this "
            "layer must not be handed an item it cannot form a question from.")
    if len(options) > len(LETTERS):
        raise GroundingUnavailable(f"{item_id}: more options than this layer can label")

    checks: list[str] = []
    detail: list[str] = []
    raws: list[str] = []
    calls = 0

    parsed, raw = _ask(provider, item_id,
                       KEY_PROMPT.format(passage=passage, stem=(item.get("stem") or "").strip(),
                                         options=format_options(options)),
                       purpose="key")
    calls += 1
    raws.append(raw)

    supported = [str(s).strip().upper()[:1] for s in (parsed.get("supported") or [])
                 if str(s).strip()]
    supported = [s for s in dict.fromkeys(supported) if s in LETTERS[:len(options)]]
    evidence = {str(k).strip().upper()[:1]: str(v)
                for k, v in (parsed.get("evidence") or {}).items()}

    unverified = [letter for letter in supported
                  if not quote_is_in(passage, evidence.get(letter, ""))]
    if unverified:
        return GroundingResult(
            ABSTAINED, (EVIDENCE_NOT_IN_PASSAGE,),
            (f"the span offered as evidence for option(s) {', '.join(unverified)} does not "
             "appear in the passage, so this run produced no usable evidence about the item",),
            tuple(supported), evidence, calls, tuple(raws))

    addresses = bool(parsed.get("passage_addresses_question", True))
    key_letter = LETTERS[key]

    if not addresses or not supported:
        checks.append(NOT_ANSWERABLE_FROM_PASSAGE)
        detail.append("the passage does not support any of the options as the answer, so the "
                      "item cannot be answered from the evidence supplied with it")
    else:
        if key_letter not in supported:
            checks.append(KEY_NOT_SUPPORTED)
            detail.append(
                f"the passage supports {', '.join(supported)} but the item keys {key_letter}")
        if len(supported) > 1:
            checks.append(MULTIPLE_OPTIONS_SUPPORTED)
            detail.append(
                f"the passage supports {len(supported)} options ({', '.join(supported)}), so "
                "the item has no single best answer")

    if check_explanation and (item.get("explanation") or "").strip():
        parsed2, raw2 = _ask(provider, item_id,
                             EXPLANATION_PROMPT.format(
                                 passage=passage,
                                 explanation=(item.get("explanation") or "").strip()),
                             purpose="explanation")
        calls += 1
        raws.append(raw2)
        contradicted = [c for c in (parsed2.get("contradicted") or []) if isinstance(c, dict)]
        verified = [c for c in contradicted
                    if quote_is_in(passage, str(c.get("passage_says", "")))]
        if contradicted and not verified:
            detail.append(
                "the explanation was reported as contradicted, but no supporting span was "
                "found in the passage, so that report was not counted")
        if not bool(parsed2.get("gives_a_reason", True)):
            checks.append(EXPLANATION_ASSERTS_WITHOUT_REASON)
            detail.append(
                "the explanation asserts the answer without giving a reason for it, so a "
                "learner who got the item wrong is told what to think and not why")
        for claim in verified:
            checks.append(EXPLANATION_CONTRADICTS_PASSAGE)
            detail.append(
                f"the explanation claims {str(claim.get('claim',''))[:160]!r}, which the "
                f"passage contradicts: {str(claim.get('passage_says',''))[:160]!r}")

    verdict = FLAGGED if checks else PASSED
    return GroundingResult(verdict, tuple(dict.fromkeys(checks)), tuple(detail),
                           tuple(supported), evidence, calls, tuple(raws))
