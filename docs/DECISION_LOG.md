# Architecture Decisions v0.2

## D001 — Independent benchmark
Decision: user sources never define benchmark truth.
Reason: avoids source-dependent validation.

## D002 — Public data is diagnostic, not sole holdout
Reason: contamination risk. Google's published MedGemma documentation warns that medical benchmark performance may be affected by training-data contamination. This claim requires a verifiable citation before any public release; see docs/CONTAMINATION_PROTOCOL.md.

## D003 — Human adjudication for critical medical errors
Reason: model judges can share systematic errors.

## D004 — Same-family judge cannot gate PASS
Reason: correlated failure modes.

## D005 — Minimum n + CI
Reason: point estimates on small samples are unstable.

## D006 — Immutable gold corrections
Reason: changing gold retroactively destroys reproducibility.

## D007 — Embedding threshold calibration only on DEV
Reason: prevents holdout overfitting.

## D008 — Video is a future benchmark track
Reason: North Star but not MVP dependency.
