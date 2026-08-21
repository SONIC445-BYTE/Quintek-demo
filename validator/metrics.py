"""
Measuring a validator: two arms, always, with the interval.

The 8B result is the whole argument for this module. It caught 11 of 20
planted defects and false-flagged 9 of 10 clean items. Reported as one number
-- "55% detection" -- it looks like a weak validator. Reported as two, it is
obvious that it is not a validator at all: it flags nearly everything, and
catching half the defects is what falling on your face looks like when you
guess ERROR most of the time.

So every result here carries BOTH arms and refuses to produce a single score.
There is no `overall`, no F1, no weighted blend. A blend lets a validator that
screams ERROR at everything buy its specificity failure with sensitivity, and
that is exactly the failure mode already observed on this project.

Rates are reported with a Wilson interval because n is 100, not 10,000. On 20
adversarial items, 11 detections is 55% -- with a 95% interval of roughly
[34%, 74%]. Quoting 55% alone invites a comparison between two validators that
the sample cannot support.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Labels an item carries, and a verdict a validator returns.
DEFECTIVE = "DEFECTIVE"
CLEAN = "CLEAN"

# What a validator said. `ABSTAIN` is deliberately available: a validator that
# can say "I do not know" is more useful than one forced to guess, and
# abstentions are counted separately rather than folded into either arm.
FLAGGED = "FLAGGED"
PASSED = "PASSED"
ABSTAINED = "ABSTAINED"


def wilson(successes: int, total: int, z: float = 1.96) -> tuple[float, float] | None:
    """
    A 95% Wilson score interval, or None when there is nothing to interval.

    Wilson rather than normal approximation: at n=20 with p near 0 or 1 the
    normal interval runs past 0% or 100%, which is not a possible rate and
    reads as false precision.
    """
    if total <= 0:
        return None
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = (z / denominator) * math.sqrt(
        p * (1 - p) / total + z * z / (4 * total * total))
    return (max(0.0, centre - margin), min(1.0, centre + margin))


@dataclass(frozen=True)
class ConfusionMatrix:
    """
    Counts only. Every rate below is derived, and every one carries its n.

    Naming follows the clinical convention the audience already uses: a
    "positive" is a DEFECT, so a true positive is a defect correctly caught.
    """

    true_positive: int = 0     # defective, flagged
    false_negative: int = 0    # defective, passed          -- a defect shipped
    true_negative: int = 0     # clean, passed
    false_positive: int = 0    # clean, flagged             -- a false alarm
    abstained_defective: int = 0
    abstained_clean: int = 0

    @property
    def defective(self) -> int:
        return self.true_positive + self.false_negative + self.abstained_defective

    @property
    def clean(self) -> int:
        return self.true_negative + self.false_positive + self.abstained_clean

    @property
    def total(self) -> int:
        return self.defective + self.clean

    @property
    def sensitivity(self) -> float | None:
        """Of the defects present, how many were caught. None when none were present."""
        decided = self.true_positive + self.false_negative
        return self.true_positive / decided if decided else None

    @property
    def specificity(self) -> float | None:
        """Of the clean items, how many were correctly left alone."""
        decided = self.true_negative + self.false_positive
        return self.true_negative / decided if decided else None

    @property
    def decided(self) -> int:
        """Items on which the validator committed. Abstentions are excluded on purpose."""
        return (self.true_positive + self.false_negative
                + self.true_negative + self.false_positive)

    @property
    def sensitivity_ci(self) -> tuple[float, float] | None:
        return wilson(self.true_positive, self.true_positive + self.false_negative)

    @property
    def specificity_ci(self) -> tuple[float, float] | None:
        return wilson(self.true_negative, self.true_negative + self.false_positive)

    @property
    def false_flag_rate(self) -> float | None:
        """
        The arm the 8B validator failed. 1 - specificity, named separately
        because "9 out of 10 clean items were flagged" is the sentence that
        makes the failure legible.
        """
        specificity = self.specificity
        return None if specificity is None else 1 - specificity

    def as_dict(self) -> dict:
        sensitivity_ci = self.sensitivity_ci
        specificity_ci = self.specificity_ci
        return {
            "counts": {
                "true_positive": self.true_positive,
                "false_negative": self.false_negative,
                "true_negative": self.true_negative,
                "false_positive": self.false_positive,
                "abstained_defective": self.abstained_defective,
                "abstained_clean": self.abstained_clean,
            },
            "n_defective": self.defective,
            "n_clean": self.clean,
            "n_total": self.total,
            "sensitivity": self.sensitivity,
            "sensitivity_ci95": sensitivity_ci,
            "specificity": self.specificity,
            "specificity_ci95": specificity_ci,
            "false_flag_rate": self.false_flag_rate,
            # Said out loud because a reader who sees only the point estimates
            # will compare two validators the sample cannot separate.
            "note": "Both arms are reported because neither alone describes a "
                    "validator. A detector that flags everything scores "
                    "perfectly on sensitivity.",
        }


def confusion(labels: list[str], verdicts: list[str]) -> ConfusionMatrix:
    """
    Build the matrix from paired labels and verdicts.

    Refuses on a length mismatch rather than zipping to the shorter list,
    which would silently drop the tail of a run and report a rate computed
    over items nobody looked at.
    """
    if len(labels) != len(verdicts):
        raise ValueError(
            f"{len(labels)} labels but {len(verdicts)} verdicts. Zipping to the "
            "shorter one would report a rate over a set nobody chose.")

    counts = {"tp": 0, "fn": 0, "tn": 0, "fp": 0, "ad": 0, "ac": 0}
    for label, verdict in zip(labels, verdicts):
        if label == DEFECTIVE:
            key = {FLAGGED: "tp", PASSED: "fn", ABSTAINED: "ad"}.get(verdict)
        elif label == CLEAN:
            key = {FLAGGED: "fp", PASSED: "tn", ABSTAINED: "ac"}.get(verdict)
        else:
            raise ValueError(f"unknown label {label!r}; expected {DEFECTIVE} or {CLEAN}")
        if key is None:
            raise ValueError(f"unknown verdict {verdict!r}")
        counts[key] += 1

    return ConfusionMatrix(
        true_positive=counts["tp"], false_negative=counts["fn"],
        true_negative=counts["tn"], false_positive=counts["fp"],
        abstained_defective=counts["ad"], abstained_clean=counts["ac"])


# ---------------------------------------------------------------------------
# The pass rule
# ---------------------------------------------------------------------------
# Two arms, both required, and judged on the LOWER CONFIDENCE BOUND rather
# than the point estimate.
#
# At n=40 per arm a point estimate of 0.80 has a 95% interval reaching down to
# about 0.65. Passing a validator on the point estimate means passing one whose
# true rate may be well under the threshold, on evidence that cannot tell the
# difference. Requiring the lower bound to clear it means "we are confident it
# is at least this good", which is the claim a gate should be making.
#
# The thresholds are STARTING POINTS. They have never been calibrated against a
# validator anyone was happy with, because no validator has yet passed. They
# are written here as configuration so that recalibrating them is a visible
# decision rather than an edit buried in a comparison.

MIN_SENSITIVITY = 0.80
MIN_SPECIFICITY = 0.90
MIN_ITEMS_PER_ARM = 30

PASS = "PASS"
FAIL = "FAIL"
INSUFFICIENT = "INSUFFICIENT_EVIDENCE"

# The largest arm size worth searching for. Beyond this the answer is "more
# items than you are going to hand-write", and saying so is more useful than
# returning a number nobody will act on.
_SEARCH_CEILING = 5000


def min_items_for(threshold: float, errors: int = 0, z: float = 1.96,
                  ceiling: int = _SEARCH_CEILING) -> int | None:
    """
    How many items an arm needs before `threshold` is even reachable.

    Judging on the lower bound of the interval has a consequence that is easy
    to miss and expensive to discover late: with a small arm, the bound cannot
    reach the threshold NO MATTER HOW GOOD THE VALIDATOR IS. Thirty clean items
    all correctly passed give a lower bound of 88 per cent, which does not
    establish 90 per cent -- so a flawless validator measured on thirty items
    fails, and the failure says nothing about the validator.

    That is not a FAIL. It is insufficient evidence, and `gate` reports it as
    such using this function, which also gives the number of items that would
    settle it.

    `errors` is how many mistakes the arm should still be able to tolerate. An
    arm sized for a perfect run certifies only a perfect run.
    """
    for n in range(max(errors + 1, 1), ceiling + 1):
        bounds = wilson(n - errors, n, z)
        if bounds and bounds[0] >= threshold:
            return n
    return None


def tolerated_errors(n: int, threshold: float, z: float = 1.96) -> int:
    """
    How many mistakes an arm of this size can absorb and still clear `threshold`.

    Returns -1 when the arm is too small even for a flawless run, which is the
    case worth naming: the number is not "zero mistakes allowed", it is "this
    measurement cannot succeed".
    """
    best = -1
    for errors in range(0, n + 1):
        bounds = wilson(n - errors, n, z)
        if bounds and bounds[0] >= threshold:
            best = errors
        else:
            break
    return best


@dataclass(frozen=True)
class Gate:
    """A pass decision, with every reason it went the way it did."""

    outcome: str
    reasons: tuple[str, ...]
    matrix: ConfusionMatrix
    min_sensitivity: float
    min_specificity: float

    @property
    def passed(self) -> bool:
        return self.outcome == PASS

    def as_dict(self) -> dict:
        return {"outcome": self.outcome, "reasons": list(self.reasons),
                "thresholds": {"min_sensitivity": self.min_sensitivity,
                               "min_specificity": self.min_specificity,
                               "min_items_per_arm": MIN_ITEMS_PER_ARM,
                               "judged_on": "lower bound of the 95% interval"},
                "matrix": self.matrix.as_dict()}


def gate(matrix: ConfusionMatrix, *, min_sensitivity: float = MIN_SENSITIVITY,
         min_specificity: float = MIN_SPECIFICITY,
         min_items: int = MIN_ITEMS_PER_ARM) -> Gate:
    """
    Does this validator clear both arms?

    INSUFFICIENT_EVIDENCE is a distinct outcome from FAIL. A run on eight
    items that happens to score well has not demonstrated anything, and
    calling that a PASS is how an unvalidated validator reaches production.
    """
    reasons: list[str] = []
    decided_defective = matrix.true_positive + matrix.false_negative
    decided_clean = matrix.true_negative + matrix.false_positive

    if decided_defective < min_items or decided_clean < min_items:
        reasons.append(
            f"{decided_defective} defective and {decided_clean} clean items "
            f"were decided; {min_items} of each are required before a rate "
            "means anything")
        return Gate(INSUFFICIENT, tuple(reasons), matrix,
                    min_sensitivity, min_specificity)

    # An arm too small for its own threshold cannot produce a PASS however good
    # the validator is, so a FAIL from it would be a fact about the corpus
    # reported as a fact about the validator.
    unreachable = []
    for name, decided, threshold in (
            ("defective", decided_defective, min_sensitivity),
            ("clean", decided_clean, min_specificity)):
        needed = min_items_for(threshold)
        if needed is not None and decided < needed:
            unreachable.append(
                f"the {name} arm has {decided} item(s); even a flawless run on {decided} "
                f"reaches a lower bound below {threshold:.0%}, so this arm cannot establish "
                f"the threshold at any performance. {needed} items are needed for a perfect "
                f"run to clear it, and {min_items_for(threshold, 1)} to clear it while "
                "tolerating a single mistake")
    if unreachable:
        return Gate(INSUFFICIENT, tuple(reasons + unreachable), matrix,
                    min_sensitivity, min_specificity)

    sensitivity_ci = wilson(matrix.true_positive, decided_defective)
    specificity_ci = wilson(matrix.true_negative, decided_clean)

    if sensitivity_ci[0] < min_sensitivity:
        reasons.append(
            f"sensitivity {matrix.sensitivity:.0%} "
            f"(95% CI {sensitivity_ci[0]:.0%}-{sensitivity_ci[1]:.0%}) does not "
            f"establish {min_sensitivity:.0%}: {matrix.false_negative} defect(s) "
            "were passed as clean")
    if specificity_ci[0] < min_specificity:
        reasons.append(
            f"specificity {matrix.specificity:.0%} "
            f"(95% CI {specificity_ci[0]:.0%}-{specificity_ci[1]:.0%}) does not "
            f"establish {min_specificity:.0%}: {matrix.false_positive} clean "
            "item(s) were flagged, and a validator that cries wolf is one "
            "nobody reads")

    abstentions = matrix.abstained_defective + matrix.abstained_clean
    if abstentions:
        # Not a failure by itself, but it changes what the rates describe.
        reasons.append(
            f"{abstentions} item(s) were abstained on and are excluded from "
            "both rates above")

    failed = any("does not establish" in r for r in reasons)
    return Gate(FAIL if failed else PASS, tuple(reasons), matrix,
                min_sensitivity, min_specificity)
