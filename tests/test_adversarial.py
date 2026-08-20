"""
Tests for the adversarial battery.

The battery's job is to make a claim that is easy to fake, hard to fake
honestly. So most of these tests are about the ways a validator could look
good without being good, and whether the report exposes them.
"""

from __future__ import annotations

import pytest

from benchmark.adversarial import EXPECTED_CHECKS, AdversarialRun, load_battery
from benchmark.corpus import DEFECT_CLASSES, load


@pytest.fixture(scope="module")
def battery():
    return load_battery()


def flag_everything(_item):
    return {"status": "flagged", "failed_checks": ["unambiguous"], "issues": ["bad"]}


def approve_everything(_item):
    return {"status": "approved", "failed_checks": [], "issues": []}


def perfect(item):
    """Flags exactly the broken ones, citing the right checks."""
    if not item.defect_class:
        return {"status": "approved", "failed_checks": [], "issues": []}
    return {"status": "flagged",
            "failed_checks": list(EXPECTED_CHECKS.get(item.defect_class, ("unambiguous",))),
            "issues": [item.defect_note]}


# ---------- the degenerate validators ----------

def test_a_validator_that_flags_everything_is_not_reported_as_good(battery):
    adversarial, sound = battery
    report = AdversarialRun(flag_everything).run(adversarial, sound)

    assert report["detection_rate"] == 1.0
    # ...and the figure that gives it away.
    assert report["false_flag_rate"] == 1.0
    assert "sound questions are also being rejected" in report["interpretation"]


def test_a_validator_that_approves_everything_detects_nothing(battery):
    adversarial, sound = battery
    report = AdversarialRun(approve_everything).run(adversarial, sound)
    assert report["detection_rate"] == 0.0
    assert report["false_flag_rate"] == 0.0


def test_a_perfect_validator_scores_perfectly_on_both_arms(battery):
    adversarial, sound = battery
    report = AdversarialRun(perfect).run(adversarial, sound)
    assert report["detection_rate"] == 1.0
    assert report["false_flag_rate"] == 0.0
    assert report["detected_for_the_right_reason"] == report["detected"]


def test_flagging_for_the_wrong_reason_is_counted_separately(battery):
    """
    Catching a hallucinated reference because the stem "seemed ambiguous" is a
    lucky guess. Over twenty items, luck is common.
    """
    adversarial, sound = battery
    report = AdversarialRun(flag_everything).run(adversarial, sound)
    assert report["detected_for_the_right_reason"] < report["detected"]
    assert "may be coincidental rather than detection" in report["interpretation"]


# ---------- the control arm ----------

def test_running_without_a_control_arm_says_the_figure_is_uninterpretable(battery):
    adversarial, _ = battery
    report = AdversarialRun(flag_everything).run(adversarial)
    assert report["control_n"] == 0
    assert "cannot distinguish a working validator from one that flags everything" in \
        report["interpretation"]


def test_the_interpretation_always_states_the_sample_size(battery):
    adversarial, sound = battery
    report = AdversarialRun(perfect).run(adversarial, sound)
    assert f"Sample size is {report['adversarial_n']}" in report["interpretation"]
    assert "not gate-grade evidence" in report["interpretation"]


# ---------- errors are not detections ----------

def test_a_crashing_validator_does_not_score_as_vigilant(battery):
    adversarial, _ = battery

    def explode(_item):
        raise RuntimeError("validator unreachable")

    report = AdversarialRun(explode).run(adversarial)
    assert report["detected"] == 0
    assert report["detection_rate"] == 0.0
    assert all(o["actual_verdict"] == "error" for o in report["outcomes"])
    assert all("validator unreachable" in o["error"] for o in report["outcomes"])


def test_a_skipped_validation_is_not_a_detection(battery):
    adversarial, _ = battery
    report = AdversarialRun(lambda i: {"status": "skipped"}).run(adversarial)
    assert report["detected"] == 0


# ---------- per-class reporting ----------

def test_every_defect_class_is_reported_even_when_unexercised():
    report = AdversarialRun(perfect).run([])
    for defect in DEFECT_CLASSES:
        assert defect in report["per_defect_class"]
        assert report["per_defect_class"][defect]["n"] == 0
        assert report["per_defect_class"][defect]["detection_rate"] is None


def test_per_class_rates_carry_their_n(battery):
    adversarial, sound = battery
    report = AdversarialRun(perfect).run(adversarial, sound)
    for defect, stats in report["per_defect_class"].items():
        if stats["n"]:
            assert stats["detected"] <= stats["n"]
            assert 0.0 <= stats["detection_rate"] <= 1.0


def test_expected_checks_are_defined_for_every_defect_class():
    """
    A defect class with no expected checks silently reports every catch as
    'not_applicable', which hides the lucky-guess problem for that class.
    """
    missing = [d for d in DEFECT_CLASSES if not EXPECTED_CHECKS.get(d)]
    assert missing == [], f"no expected checks defined for: {missing}"


def test_expected_checks_name_real_validator_checks():
    from student.validation import CHECKS

    known = {name for name, _ in CHECKS}
    for defect, checks in EXPECTED_CHECKS.items():
        unknown = set(checks) - known
        assert not unknown, f"{defect} expects checks that do not exist: {unknown}"


# ---------- the shipped battery ----------

def test_the_shipped_battery_exercises_every_defect_class(battery):
    adversarial, _ = battery
    assert {i.defect_class for i in adversarial} == set(DEFECT_CLASSES)


def test_the_control_arm_contains_no_deliberately_broken_items(battery):
    _, sound = battery
    assert all(not i.is_negative for i in sound)
    assert len(sound) >= 20
