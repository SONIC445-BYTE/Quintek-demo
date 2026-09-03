"""
Layer C: an independent answer, from a model that had no hand in writing the item.

WHAT INDEPENDENCE MEANS HERE, AND WHY IT IS ENFORCED RATHER THAN INTENDED
------------------------------------------------------------------------
A model asked to check its own output agrees with it. That is not dishonesty,
it is the same weights producing the same answer twice, and the second run
carries no information the first did not. So this layer refuses to run when the
judge is the model that authored the item -- not by convention, by raising.

The judge is also not shown the key or the explanation. A judge told which
answer was intended will find a reason for it; withholding the intended answer
is the only way its agreement means anything.

WHAT IT ADDS OVER LAYER B
-------------------------
Layer B asks whether the supplied passage supports the key. That question is
checkable, which is its strength, and it is silent whenever the passage is thin
or the defect lives outside the passage -- a key that is wrong in medicine but
consistent with a sloppy paragraph passes Layer B cleanly.

This layer asks a different question: answer it yourself, and say whether more
than one option works. Disagreement between an independent answer and the key
is the single most informative signal available without a human, and it is the
one that costs the most, which is why it runs last and only on items the
cheaper layers did not already settle.

WHAT DISAGREEMENT IS AND IS NOT
-------------------------------
Disagreement is not proof the item is wrong. It is proof that the item and a
competent reader disagree, which is exactly the set a human should look at.
Treating it as a verdict rather than a referral is how a validator with a 10
per cent error rate silently deletes 10 per cent of good questions -- so a
low-confidence disagreement ABSTAINS instead of flagging, and says so.
"""

from __future__ import annotations

from dataclasses import dataclass

from benchmark.providers.base import GenerationRequest

from validator.grounding import (LETTERS, MAX_REPLY_TOKENS, extract_json, format_options,
                                 quote_is_in)
from validator.metrics import ABSTAINED, FLAGGED, PASSED

PROMPT_VERSION = "judge/0.1.0"

DISAGREES_WITH_KEY = "independent_answer_differs_from_key"
MULTIPLE_DEFENSIBLE = "judge_finds_more_than_one_defensible"
NOT_ANSWERABLE = "judge_cannot_answer_from_material"
LOW_CONFIDENCE = "judge_not_confident"

# Below this the judge's own answer is not evidence about the item.
CONFIDENCE_FLOOR = 0.6

SYSTEM = (
    "You are answering a single medical multiple-choice question. You have not seen the "
    "intended answer and you are not being asked to grade anyone. Answer it as you would in "
    "an examination, then say honestly how sure you are and whether more than one option "
    "works. Reply with one JSON object and nothing else."
)

PROMPT = """{passage_block}Question: {stem}

Options:
{options}

Answer the question. Then say whether any option OTHER than your answer is also defensible,
and how confident you are on a scale from 0 to 1.

Reply with one JSON object:
{{"answer": "A",
  "confidence": 0.0 to 1.0,
  "also_defensible": ["B"],
  "answerable": true or false,
  "reasoning": "one or two sentences"}}

Set "answerable" to false if the question cannot be answered as written -- for example if it
does not determine a single answer, or if it depends on information that is not here."""

PASSAGE_BLOCK = """Reference passage supplied with the question:
\"\"\"
{passage}
\"\"\"

"""


class JudgeUnavailable(RuntimeError):
    """The judge could not run, or could not be trusted to. Never a PASS."""


class JudgeNotIndependent(JudgeUnavailable):
    """The judge is the model that wrote the item."""


@dataclass(frozen=True)
class JudgeResult:
    verdict: str
    checks: tuple[str, ...] = ()
    detail: tuple[str, ...] = ()
    answer: str = ""
    confidence: float = 0.0
    also_defensible: tuple[str, ...] = ()
    calls: int = 0

    @property
    def ok(self) -> bool:
        return self.verdict == PASSED

    def as_dict(self) -> dict:
        return {"layer": "judge", "prompt_version": PROMPT_VERSION,
                "verdict": self.verdict, "checks": list(self.checks),
                "detail": list(self.detail), "answer": self.answer,
                "confidence": self.confidence,
                "also_defensible": list(self.also_defensible), "calls": self.calls}


def assert_independent(item: dict, provider) -> None:
    """
    Refuse a judge that wrote the item, or that shares its model family.

    Family matters as well as model id: two checkpoints of one family agree
    with each other far more than either agrees with a person, so a "second
    opinion" from a sibling model reports a smaller disagreement rate for
    reasons that have nothing to do with the item.
    """
    author_model = str(item.get("generated_by_model") or "").strip().lower()
    author_family = str(item.get("generated_by_family") or "").strip().lower()
    judge_model = str(getattr(provider, "model", "") or "").strip().lower()
    judge_family = str(getattr(provider, "model_family", "") or "").strip().lower()

    if author_model and judge_model and author_model == judge_model:
        raise JudgeNotIndependent(
            f"{item.get('id', 'item')}: {judge_model!r} wrote this item and cannot judge it. "
            "A model checking its own output agrees with itself, and that agreement is not "
            "evidence.")
    if author_family and judge_family and author_family == judge_family and author_family != "none":
        raise JudgeNotIndependent(
            f"{item.get('id', 'item')}: the judge and the author are both from the "
            f"{author_family!r} family. Sibling checkpoints agree with each other for reasons "
            "unrelated to whether the item is correct.")


def check(item: dict, provider, *, show_passage: bool = True,
          confidence_floor: float = CONFIDENCE_FLOOR) -> JudgeResult:
    """
    Ask an independent model to answer the item, and compare with the key.

    `show_passage` is deliberately a choice. Showing it measures whether the
    item is answerable from the material a learner is given; withholding it
    measures whether the key is right in medicine. Both are useful and they are
    not the same measurement, so the caller states which one it wants and the
    result records it.
    """
    assert_independent(item, provider)

    options = list(item.get("options") or [])
    key = item.get("correct_index")
    item_id = item.get("id", "item")
    passage = (item.get("source_passage") or "").strip()

    if not options or not isinstance(key, int) or isinstance(key, bool) \
            or not 0 <= key < len(options):
        raise JudgeUnavailable(
            f"{item_id}: the options or the key are malformed; that is Layer A's finding, not "
            "something to spend a model call on.")
    if len(options) > len(LETTERS):
        raise JudgeUnavailable(f"{item_id}: more options than this layer can label")

    block = PASSAGE_BLOCK.format(passage=passage) if (show_passage and passage) else ""
    request = GenerationRequest(
        item_id=f"{item_id}:judge", system=SYSTEM, temperature=0.0,
        max_tokens=MAX_REPLY_TOKENS,
        prompt=PROMPT.format(passage_block=block, stem=(item.get("stem") or "").strip(),
                             options=format_options(options)),
        metadata={"layer": "judge", "prompt_version": PROMPT_VERSION,
                  "passage_shown": bool(block)})
    response = provider.generate(request)
    if not response.ok:
        raise JudgeUnavailable(
            f"{item_id}: the judge backend failed ({response.error}). Nothing was judged, so "
            "nothing may be reported as judged.")
    parsed = extract_json(response.raw_output)
    if parsed is None:
        raise JudgeUnavailable(
            f"{item_id}: the judge returned no JSON object. An unparseable judge is an outage, "
            "not an agreement.")

    valid = LETTERS[:len(options)]
    answer = str(parsed.get("answer") or "").strip().upper()[:1]
    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(max(confidence, 0.0), 1.0)
    also = [str(a).strip().upper()[:1] for a in (parsed.get("also_defensible") or [])]
    also = [a for a in dict.fromkeys(also) if a in valid and a != answer]
    answerable = bool(parsed.get("answerable", True))
    key_letter = LETTERS[key]

    checks: list[str] = []
    detail: list[str] = []

    if answer not in valid:
        raise JudgeUnavailable(
            f"{item_id}: the judge answered {answer!r}, which is not one of the "
            f"{len(options)} options. A reply that does not answer the question asked is an "
            "outage, not a disagreement.")

    if not answerable:
        checks.append(NOT_ANSWERABLE)
        detail.append("the judge reports the question cannot be answered as written")
    if also:
        checks.append(MULTIPLE_DEFENSIBLE)
        detail.append(
            f"the judge answered {answer} and considers {', '.join(also)} also defensible, so "
            "the item may not have a single best answer")
    if answer != key_letter:
        checks.append(DISAGREES_WITH_KEY)
        detail.append(f"the judge answered {answer}; the item keys {key_letter}")

    if confidence < confidence_floor:
        # An unsure judge is not evidence in either direction. Say so and stop,
        # rather than converting an admission of uncertainty into a verdict.
        return JudgeResult(
            ABSTAINED, tuple(dict.fromkeys(checks + [LOW_CONFIDENCE])),
            tuple(detail + [f"the judge reported confidence {confidence:.2f}, below the "
                            f"{confidence_floor:.2f} floor, so its answer is not evidence "
                            "about this item in either direction"]),
            answer, confidence, tuple(also), 1)

    verdict = FLAGGED if checks else PASSED
    return JudgeResult(verdict, tuple(dict.fromkeys(checks)), tuple(detail),
                       answer, confidence, tuple(also), 1)
