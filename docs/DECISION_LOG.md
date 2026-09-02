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

## D014 -- Phase 0 attempt 2 was destroyed by a container restart, and the runner now journals every reply

Attempt 2 started 2026-09-02T19:35:21Z under freeze `c2955816b918`. At roughly
118 minutes, still inside arm 1 (A+B+D), the container running the session was
restarted and the process was killed. The runner flushes at experiment
boundaries, so an arm that has not finished has written nothing: no
`phase0_ablation_v2.json`, no arm record, no partial matrix. The API spend was
real and the evidence is gone in its entirety.

CLASSIFICATION: attempt 2 is INCOMPLETE, terminated by infrastructure rather
than by any defined outcome. It is not INSUFFICIENT_EVIDENCE -- that is a
statement about a measurement that happened. Nothing here was measured. No
number from it is quoted anywhere, because none exists.

WHAT CHANGED, AND WHY IT IS NOT A CHANGE TO THE EXPERIMENT

A third attempt that is another uninterrupted five-hour process is the same
gamble again: the set needs ~765 logical calls at a measured 25s or worse, and
the environment has now demonstrated it will not reliably hold a process that
long. So the runner gained `--journal`: every reply is written to an
append-only file and fsynced BEFORE it is used, and a resumed invocation
replays what is on disk and pays only for what was never asked.

`benchmark/journal.py` sits outside `validator/` and `benchmark/providers/`,
the two trees `validator_fingerprint` hashes. Verified rather than assumed:
the fingerprint is `2b8e15b6f390...` before and after, identical to the value
in the freeze, so freeze `c2955816b918` remains in force and this is the same
experiment continued, not a new one. No refreeze; the digest does not move.

THE THREE RULES THAT KEEP A RESUME FROM BECOMING A DIFFERENT EXPERIMENT

1. A recorded outage replays as that outage. The model is not re-asked.
   Replaying successes while re-asking failures is selective retry, and it
   converts an outage rate into whatever the operator has patience for. This
   is the rule that mattered most: attempt 1 had 71 outages in 100 items, and
   a resume that "just retried the failures" would have manufactured a clean
   arm out of a broken run.
2. Each arm pays for its own calls. The journal key includes the arm, so a
   reply recorded for A+B+D is never served to A+B+C+D even when the request
   is byte-identical. Sharing would strip between-arm sampling variation out
   of `ABCD - ABD`, which is a quieter experiment than the frozen one.
3. Spend and elapsed time carry across resumes, so the frozen
   `budget_max_calls: 2400` and `max_wall_minutes: 480` bound the SET.
   Resetting them at each restart would turn the ceiling into 2400 per crash.

Verdicts are recomputed by the real pipeline from the real replies; nothing is
rehydrated from a summary, because a lossily reconstructed verdict is
fabricated evidence. `tests/test_journal.py` asserts that an interrupted-then-
resumed arm reaches the same verdicts as an uninterrupted one, field for
field, and asserts each refusal above separately.

AN OVERSPEND THAT IS ON THE RECORD RATHER THAN HIDDEN

Attempt 2's spend was not journalled -- there was no journal yet -- so it
cannot be carried forward, and the relaunch begins its accounting at zero
against the same frozen ceiling. Across both invocations the set will
therefore have cost more than `budget_max_calls: 2400` while producing one
set of evidence. The frozen ceiling still bounds what the surviving
measurement cost, which is what it exists to guarantee; the lost invocation is
recorded here as a known, unrecoverable overspend rather than quietly
absorbed.

## D015 -- the wall-clock ceiling is raised to 1200 minutes on a measured rate, before any result is seen

Freeze `c2955816b918` is discarded and replaced. The ONLY field that changes
is `max_wall_minutes`, 480 -> 1200. Corpus, corpus hash, pairing, prompt
versions, temperature, retry policy, call budget, the three arms and every
threshold are byte-identical, and the validator fingerprint is unchanged at
`2b8e15b6f390...`.

WHY

The relaunched run journalled 8 replies in 10.0 minutes: 75.2 seconds per
logical call, median 55.4s, max 158.5s. The set is 765 logical calls, so it
projects to 16.0 hours. The 480-minute ceiling would have been crossed near
the halfway mark and the set would have returned INCOMPLETE for the third
time -- arm 1 alone is 285 calls, about 6 hours, which is precisely why
attempt 1 died inside it at 181 minutes.

1200 minutes is the measured 16.0 hours plus about 25% for the observed
latency spread and for retries.

WHY THIS IS NOT MOVING A GOALPOST

The wall clock is an operational bound on completion. It is not one of the
thresholds that decide the result, and none of those moved: min_sensitivity
0.80, min_specificity 0.90, min_items_per_arm 30 and judge_confidence_floor
0.60 are exactly as frozen. Nothing about what is measured, how it is scored,
or what would count as a pass is different.

The timing is the substance of the claim. This was decided 10 minutes and 9
outbound attempts in, from a rate measured on this configuration, with NO arm
finished, NO matrix computed and NO score of any kind in existence. Raising a
ceiling after seeing a number one did not like is a different act with the
same diff, and the record needs to show which one this was. The call budget
was deliberately NOT touched: spend stays bounded at the forecast 2400/600.

A COST WORTH NAMING

The journal is bound to a freeze digest, so a refreeze discards it. The 8
recorded replies are abandoned rather than replayed under a digest they were
not recorded under. That is the correct trade at 8 calls and would have been a
catastrophic one at hour 8, which is the argument for measuring the rate early
rather than trusting the estimate.

AN EARLIER ESTIMATE THAT WAS WRONG

D013 recorded the 180-minute ceiling as having been set from a single-probe
latency that under-predicted by roughly 3x. The 480-minute replacement was my
own estimate of ~25 s/call and it under-predicted by a further 3x. Both were
projections from too little data. This one is not an estimate: it is 8
observations of the frozen configuration doing the actual work, and it is
recorded here with its sample size so the next reader can weigh it properly.

WHAT THE RUN ALREADY SHOWS, AND WHAT IT DOES NOT

Zero outages in 8 calls, against attempt 1's 71 in 100 items. That is
consistent with the D013 adapter defect having been the cause, and it is 8
calls: it is an encouraging sign about the repair, not a measurement of
anything, and no arm, gate or score is claimed from it.
