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

## D009 — Reverted: OpenRouter is not a Quintek discovery source
Decision: OpenRouter was added to `benchmark/provider_catalogue.py` as a
second catalogue source on 2026-09-02 and reverted the same day, unrun.
Reason: NVIDIA is the only authorized live inference provider for Quintek, and
the separate Registry repository belongs to a different provider/discovery
project; reinterpreting it as an OpenRouter registry would collapse that
boundary. Removed: `OpenRouterSource`, the `openrouter` entry in `SOURCES`,
the `catalogue_requires_key` / `credential_used` mechanism added to support a
public catalogue, 421 reconciled catalogue rows, one snapshot, and three
integration tests. No inference call was ever made to OpenRouter and no
credential was requested or held — the catalogue endpoint is public. The
DECLARED-versus-OBSERVED test survives with a neutral provider label, because
the invariant it pins is Quintek's and not OpenRouter's. Pre-existing
OpenRouter references elsewhere in the repository are untouched.

## D010 — OPEN: the 8B/70B plan contradicts the same-family judge prohibition
Decision: NOT MADE. Recorded as an unresolved contradiction.
`docs/VALIDATOR.md` names "8B candidate, 70B judge" as the first experiment
set, and both `meta/llama-3.1-8b-instruct` and `meta/llama-3.1-70b-instruct`
infer to model family `llama`. D004 and `docs/JUDGE_INDEPENDENCE.md` Tier 2
require a judge of a different family, and a same-family judge is Tier 3 which
may never be the sole basis for PASS. The two documents cannot both be
followed. Separately, both models answered HTTP 410 end-of-life on
2026-08-26, so the pairing is also unrunnable on availability grounds. No
substitute pairing has been selected: the repository contains no deterministic
rule for choosing one, and inventing a selection rule after seeing which
models happen to be alive is the substitution the freeze protocol exists to
prevent. Awaiting authorization.
