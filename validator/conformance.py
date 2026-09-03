"""
Layer D: did the generator produce what it was asked for?

A DEFECT THAT CANNOT BE SEEN WITHOUT THE REQUEST
------------------------------------------------
Three of the ten defect classes are invisible to every other layer, and for the
same reason: nothing about the item alone is wrong.

    out_of_syllabus   a correct, well-grounded question about the wrong concept
    trivial           a correct question well below the difficulty asked for
    giveaway          a correct question answerable from its own wording

Layer A sees a well-formed item. Layer B sees a key the passage supports. Layer
C sees a question it answers confidently and correctly. All three are right, and
the item is still not the one the learner asked for. The missing input is the
REQUEST -- the concept, the difficulty and the question type that were specified
before generation -- which is why this layer takes them explicitly rather than
inferring them.

WHAT WAS TRIED FIRST, AND WHY IT IS NOT HERE
--------------------------------------------
A deterministic version of the giveaway check -- a distinctive word shared
between the stem and the keyed option and absent from every distractor -- was
built and measured against this corpus first, because a free check with no false
flags beats a model call every time. It flagged 5 of 40 clean items and caught
2 of 4 planted giveaways. That is worse than useless: it rejects good questions
at eight times the rate it catches bad ones. Deterministic word overlap cannot
distinguish a cue from a topic, so the check was removed rather than tuned, and
the question is asked of a model here instead, where it can be phrased as
"could this be answered without medical knowledge" rather than as string
matching.

The cue the model offers must still be quoted from the question, and is checked.
"""

from __future__ import annotations

from dataclasses import dataclass

from benchmark.providers.base import GenerationRequest

from validator.grounding import (MAX_REPLY_TOKENS, extract_json, format_options,
                                 quote_is_in)
from validator.metrics import ABSTAINED, FLAGGED, PASSED

PROMPT_VERSION = "conformance/0.1.0"

OFF_CONCEPT = "tests_a_different_concept"
BELOW_DECLARED_DIFFICULTY = "below_declared_difficulty"
ANSWERABLE_FROM_WORDING = "answerable_from_wording_alone"
CUE_NOT_IN_QUESTION = "cue_not_found_in_question"
#: The cue lies inside the keyed option. Circular: every key appears in its own
#: options, so this condition is satisfiable for any item whatsoever and proves
#: nothing about wording.
CUE_IS_THE_KEY = "cue_is_the_keyed_answer"
#: The cue appears only in a distractor. A wording that occurs solely in a wrong
#: option cannot select the RIGHT one, which is what the check claims.
CUE_ONLY_IN_DISTRACTOR = "cue_only_in_a_distractor"
#: The cue is genuinely in the stem, and nothing deterministic here can
#: establish that it hands over the key. Reported as unevaluable rather than
#: guessed.
CUE_NOT_EVALUABLE = "wording_giveaway_not_evaluable"

RECALL = "recall"
APPLICATION = "application"
ANALYSIS = "analysis"
LEVELS = (RECALL, APPLICATION, ANALYSIS)

# Which declared difficulties tolerate a recall-only question. `foundation` is
# meant to be recall; the two postgraduate levels are not.
RECALL_IS_ACCEPTABLE_AT = ("foundation",)

SYSTEM = (
    "You are checking whether a multiple-choice question matches the specification it was "
    "written to. You are not judging whether it is correct or medically sound; another check "
    "does that. Reply with one JSON object and nothing else."
)

PROMPT = """A question was requested with this specification:

  concept:     {concept}
  difficulty:  {difficulty}
  type:        {question_type}

This is the question that was produced:

Question: {stem}

Options:
{options}

Answer three things.

1. Which concept does the question actually test? Does it test the concept that was requested?
   A question can be correct, well written and about the wrong thing.

2. What cognitive level does it demand: "recall" (retrieve a stated fact), "application" (use a
   principle in a situation) or "analysis" (compare, infer or evaluate)?

3. Could a candidate with no knowledge of the subject choose the right option from the wording
   alone -- because a word in the question points at one option, or because the other options are
   obviously not answers to the question asked? If so, quote the wording that gives it away,
   copied exactly from the question or the options.

Reply with one JSON object:
{{"concept_tested": "...",
  "matches_requested_concept": true or false,
  "cognitive_level": "recall" or "application" or "analysis",
  "answerable_from_wording_alone": true or false,
  "wording_cue": "copied exactly, or empty",
  "reasoning": "one or two sentences"}}"""


class ConformanceUnavailable(RuntimeError):
    """The layer could not run. Never a PASS."""


@dataclass(frozen=True)
class ConformanceResult:
    verdict: str
    checks: tuple[str, ...] = ()
    detail: tuple[str, ...] = ()
    concept_tested: str = ""
    cognitive_level: str = ""
    calls: int = 0

    @property
    def ok(self) -> bool:
        return self.verdict == PASSED

    def as_dict(self) -> dict:
        return {"layer": "conformance", "prompt_version": PROMPT_VERSION,
                "verdict": self.verdict, "checks": list(self.checks),
                "detail": list(self.detail), "concept_tested": self.concept_tested,
                "cognitive_level": self.cognitive_level, "calls": self.calls}


def check(item: dict, provider, *,
          recall_acceptable_at: tuple[str, ...] = RECALL_IS_ACCEPTABLE_AT) -> ConformanceResult:
    """
    Compare one item against the specification recorded on it.

    The specification is the item's own declared concept, difficulty and type.
    In production those come from the generation request; on a corpus they are
    the fields, which is the same claim written down earlier.
    """
    item_id = item.get("id", "item")
    concept = (item.get("concept") or "").strip()
    difficulty = (item.get("difficulty") or "").strip()
    options = list(item.get("options") or [])
    stem = (item.get("stem") or "").strip()

    if not concept or not difficulty:
        raise ConformanceUnavailable(
            f"{item_id}: no declared concept or difficulty. This layer compares the item "
            "against what was asked for; with nothing asked for there is nothing to compare "
            "against, and passing it silently would report an unchecked item as conforming.")
    if not options or not stem:
        raise ConformanceUnavailable(
            f"{item_id}: the item has no stem or no options; that is Layer A's finding.")

    request = GenerationRequest(
        item_id=f"{item_id}:conformance", system=SYSTEM, temperature=0.0,
        max_tokens=MAX_REPLY_TOKENS,
        prompt=PROMPT.format(concept=concept, difficulty=difficulty,
                             question_type=(item.get("question_type") or "").strip() or "mcq",
                             stem=stem, options=format_options(options)),
        metadata={"layer": "conformance", "prompt_version": PROMPT_VERSION})
    response = provider.generate(request)
    if not response.ok:
        raise ConformanceUnavailable(
            f"{item_id}: the conformance backend failed ({response.error}). Nothing was "
            "checked, so nothing may be reported as checked.")
    parsed = extract_json(response.raw_output)
    if parsed is None:
        raise ConformanceUnavailable(
            f"{item_id}: the conformance backend returned no JSON object. An unparseable "
            "check is an outage, not a conforming item.")

    checks: list[str] = []
    detail: list[str] = []

    concept_tested = str(parsed.get("concept_tested") or "").strip()
    if not bool(parsed.get("matches_requested_concept", True)):
        checks.append(OFF_CONCEPT)
        detail.append(
            f"the item declares the concept {concept!r} but tests "
            f"{concept_tested or 'something else'}; it may be a correct question about the "
            "wrong thing")

    level = str(parsed.get("cognitive_level") or "").strip().lower()
    if level not in LEVELS:
        level = ""
    if level == RECALL and difficulty not in recall_acceptable_at:
        checks.append(BELOW_DECLARED_DIFFICULTY)
        detail.append(
            f"the item is declared {difficulty!r} but demands only recall of a stated fact")

    if bool(parsed.get("answerable_from_wording_alone", False)):
        # THE CLAIM IS A RELATION, NOT A PRESENCE.
        #
        # This check asserts that a wording "selects the keyed option without
        # any knowledge of the subject". It used to verify only that the cue
        # occurred somewhere in `stem + all options`, which is a much weaker
        # and sometimes contradictory test. Measured over Phase 0's journal,
        # all eight flags on clean items failed the claim they asserted:
        #
        #   2  the cue lay ONLY in a distractor -- text in a wrong option
        #      cannot select the right one
        #   2  the cue WAS the keyed option -- circular, and satisfiable for
        #      any item, since every key appears in its own options
        #   4  the cue was stem text absent from the key, so the giveaway was
        #      asserted rather than shown
        #
        # Each of those is decided from the question text alone: where a cue
        # lies is a string fact needing no subject knowledge. So they are
        # separated here and reported by name.
        #
        # What remains -- whether a stem cue genuinely hands over the answer --
        # is a judgement, and this module has no deterministic rule for it.
        # Inventing one (say, lexical overlap with the key and not the
        # distractors) would be a new heuristic asserting the same
        # unestablished claim. So the honest outcome is ABSTAINED: the layer
        # records that it could not evaluate the giveaway, and the item is
        # excluded from both rates rather than counted as a finding.
        cue = str(parsed.get("wording_cue") or "")
        # This layer does not otherwise need the key -- it checks an item
        # against its own declared specification, not against its answer. It is
        # read here for one purpose: to tell the keyed option apart from the
        # distractors, which is what separates a circular cue from a
        # contradictory one. A malformed key leaves both empty, and the branch
        # below then falls through to the stem test, which is the conservative
        # direction.
        key_index = item.get("correct_index")
        keyed = (isinstance(key_index, int) and not isinstance(key_index, bool)
                 and 0 <= key_index < len(options))
        key_text = str(options[key_index]) if keyed else ""
        distractors = ([str(o) for i, o in enumerate(options) if i != key_index]
                       if keyed else [])

        in_stem = quote_is_in(stem, cue)
        in_key = quote_is_in(key_text, cue)
        in_distractor = any(quote_is_in(d, cue) for d in distractors)

        if in_key:
            reason, note = CUE_IS_THE_KEY, (
                "a giveaway was reported but the wording offered as the cue is the keyed "
                "answer itself. Every key appears in its own options, so this shows "
                "nothing about the question's wording")
        elif in_distractor and not in_stem:
            reason, note = CUE_ONLY_IN_DISTRACTOR, (
                "a giveaway was reported but the wording offered as the cue appears only "
                "in a distractor, and wording that occurs solely in a wrong option cannot "
                "select the right one")
        elif not in_stem:
            reason, note = CUE_NOT_IN_QUESTION, (
                "a giveaway was reported but the wording offered as the cue does not "
                "appear in the question, so this run produced no usable evidence about it")
        else:
            # Grounded in the stem, and neither circular nor contradictory.
            # This is the case the corpus's own planted giveaways use -- see
            # `vd-def-009`, whose defect_note reads "The stem now contains the
            # word 'caseating', which appears in no other option". So it is
            # reported, and the wording of the finding says only what was
            # actually established: the cue is in the stem. Whether it hands
            # over the key is the item-writer's judgement, and the detail no
            # longer claims this layer proved it.
            checks.append(ANSWERABLE_FROM_WORDING)
            detail.append(
                f"the stem carries the wording {cue.strip()[:120]!r}, offered as a cue that "
                "points to the keyed option without knowledge of the subject. The cue is "
                "confirmed present in the stem and absent from the key; whether it is "
                "sufficient to answer on is not established here")
            reason = None

        if reason is not None:
            return ConformanceResult(
                ABSTAINED, tuple(dict.fromkeys(checks + [reason])),
                tuple(detail + [note]), concept_tested, level, 1)

    verdict = FLAGGED if checks else PASSED
    return ConformanceResult(verdict, tuple(dict.fromkeys(checks)), tuple(detail),
                             concept_tested, level, 1)
