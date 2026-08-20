"""
Can Quintek recognise a bad question?

Generation quality is the obvious thing to measure and the harder one to
measure honestly -- it needs expert gold. Rejection is measurable right now,
because the ground truth of a deliberately broken question is the way it was
broken, and that is true by construction rather than by authority.

It is also the more interesting property. A system that generates well but
approves everything has no validator; a system that generates unevenly but
reliably refuses the bad output is usable. The claim worth being able to make
is not "Quintek writes good questions" but:

    generation is not acceptance.

WHAT IS AND IS NOT MEASURED
---------------------------
Per defect class:

  * **detection rate** -- of the items broken in this way, how many did the
    validator flag? A validator that flags everything scores 100% here and is
    worthless, which is why the next figure is mandatory.

  * **false-flag rate** -- of the sound development items, how many were
    flagged anyway? A validator is only as good as the gap between these two.
    Reporting detection without it is the single easiest way to make a
    validator look good.

  * **reason quality** -- did the validator's failed checks actually
    correspond to the defect? Catching a hallucinated reference because the
    stem "seemed ambiguous" is a lucky guess, not detection, and over a small
    battery luck is common. `check_alignment` records whether the checks that
    failed are the ones that should have.

Sample sizes here are tiny (two items per class). Every figure is reported
with its n, and `interpretation` says plainly that these are indicative and
not gate-grade. Two out of two is not 100%.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .corpus import DEFECT_CLASSES, CorpusItem, load

# Which validator checks SHOULD fail for each defect class. Used to tell
# detection from a lucky guess -- not to score, and never to override the
# validator's own verdict.
EXPECTED_CHECKS: dict[str, tuple[str, ...]] = {
    "wrong_key": ("key_is_right", "factually_correct"),
    "two_correct": ("unambiguous", "key_is_right", "distractors_plausible"),
    "ambiguous_stem": ("unambiguous",),
    "hallucinated_fact": ("factually_correct", "no_unsupported_claims"),
    "hallucinated_reference": ("no_unsupported_claims", "factually_correct"),
    "out_of_syllabus": ("concept_aligned", "pg_level"),
    "poor_reasoning": ("no_unsupported_claims", "key_is_right"),
    "ungrounded": ("grounded_in_source", "no_unsupported_claims"),
    "giveaway": ("distractors_plausible", "pg_level"),
    "trivial": ("pg_level",),
}


@dataclass
class ItemOutcome:
    item_id: str
    defect_class: str
    expected_verdict: str          # "flag" for adversarial, "approve" for sound
    actual_verdict: str            # approved | flagged | skipped | error
    correct: bool
    failed_checks: list[str] = field(default_factory=list)
    check_alignment: str = ""      # aligned | misaligned | not_applicable
    issues: list[str] = field(default_factory=list)
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "item_id": self.item_id, "defect_class": self.defect_class,
            "expected_verdict": self.expected_verdict, "actual_verdict": self.actual_verdict,
            "correct": self.correct, "failed_checks": self.failed_checks,
            "check_alignment": self.check_alignment, "issues": self.issues,
            "error": self.error,
        }


def _alignment(defect_class: str, failed_checks: list[str]) -> str:
    """Did the checks that failed correspond to the actual defect?"""
    if not defect_class:
        return "not_applicable"
    if not failed_checks:
        return "misaligned"
    expected = set(EXPECTED_CHECKS.get(defect_class, ()))
    if not expected:
        return "not_applicable"
    return "aligned" if expected & set(failed_checks) else "misaligned"


class AdversarialRun:
    """
    Feeds corpus items through a validator and reports what it caught.

    `validate_fn(item) -> dict` is injected rather than importing the
    validator directly, so the same run works against the student engine's
    QuestionValidator, a scripted double, or any future validator without
    this module knowing which.
    """

    def __init__(self, validate_fn):
        self.validate_fn = validate_fn

    def _one(self, item: CorpusItem) -> ItemOutcome:
        expected = "flag" if item.is_negative else "approve"
        try:
            result = self.validate_fn(item)
        except Exception as exc:
            # An error is not a detection. Counting a crash as a catch would
            # make a broken validator look vigilant.
            return ItemOutcome(item.id, item.defect_class, expected, "error",
                               correct=False, error=f"{type(exc).__name__}: {exc}")

        status = result.get("status", "error")
        failed = list(result.get("failed_checks") or [])
        correct = (status == "flagged") if item.is_negative else (status == "approved")
        return ItemOutcome(
            item.id, item.defect_class, expected, status, correct=correct,
            failed_checks=failed, check_alignment=_alignment(item.defect_class, failed),
            issues=[str(i) for i in (result.get("issues") or [])])

    def run(self, adversarial: list[CorpusItem],
            sound: list[CorpusItem] | None = None) -> dict:
        """
        Run the battery. `sound` items are the control arm.

        The control arm is not optional in spirit: without it, "detection
        rate" is uninterpretable, because flagging everything scores perfectly.
        It is optional in code only so a caller can measure the two halves
        separately and merge the reports.
        """
        adversarial_outcomes = [self._one(i) for i in adversarial]
        sound_outcomes = [self._one(i) for i in (sound or [])]

        by_class: dict[str, list[ItemOutcome]] = defaultdict(list)
        for outcome in adversarial_outcomes:
            by_class[outcome.defect_class].append(outcome)

        per_class = {}
        for defect in DEFECT_CLASSES:
            outcomes = by_class.get(defect, [])
            if not outcomes:
                per_class[defect] = {"n": 0, "detected": 0, "detection_rate": None,
                                     "aligned": 0, "note": "not exercised by this battery"}
                continue
            detected = sum(1 for o in outcomes if o.correct)
            per_class[defect] = {
                "n": len(outcomes),
                "detected": detected,
                "detection_rate": detected / len(outcomes),
                # Of the ones caught, how many were caught for the right reason.
                "aligned": sum(1 for o in outcomes
                               if o.correct and o.check_alignment == "aligned"),
                "errors": sum(1 for o in outcomes if o.actual_verdict == "error"),
            }

        detected_total = sum(1 for o in adversarial_outcomes if o.correct)
        aligned_total = sum(1 for o in adversarial_outcomes
                            if o.correct and o.check_alignment == "aligned")
        false_flags = sum(1 for o in sound_outcomes if not o.correct
                          and o.actual_verdict == "flagged")

        n_adv, n_sound = len(adversarial_outcomes), len(sound_outcomes)
        detection_rate = detected_total / n_adv if n_adv else None
        false_flag_rate = false_flags / n_sound if n_sound else None

        return {
            "adversarial_n": n_adv,
            "detected": detected_total,
            "detection_rate": detection_rate,
            "detected_for_the_right_reason": aligned_total,
            "control_n": n_sound,
            "false_flags": false_flags,
            "false_flag_rate": false_flag_rate,
            "per_defect_class": per_class,
            "outcomes": [o.as_dict() for o in adversarial_outcomes],
            "control_outcomes": [o.as_dict() for o in sound_outcomes],
            "interpretation": _interpret(detection_rate, false_flag_rate, aligned_total,
                                         detected_total, n_adv, n_sound),
        }


def _interpret(detection_rate, false_flag_rate, aligned, detected, n_adv, n_sound) -> str:
    """
    Prose that refuses to overclaim from a small battery.

    Written here rather than left to the reader because "100% detection" over
    twenty items is exactly the sentence that gets quoted without its n.
    """
    if not n_adv:
        return "No adversarial items were run, so nothing was measured."

    parts = [f"{detected}/{n_adv} deliberately broken questions were flagged"]
    if n_sound:
        parts.append(f"and {n_sound - (false_flag_rate or 0) * n_sound:.0f}/{n_sound} sound "
                     f"questions were left approved")
    else:
        parts.append("but no sound control items were run, so this figure cannot distinguish "
                     "a working validator from one that flags everything")

    text = ", ".join(parts) + "."

    if detected and aligned < detected:
        text += (f" {detected - aligned} of the catches did not cite a check corresponding to "
                 "the actual defect, so they may be coincidental rather than detection.")

    text += (f" Sample size is {n_adv} adversarial items; this is indicative only and is not "
             "gate-grade evidence. A rate computed over two items per defect class carries an "
             "interval wide enough to include most values of interest.")
    if false_flag_rate:
        text += (f" A false-flag rate of {false_flag_rate:.0%} means sound questions are also "
                 "being rejected, which costs the learner content.")
    return text


def load_battery(corpus_dir: str | Path = "corpus") -> tuple[list[CorpusItem], list[CorpusItem]]:
    """The shipped adversarial items, and the sound items that control them."""
    corpus_dir = Path(corpus_dir)
    adversarial = load(corpus_dir / "adversarial.jsonl")
    sound = [i for i in load(corpus_dir / "development.jsonl") if not i.is_negative]
    return adversarial, sound


def write_report(report: dict, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return path
