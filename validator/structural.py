"""
Layer A: what an LLM has no business judging.

Whether a question has four options, whether two of them are identical,
whether the keyed index points at an option that exists -- these are facts
about a data structure. Asking a language model costs a network round trip,
introduces a false-flag rate, and returns a probabilistic answer to a
deterministic question.

The measured 8B validator false-flagged 9 of 10 clean items. Every check in
this file has a false-flag rate of exactly zero, because it is arithmetic.
Anything that can be decided here must not reach a model.

Each finding names the CHECK that produced it, so a downstream confusion
matrix can attribute a flag to a layer rather than to "the validator".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Check ids. Stable strings: they appear in reports and in error analysis, and
# renaming one silently rewrites history.
MISSING_FIELD = "missing_field"
NO_OPTIONS = "no_options"
TOO_FEW_OPTIONS = "too_few_options"
TOO_MANY_OPTIONS = "too_many_options"
KEY_OUT_OF_RANGE = "key_out_of_range"
DUPLICATE_OPTIONS = "duplicate_options"
EMPTY_OPTION = "empty_option"
EMPTY_STEM = "empty_stem"
NO_EXPLANATION = "no_explanation"
NO_SOURCE_PASSAGE = "no_source_passage"
NO_REFERENCE = "no_reference"
UNKNOWN_QUESTION_TYPE = "unknown_question_type"
ANSWER_LEAKED_IN_STEM = "answer_leaked_in_stem"
ALL_OF_THE_ABOVE = "all_of_the_above"
OPTION_LENGTH_TELL = "option_length_tell"
UNVERIFIABLE_REFERENCE = "unverifiable_reference"

# The minimum an MCQ needs to be answerable at all. Four is the convention in
# this product; three is accepted because a true/false-style discrimination
# item is legitimate, and five is the practical ceiling on a phone.
MIN_OPTIONS = 3
MAX_OPTIONS = 6

REQUIRED_FIELDS = ("stem", "options", "correct_index", "explanation")

# Options that make a question unanswerable as a discrimination task. Not a
# style opinion: "all of the above" changes what is being tested and is
# excluded by the question-type contract rather than by taste.
DEGENERATE_OPTIONS = ("all of the above", "none of the above",
                      "both a and b", "all of these", "none of these")

# A citation precise enough to be checked, to a work this system does not hold.
#
# The product's evidence is the passage the learner uploaded. A reference that
# points somewhere else AND carries an exact locator -- page 3122, Table 402-4,
# Recommendation 4.2 -- is asserting something no part of this system can
# substantiate, and a fabricated locator is indistinguishable from a real one
# to everybody downstream, including the learner.
#
# The rule is deterministic; whether it is the right rule is a product
# decision, and it is one. An honest book-level citation is unaffected. A
# genuine page citation checked by a named human is unaffected, because
# `reviewed_by` exempts the item -- a person vouching for a locator is exactly
# the evidence this check is missing.
LOCATOR_PATTERN = re.compile(
    r"\b(?:p{1,2}\.|page|pages|table|fig\.|figure|chapter|chap\.|section|sec\.|box|"
    r"recommendation|appendix|paragraph|para\.)\s*\d",
    re.IGNORECASE)


@dataclass(frozen=True)
class Finding:
    check: str
    detail: str
    # Whether this alone makes the item unusable. A `fatal` finding means the
    # item cannot be shown to a learner; a non-fatal one is a quality signal.
    fatal: bool = True

    def as_dict(self) -> dict:
        return {"check": self.check, "detail": self.detail, "fatal": self.fatal}


@dataclass(frozen=True)
class StructuralResult:
    findings: tuple[Finding, ...] = ()
    checks_run: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not any(f.fatal for f in self.findings)

    @property
    def failed_checks(self) -> list[str]:
        return [f.check for f in self.findings]

    def as_dict(self) -> dict:
        return {"ok": self.ok, "findings": [f.as_dict() for f in self.findings],
                "checks_run": list(self.checks_run),
                "layer": "structural",
                "note": "Deterministic. No model was consulted, and these "
                        "findings have no false-flag rate."}


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _normalise_option(text: str) -> str:
    """
    Normalise an option for comparison WITHOUT discarding arithmetic operators.

    `_normalise` strips every non-alphanumeric character, which is right for
    prose. Applied to a formula it is catastrophic: "Na - (Cl + HCO3)" and
    "(Na + Cl) - HCO3" both reduce to "na cl hco3", and the duplicate-option
    check then reports two different formulae as the same answer written twice.
    That was a real false flag, on a real item, from a check whose whole claim
    is that it has no false-flag rate.

    Word-internal hyphens are still treated as spaces, so "well-controlled" and
    "well controlled" continue to compare equal; a hyphen with a space or a
    digit beside it is kept, because there it is a minus sign.
    """
    text = (text or "").lower()
    text = re.sub(r"(?<=[a-z])-(?=[a-z])", " ", text)
    text = re.sub(r"[^a-z0-9+\-*/=<>%().]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def check(item: dict, *, question_types: tuple[str, ...] = (),
          require_source: bool = True,
          require_verifiable_reference: bool = True) -> StructuralResult:
    """
    Run every deterministic check over one question.

    `item` is a plain dict so this layer stays usable on a raw model reply,
    before anything has been parsed into a corpus object -- which is where the
    malformed cases actually appear.
    """
    findings: list[Finding] = []
    run: list[str] = []

    def record(check_id: str, detail: str, fatal: bool = True):
        findings.append(Finding(check_id, detail, fatal))

    # ---- required fields -------------------------------------------------
    run.append(MISSING_FIELD)
    for field_name in REQUIRED_FIELDS:
        if field_name not in item or item.get(field_name) in (None, ""):
            record(MISSING_FIELD, f"{field_name!r} is missing or empty")

    stem = (item.get("stem") or "").strip()
    options = item.get("options") or []
    correct_index = item.get("correct_index")

    run.append(EMPTY_STEM)
    if not stem:
        record(EMPTY_STEM, "the stem is empty, so there is no question")

    # ---- options ---------------------------------------------------------
    run.extend([NO_OPTIONS, TOO_FEW_OPTIONS, TOO_MANY_OPTIONS, EMPTY_OPTION,
                DUPLICATE_OPTIONS, ALL_OF_THE_ABOVE])
    if not isinstance(options, list) or not options:
        record(NO_OPTIONS, "there are no options to choose between")
    else:
        if len(options) < MIN_OPTIONS:
            record(TOO_FEW_OPTIONS,
                   f"{len(options)} option(s); at least {MIN_OPTIONS} are needed")
        if len(options) > MAX_OPTIONS:
            record(TOO_MANY_OPTIONS,
                   f"{len(options)} options; more than {MAX_OPTIONS} is unreadable "
                   "on a phone", fatal=False)

        for position, option in enumerate(options):
            if not str(option).strip():
                record(EMPTY_OPTION, f"option {position} is empty")

        seen: dict[str, int] = {}
        for position, option in enumerate(options):
            key = _normalise_option(str(option))
            if key and key in seen:
                record(DUPLICATE_OPTIONS,
                       f"options {seen[key]} and {position} are the same answer "
                       "written twice, so one of them cannot be wrong")
            seen.setdefault(key, position)

        for position, option in enumerate(options):
            if _normalise(str(option)) in [_normalise(d) for d in DEGENERATE_OPTIONS]:
                record(ALL_OF_THE_ABOVE,
                       f"option {position} is {str(option)!r}, which changes the "
                       "task from discrimination to elimination")

    # ---- the key ---------------------------------------------------------
    run.append(KEY_OUT_OF_RANGE)
    if isinstance(options, list) and options:
        if not isinstance(correct_index, int) or isinstance(correct_index, bool):
            record(KEY_OUT_OF_RANGE,
                   f"correct_index is {correct_index!r}, which is not an index")
        elif not 0 <= correct_index < len(options):
            record(KEY_OUT_OF_RANGE,
                   f"correct_index {correct_index} does not point at one of the "
                   f"{len(options)} options, so the question has no answer")

    # ---- provenance ------------------------------------------------------
    run.append(NO_EXPLANATION)
    if not (item.get("explanation") or "").strip():
        record(NO_EXPLANATION,
               "no explanation, so a learner who gets it wrong learns nothing")

    if require_source:
        run.extend([NO_SOURCE_PASSAGE, NO_REFERENCE])
        if not (item.get("source_passage") or "").strip():
            record(NO_SOURCE_PASSAGE,
                   "no source passage, so grounding cannot be checked by anyone")
        if not (item.get("reference") or "").strip():
            record(NO_REFERENCE,
                   "no reference, so the learner cannot be shown where this came "
                   "from", fatal=False)

    if require_verifiable_reference:
        run.append(UNVERIFIABLE_REFERENCE)
        reference = (item.get("reference") or "").strip()
        vouched = bool((item.get("reviewed_by") or "").strip())
        if reference and not vouched:
            match = LOCATOR_PATTERN.search(reference)
            if match:
                record(UNVERIFIABLE_REFERENCE,
                       f"the reference cites {match.group(0).strip()!r} in a work this item "
                       "does not supply, and nothing here can confirm the cited page says "
                       "what the item claims. Cite the supplied passage, or have a named "
                       "reviewer vouch for the locator.")

    # ---- question type ---------------------------------------------------
    if question_types:
        run.append(UNKNOWN_QUESTION_TYPE)
        declared = item.get("question_type")
        if declared and declared not in question_types:
            record(UNKNOWN_QUESTION_TYPE,
                   f"{declared!r} is not one of {', '.join(question_types)}")

    # ---- giveaways -------------------------------------------------------
    run.extend([ANSWER_LEAKED_IN_STEM, OPTION_LENGTH_TELL])
    if stem and isinstance(options, list) and isinstance(correct_index, int) \
            and not isinstance(correct_index, bool) \
            and 0 <= correct_index < len(options):
        answer = str(options[correct_index])
        normalised_answer = _normalise(answer)
        # Only a substantial answer can leak. A one-word option appearing in
        # the stem is ordinary phrasing, not a giveaway.
        if len(normalised_answer.split()) >= 4 and normalised_answer in _normalise(stem):
            record(ANSWER_LEAKED_IN_STEM,
                   "the keyed answer appears verbatim in the stem", fatal=False)

        lengths = [len(str(o)) for o in options]
        if len(lengths) >= 3 and max(lengths) > 0:
            longest = lengths.index(max(lengths))
            # A correct option far longer than every other is the oldest tell
            # in multiple choice. Non-fatal: sometimes the right answer needs
            # the words.
            others = sorted(lengths)[:-1]
            if longest == correct_index and others and max(lengths) > 2 * (
                    sum(others) / len(others)):
                record(OPTION_LENGTH_TELL,
                       "the keyed option is more than twice the average length of "
                       "the others, which is answerable without reading them",
                       fatal=False)

    return StructuralResult(tuple(findings), tuple(dict.fromkeys(run)))
