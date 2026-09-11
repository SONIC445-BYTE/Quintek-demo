"""
Checks that run and report but do not decide a gate.

WHY THIS EXISTS
---------------
`conformance/below_declared_difficulty` compares an item's declared
`difficulty` against the cognitive level the candidate reports. It applies that
rule faithfully -- `docs/ADJUDICATION_below_declared_difficulty.md` classified
all 16 recoverable disputed items and found ZERO implementation issues.

What has no evidence is the other side of the comparison. Every item in
`corpus/validator_dev` carries:

    provenance    : model_authored
    gold_standard : false
    reviewed_by   : ""              (empty on all 100)

The difficulty labels were assigned by a model when the items were written and
never reviewed. Measuring "does the candidate judge difficulty correctly"
against them compares one model's judgement with another's and calls the
disagreement a defect. Whichever way that comparison came out it would not
have been evidence.

THIS IS NOT A LOWERED THRESHOLD
-------------------------------
The 90% specificity floor is unchanged and still applies to every check that
has gold behind it. This is the same UNEVALUABLE discipline the rest of the
harness uses: no gold means no measurement, not a failed one. A check with
nothing to be right or wrong against cannot contribute evidence in either
direction, so it contributes to neither rate.

The distinction matters in one direction especially: excluding this check does
NOT make a candidate qualified. It removes a number that was never evidence.

WHAT STILL HAPPENS
------------------
The check runs. It calls the model, it reports its finding, and the finding
appears in the run record under `non_gating` with the reason below. What
changes is only that an item flagged SOLELY by a non-gating check is not
counted as a false positive, because there is no gold that would make it one.

RETIRING AN ENTRY
-----------------
An entry leaves this list when its gold arrives -- for
`below_declared_difficulty`, that is a qualified reviewer applying an explicit
difficulty rubric to the corpus. No model may supply that gold, because it is
the gold the model will be graded against.
"""

from __future__ import annotations

#: check id -> why it cannot be evaluated on this corpus.
#: Keyed on the CHECK, not the layer: the layer is fine, one of its checks has
#: nothing to be measured against.
NON_GATING: dict[str, str] = {
    "below_declared_difficulty": (
        "UNEVALUABLE on corpus/validator_dev: every item's `difficulty` label is "
        "unreviewed model output (provenance=model_authored, gold_standard=false, "
        "reviewed_by empty), so a disagreement between the label and the candidate's "
        "reported cognitive level is not evidence about either. See "
        "docs/ADJUDICATION_below_declared_difficulty.md. Retire this entry when a "
        "qualified reviewer has labelled the corpus against an explicit rubric."
    ),
}


def is_gating(check: str) -> bool:
    return check not in NON_GATING


def reason(check: str) -> str:
    return NON_GATING.get(check, "")


def split(flags):
    """`(gating, non_gating)` for a sequence of `(layer, check)` pairs."""
    gating = tuple((lay, chk) for lay, chk in flags if is_gating(chk))
    non_gating = tuple((lay, chk) for lay, chk in flags if not is_gating(chk))
    return gating, non_gating
