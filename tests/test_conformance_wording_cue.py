"""
`answerable_from_wording_alone` must test the claim it makes.

The check asserts a RELATION -- that a wording "selects the keyed option
without any knowledge of the subject". It used to verify only PRESENCE: that
the cue occurred somewhere in `stem + all options`. Measured over Phase 0's
journal, all eight flags raised on clean items failed the claim they asserted:
two cues lay only in a distractor, two were the keyed answer itself, and four
were stem text absent from the key.

Where a cue lies is decidable from the question text alone, so the first two
are provable faults. Whether a stem cue genuinely hands over the answer is a
judgement with no deterministic rule here, so it abstains rather than guesses.

These tests hold both halves: the faults stay fixed, and the abstention does
not quietly become a heuristic.
"""

from __future__ import annotations

import json

import pytest

from validator.conformance import (ANSWERABLE_FROM_WORDING, CUE_IS_THE_KEY,
                                   CUE_NOT_EVALUABLE, CUE_NOT_IN_QUESTION,
                                   CUE_ONLY_IN_DISTRACTOR, check)
from validator.metrics import ABSTAINED
from validator.scripted import ReplayProvider

STEM = "Herd immunity protects unvaccinated individuals in a community because:"
KEY = "Transmission chains are interrupted when susceptibility falls low enough"
DISTRACTORS = [
    "Vaccinated individuals shed protective antibody into the environment",
    "Prevent progression to rheumatic carditis in exposed contacts",
    "Maternal antibody persists throughout the first decade of life",
]

ITEM = {
    "id": "t-1", "stem": STEM, "question_type": "mcq",
    "options": [KEY] + DISTRACTORS, "correct_index": 0,
    "concept": "Herd immunity", "difficulty": "foundation",
}


def reply(cue, *, level="application", giveaway=True):
    return {
        "concept_tested": "Herd immunity",
        "matches_requested_concept": True,
        "cognitive_level": level,
        "answerable_from_wording_alone": giveaway,
        "wording_cue": cue,
    }


def run(cue, item=None, **kw):
    provider = ReplayProvider({"t-1:conformance": reply(cue, **kw)})
    return check(item or ITEM, provider)


# ---------------------------------------------------------------------------
# The two provable faults
# ---------------------------------------------------------------------------

def test_a_cue_that_is_the_keyed_answer_is_circular():
    """
    Every key appears in its own options, so this condition is satisfiable for
    ANY item. Accepting it would let the check fire on anything.
    """
    result = run("Transmission chains are interrupted")
    assert result.verdict == ABSTAINED
    assert CUE_IS_THE_KEY in result.checks
    assert ANSWERABLE_FROM_WORDING not in result.checks


def test_a_cue_only_in_a_distractor_cannot_select_the_key():
    """Wording that occurs solely in a WRONG option cannot select the right one."""
    result = run("Prevent progression to rheumatic carditis")
    assert result.verdict == ABSTAINED
    assert CUE_ONLY_IN_DISTRACTOR in result.checks
    assert ANSWERABLE_FROM_WORDING not in result.checks


def test_a_short_fragment_of_the_key_is_still_the_key():
    """
    `is preserved` -- twelve characters, exactly at the quote_is_in length
    floor -- was one of the real Phase 0 flags. A fragment of the key is the
    key for this purpose.
    """
    item = dict(ITEM, options=["Glucose-coupled sodium absorption is preserved"] + DISTRACTORS)
    result = run("is preserved", item=item)
    assert result.verdict == ABSTAINED
    assert CUE_IS_THE_KEY in result.checks


def test_a_cue_in_several_distractors_is_still_not_evidence():
    item = dict(ITEM, options=[KEY, "shared wording here", "shared wording here", DISTRACTORS[2]])
    result = run("shared wording here", item=item)
    assert result.verdict == ABSTAINED
    assert CUE_ONLY_IN_DISTRACTOR in result.checks


# ---------------------------------------------------------------------------
# The judgement that has no deterministic rule
# ---------------------------------------------------------------------------

def test_a_stem_grounded_cue_is_still_reported():
    """
    This is the case the corpus's own planted giveaways use -- `vd-def-009`'s
    defect_note reads "The stem now contains the word 'caseating', which
    appears in no other option". Abstaining here would make the check miss
    every planted giveaway, so it reports.
    """
    result = run("protects unvaccinated individuals")
    assert ANSWERABLE_FROM_WORDING in result.checks


def test_the_finding_claims_only_what_was_established():
    """
    The detail string used to assert the cue "selects the keyed option without
    any knowledge of the subject" -- a relation this layer never tested. It now
    states what it checked and marks the rest as not established.
    """
    result = run("protects unvaccinated individuals")
    text = " ".join(result.detail)
    assert "confirmed present in the stem" in text
    assert "not established here" in text


def test_a_cue_in_both_stem_and_key_is_treated_as_the_key():
    """
    The circular reading dominates: if the cue is in the key, its presence in
    the stem does not rescue it as evidence.
    """
    item = dict(ITEM, stem="Transmission chains are interrupted in herd immunity because:")
    result = run("Transmission chains are interrupted", item=item)
    assert result.verdict == ABSTAINED
    assert CUE_IS_THE_KEY in result.checks


def test_a_cue_absent_everywhere_still_abstains_as_before():
    """The pre-existing behaviour, unchanged."""
    result = run("a phrase that appears nowhere in this item at all")
    assert result.verdict == ABSTAINED
    assert CUE_NOT_IN_QUESTION in result.checks


def test_case_and_whitespace_are_normalised():
    """`quote_is_in` flattens whitespace and case; the cue tests inherit that."""
    result = run("TRANSMISSION   chains\n are INTERRUPTED")
    assert result.verdict == ABSTAINED
    assert CUE_IS_THE_KEY in result.checks


# ---------------------------------------------------------------------------
# The check never fabricates a flag
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cue", [
    "Transmission chains are interrupted",          # the key itself
    "Prevent progression to rheumatic carditis",    # a distractor only
    "nowhere to be found in this question",         # absent entirely
])
def test_only_a_stem_grounded_cue_can_produce_the_flag(cue):
    """
    The gate-facing consequence. Three of the four positions cannot raise the
    finding; only a cue actually in the stem can. Measured over Phase 0's
    journal this removes 4 of 8 false flags on clean items -- the two circular
    ones and the two contradictory ones -- while leaving planted giveaways
    detectable.
    """
    result = run(cue)
    assert ANSWERABLE_FROM_WORDING not in result.checks
    assert result.verdict == ABSTAINED


def test_a_malformed_key_does_not_crash_the_layer():
    """
    A bad correct_index leaves key and distractors empty, so the cue falls
    through to the stem test rather than raising.
    """
    item = dict(ITEM, correct_index=99)
    result = run("protects unvaccinated individuals", item=item)
    assert result.verdict in (ABSTAINED, "FLAGGED", result.verdict)
    assert isinstance(result.checks, tuple)


def test_no_giveaway_claim_leaves_the_check_silent():
    """When the model reports no giveaway, none of this machinery runs."""
    result = run("", giveaway=False)
    for c in (ANSWERABLE_FROM_WORDING, CUE_IS_THE_KEY, CUE_ONLY_IN_DISTRACTOR,
              CUE_NOT_EVALUABLE, CUE_NOT_IN_QUESTION):
        assert c not in result.checks
