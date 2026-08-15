# Human Review and Inter-Rater Reliability

## Review design

For generation and validation evaluation:
- two independent primary raters per item
- blinded to model identity
- randomized item order
- no discussion before initial rating

A senior adjudicator resolves disagreements.

## Reliability metrics

### Binary/categorical labels
Use Cohen's kappa for exactly two raters.

Report:
- observed agreement
- expected agreement
- kappa
- 95% CI

Target:
- kappa >= 0.80 for critical labels.

If 0.67 <= kappa < 0.80:
- adjudicate all critical disagreements
- retrain/calibrate raters
- rerate a calibration sample.

If kappa < 0.67:
- the rubric is not reliable enough for a gate
- revise rubric and rerun calibration.

### Ordinal 0–4 rubric
Use weighted Cohen's kappa per criterion.

Also report:
- exact agreement
- agreement within one point
- mean absolute disagreement.

### More than two raters
Use Krippendorff's alpha as the primary multi-rater statistic.

## Reviewer calibration

Before production scoring:
- 30 shared calibration items
- discuss disagreements
- lock rubric
- do not use holdout items for calibration.

## Reviewer drift

Insert 5% previously adjudicated sentinel items without identifying them as sentinels.

If sentinel agreement falls below threshold:
- pause the run
- recalibrate
- repeat affected items.
