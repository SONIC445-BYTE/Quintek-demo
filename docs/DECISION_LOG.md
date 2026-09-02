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

## D011 — V1 experimental pairing, selected deterministically
Decision: for Quintek V1 the Phase 0 pair is
candidate `nvidia:deepseek-ai/deepseek-v4-flash-0731` (family `deepseek`),
judge `nvidia:nvidia/ising-calibration-1.5-31b` (family `ising`).

Rule applied, not invented. The repository already contains the objective
ranking for this decision: `DynamicModelRegistry.sort_key`, used by
`shortlist()`, whose documented purpose is to "trim a passing set to a
benchmarkable size WITHOUT judging quality -- cost then context then id, all
objective, none a proxy for how good a model is." That is exactly a
pre-benchmark candidate selection. `benchmark/fitness.py`'s `rank_key` is the
routing ranking and is degenerate here: with no benchmark observations every
candidate scores `fitness=None`, so it yields no ordering at all.

Mechanically: the eligible set is the four models with OBSERVED evidence for
both validation capabilities. NVIDIA publishes neither prices nor context
windows, so all four tie on the first three sort terms and the ordering
reduces to the key tiebreak -- deterministic and reproducible, which is what
was required. The rank-1 candidate paired with the rank-1 model of a different
family on the first attempt, so the search stopped there per rule G.

This is NOT a claim that the pair is globally optimal. It is the deterministic
V1 experimental pair. Latency was deliberately NOT a selection input: the
candidate's best observed latency is 8460ms against the judge's 419ms, and
choosing on that basis would have been preference dressed as engineering. The
consequence is accepted -- roughly 570 candidate calls at that latency, which
the wall-clock ceiling bounds, and a ceiling breach is recorded INCOMPLETE
rather than scored.

Independence: families differ (`deepseek` vs `ising`), so Tier 2 requirement 1
is satisfied and D004 is not engaged. Requirement 2, "different provider when
practical", is NOT satisfied: both are NVIDIA-hosted, and NVIDIA is the only
authorized V1 provider, so a different provider is not practical. Recorded as
a V1 limitation, not waived.

## D012 — V1 corpus governance: six fields are future work, not a V1 gate
Decision: `review_status`, `challenge_history`, `corrections`, `adjudication`,
`version` and `contamination` are NOT added to corpus items for V1.
Reason: they gate nothing on the V1 qualification path. None is read by
`benchmark/corpus.py`, `benchmark/gates.py`, `benchmark/integrity.py`,
`validator/devset.py` or `validator/holdout.py`. Adding them would change
`corpus_hash` -- which the freeze pins -- for fields no gate consults, so the
change would buy nothing and cost the comparability of every run against this
corpus. This is a decision that they are not required, not a decision to
tolerate a known defect: correctness would take priority over the hash if a
gate depended on them.

`GoldChallengeLedger` stays unwired for V1 for the same reason: it is the
Phase 3 human-review mechanism, and V1 has no qualified reviewers to operate
it. Both are recorded as V1 limitations in docs/FINAL_STATUS.md.

## D013 — Phase 0 result: INCOMPLETE. Layer C undetermined.
Decision recorded, not made: Phase 0 ran once under freeze 3bca900de60e and
terminated INCOMPLETE. It did not decide whether Layer C earns its place.

What happened. Experiment 1 (A+B+D) consumed the whole 180-minute wall-clock
ceiling, reached at 181.4 minutes, and experiments 2 (C) and 3 (A+B+C+D) never
started. With no ABCD arm the incremental contribution of the judge cannot be
computed, so the experiment conclusion is "not determined" and the model
conclusion is DEFERRED. Both are the defined outcomes, not failures to work
around, and no arm was retried.

The arm that did run is itself INCOMPLETE: 71 of 100 items produced outages,
leaving 28 decided against a required 30 per arm, so the gate reads
INSUFFICIENT_EVIDENCE. Its numbers -- sensitivity 100% (4 defective),
specificity 0% (24 clean items all flagged) -- are recorded and are NOT a
measurement of anything. They rest on four defective items and a run that lost
seven items in ten.

Two contributing defects, both ours, both fixed:

1. `benchmark/providers/nvidia.py` read only `message.content`. The candidate
   is a reasoning model that leaves that null and puts its reply in
   `reasoning_content`, so the JSON extractor was handed None and raised
   "expected string or bytes-like object, got 'NoneType'". Each such item was
   recorded as a backend outage. The identical bug had been found and fixed in
   `benchmark/capability_probe.py` days earlier and not here; the extraction
   now has ONE definition in `providers/base.py`.

2. `validator_fingerprint` hashed only `validator/`. The provider adapter sat
   outside it, so fixing defect 1 would have changed what a run measures while
   leaving the digest identical -- stamping the repaired run and the broken one
   as comparable. `benchmark/providers/` is now inside the fingerprint.

A selection consequence worth stating plainly: D011's rule forbade latency as
an input and selected the slowest of the four qualified models (8460ms best
observed against the judge's 419ms). The 180-minute ceiling was set from a
single-probe latency that under-predicted the real rate by roughly 3x. The
rule was applied correctly and the ceiling was honest; together they made this
run unable to finish. That is a fact about the configuration, not a reason to
raise the ceiling after seeing the result.

Consequence: Phase 1 does NOT open. Its entry condition is a genuinely
complete Phase 0. The holdout remains untouched at 0 of MAX_USES 5.

Any re-run is a NEW experiment under a NEW freeze: defect 2's fix moves the
fingerprint, so the digest necessarily changes and the two runs are correctly
non-comparable. Re-running requires explicit authorization -- it is a fresh
spend and a refreeze, both decision gates.
