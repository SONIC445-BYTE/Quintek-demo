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

## D016 -- a request that never reached the provider is not evidence about the provider

Found by running D014's journal, which is what it was built for.

The second container restart killed the run at 52 journalled calls. The
journal did its job -- 52 replies survived where attempt 2 lost 118 minutes
entire -- and then the surviving record showed something worse than a lost
run:

    calls  1-20  ok, latencies 31-176s
    call     21  ERR  54.6s  [Errno 111] Connection refused
    calls 22-52  ERR   0.1s  [Errno 111] Connection refused  (31 of them)

Thirty-two consecutive instant connection refusals as the container was torn
down. The endpoint answered HTTP 200 in 4.1s from the next container minutes
later, so the provider was healthy throughout. Nothing was listening on this
side of the socket. Nothing was asked.

THE DEFECT, WHICH IS IN CODE WRITTEN HOURS EARLIER IN THIS SESSION

D014's rule 1 -- a recorded outage replays as that outage and is never
re-asked -- is correct, and it is what stops a resume from becoming selective
retry. Applied to these 32 rows it is catastrophic: they would have replayed
into arm A+B+D forever as 32 model outages. A fact about the harness, made
permanent as a fact about the model, by the very mechanism built to protect
the record. Left alone, arm 1 would have carried 32 fabricated outages into
`ABCD - ABD`.

THE FIX, AND WHERE THE LINE IS DRAWN

`ProviderStatus.UNREACHED`: the TCP connection was never established, so the
provider never saw the request. The journal does not record such a response,
so a resumed run asks that item for the first time -- not again.

The line is deliberately narrow, because "it was probably the network" is a
universal solvent for inconvenient outages:

  - EXEMPT, and only these: failures that PROVE no connection came up --
    connection refused, DNS resolution failure, no route to host, network
    unreachable.
  - RECORDED: everything else, including every timeout. A timeout is
    ambiguous -- the request may have arrived and the reply been lost coming
    back -- and the conservative direction for an ambiguous failure is that it
    counts. Also recorded: connection reset, which can arrive after delivery.

Checked before TIMEOUT in `classify`, because the NVIDIA adapter wraps every
`URLError` as `TimeoutError`: "connection refused" arrives wearing a timeout's
class and would otherwise read as an endpoint that was too slow. Its policy
sets `counts_against_quality=False` and `open_circuit=False` -- the break is
on our side, and opening the circuit on the provider would blame it for our
outage.

NOTHING WAS DELETED

The 32 rows remain in the journal file. They are skipped at load by the rule
above, which is the thing under test, rather than edited out by hand. The file
stays a complete account of what happened; 20 genuine replies are kept and
carried forward. Deleting evidence to repair a rule is how the repair becomes
unauditable.

Carried spend correctly reads 21 outbound attempts rather than 117: the 96
attempts that never opened a connection cost nothing at the provider, which is
the same fact the status class encodes.

The validator fingerprint is unchanged at `2b8e15b6f390...` and freeze
`919cd25bc306` remains in force -- `benchmark/provider_status.py` and
`benchmark/journal.py` are both outside the hashed trees. The run resumes; it
does not restart. Suite: 1274 passed, 4 skipped.

## D017 -- the validator was reading the HTTP envelope as the model's answer. Every Phase 0 arm to date is void

Arm 1 of the resumed run completed cleanly: 68 of 68 decided, 0 abstentions,
15 outages, no restarts. It reported sensitivity 100%, specificity 0%, and a
discrimination rate of 0% across 29 matched pairs, with all 34 false positives
on one check, `grounding/not_answerable_from_passage`.

A validator that flags every item it sees is not measuring the items. The
journal made the cause auditable, because it had stored every raw reply.

### The defect

`NVIDIAProvider._call` returned the entire HTTP body as `raw_output`. Every
validator layer recovers the model's JSON with
`validator.grounding.extract_json(response.raw_output)`, which takes the first
BALANCED JSON object in the text. An HTTP envelope is itself a balanced JSON
object, so the validator parsed

    {"id": "chatcmpl-...", "choices": [...], "usage": {...}}

and read that as the model's answer. `supported` is absent from an envelope,
so `supported == []` and every item took the `not addresses or not supported`
branch: `NOT_ANSWERABLE_FROM_PASSAGE`. Measured over the journal: 91 of 94
grounding `:key` calls returned the envelope, 3 returned nothing, and ZERO
returned the model's answer.

The contract was never ambiguous. `validator/scripted.py` has always returned
`json.dumps(reply)` -- the model's reply. Only this adapter disagreed, and
1283 tests were green because every one of them drives a scripted provider.
Nothing anywhere compared the two implementations against the consumer they
share. The gap was not a missing assertion inside a test; it was a missing
test at the seam.

The model was answering correctly the whole time. `vd-clean-001` returned
`{"passage_addresses_question": true, "supported": ["D"], ...}` with verbatim
evidence, and was flagged as unanswerable.

### Two further defects found in the same evidence

2. `content_of` CONCATENATED `content` and `reasoning_content`. The intent was
   that a reasoning model fills `reasoning_content` INSTEAD of `content`; when
   it fills both, the reply is in `content` and the other field is the
   thinking. Joining appended prose to the JSON, and the greedy extractor then
   spanned from the answer's opening brace into the prose. Of 340 journalled
   replies, 219 carried both fields: joined, 184 parsed; first-non-empty, 277.
   The join was costing 93 answers, recorded as backend outages. This was
   introduced by D013's own fix -- a repair that created a subtler version of
   the bug it repaired.

3. A reply cut off at `max_tokens` with no `content` was handed to
   `extract_json` as reasoning prose, which quotes JSON: a fragment such as
   `{"A": "..."}` inside the model's deliberation was read as its answer and
   the item flagged on the strength of it. An unfinished reply is now an
   outage.

### A configuration change, made before any valid score existed

`GenerationRequest` defaults to `max_tokens=1024`. That was a dataclass
default, not a validator decision. Against this reasoning candidate: 277
replies finished normally using a mean of 402 completion tokens (median 351,
p90 811, max 1007); 63 hit `finish_reason: length` at exactly 1024, and 52 of
those had emitted no answer at all.

An 18% loss rate is not survivable here, and arm 1 said why: the clean arm
needs at least 35 scored items for even a flawless run to reach a 90%
specificity lower bound, and the corpus supplies 40. At 18% outages the arm
lands near 33 and the gate reads INSUFFICIENT_EVIDENCE whatever the model
does. The cap was making the experiment unable to return a verdict.

`validator.grounding.MAX_REPLY_TOKENS = 4096`, used by all three layers. It is
a ceiling on truncation, not a target; a reply that fits in 351 tokens still
costs 351. Decided from measurement, before any valid score existed -- the
only scores in existence were produced by the defect above and are void.

### A V1 limitation that this surfaced and does not fix

Specificity >= 0.90 is reachable on this corpus ONLY with a perfect clean arm:
35 items for a flawless run, 53 to clear the threshold while tolerating a
single false positive, against 40 available. The corpus is marginal for its
own gate. That is a property of the corpus, not of any model, and no amount of
re-running changes it. Recorded here as a known V1 limitation.

### Consequence

Every Phase 0 arm produced so far is VOID -- not INSUFFICIENT_EVIDENCE, which
is a statement about a measurement that happened, but void: the instrument was
reading its own transport. Attempt 1's specificity 0% carried the same
signature and has the same explanation, so D013 was right that there was an
adapter defect and wrong that it had found it.

No score from any of them is quoted or carried forward. The fingerprint moves
to `a438d091e1d4...`, freeze `919cd25bc306` is retired, and its journal is
refused rather than replayed: those replies were collected under a
configuration that truncated at 1024 and cannot be mixed with replies that do
not. Suite: 1283 passed, 4 skipped.

## D018 -- Phase 0 terminated INCOMPLETE. The Layer-C decision is not determined, and the reason is item attrition, not budget or time

The complete three-arm set ran to the end under freeze `acd21b3687b9` on the
instrument repaired in D017. All three arms executed. Nothing was retried
selectively, no model was substituted, and the frozen configuration was not
touched while it ran.

    arm                              sens   spec   FP   FN  abst  out  status
    1  A+B+D  without the judge       91%    39%   22    3     2   12  INCOMPLETE
    2  C      the judge alone         18%   100%    0   33     0    0  COMPLETE
    3  ABCD   the whole validator    100%    41%   19    0     2   14  INCOMPLETE

    LAYER C (ABCD - ABD): NOT COMPUTED
    EXPERIMENT CONCLUSION:  not determined
    MODEL CONCLUSION:       DEFERRED

WHY IT IS INCOMPLETE

Not budget: 588 of 2400 candidate attempts and 189 of 600 judge attempts, 25%
of the ceiling. Not wall clock: 500.3 of 1200 minutes, 42%. The run had ample
headroom in both and stopped because it finished, not because it was cut off.

It is incomplete because arms 1 and 3 could not decide 12 and 14 items: the
model returned replies that did not parse, and an unparseable validator is an
outage rather than a clean item. The harness then refused the subtraction on
its own terms -- "a run that did not reach every item cannot be subtracted
from one that did; the difference would be mostly the missing items." Arm 3
separately reads INSUFFICIENT_EVIDENCE because attrition left its clean arm at
32 items where 35 are needed for a flawless run to reach a 90% lower bound.

WHAT MUST NOT BE READ INTO IT

Arm 3 shows 100% sensitivity and specificity 41% against arm 1's 39%. That
+2-point difference is NOT evidence that the judge earns its place. The two
arms are not comparable -- 26 items are missing between them -- and the
difference is smaller than the attrition. Reporting it as a positive
contribution would be exactly the reinterpretation this protocol forbids.

Equally, arm 2's 18% sensitivity is not a verdict on the judge model. Layer C
is decided by the incremental contribution, never by C's standalone score, and
that contribution is undetermined.

WHAT THE RUN DID ESTABLISH

The validator discriminates. Before D017 it flagged all 68 decided items, 0%
discrimination on 29 matched pairs. It now separates clean from defective at
39-41% across arms 1 and 3, fails on calibration rather than blindness, and
its false positives are concentrated in two named conformance checks
(`below_declared_difficulty`, `answerable_from_wording_alone`) rather than
spread across the layer. That is a working instrument producing an honest
negative result.

CONSEQUENCE

Phase 1 does NOT open: its precondition is a legitimately complete Phase 0.
The holdout remains at 0 scoring runs of MAX_USES 5. No further inference is
authorized against this configuration, and no replacement experiment is run.
Closing the gap would need the conformance checks recalibrated and the item
attrition reduced, both of which are V2 work.

## D019 -- Phase 1: NO MODEL QUALIFIED. The blocker is a corpus/threshold judgement, not a defect, and it is not mine to settle

Asked whether Quintek can reach a legitimate qualified production model under
the existing specification. It cannot, and re-running the experiment cannot
change that. The reason is arithmetic, not luck.

WHERE THE SPECIFICITY GOES

Arm 1 of the repaired run decided 36 clean items and flagged 22 of them
(specificity 38.9%). The flags are concentrated, not diffuse:

    17  conformance/below_declared_difficulty
    11  conformance/answerable_from_wording_alone
     2  grounding/explanation_contradicts_passage

`below_declared_difficulty` fires when the candidate classifies an item's
cognitive level as `recall` while the item declares a difficulty at which
recall is not acceptable. `RECALL_IS_ACCEPTABLE_AT = ("foundation",)`, and the
clean arm is 25 `pg_entry` items and 15 `foundation` ones. So the candidate is
judging roughly 17 of the 25 `pg_entry` clean items to be mere recall.

Even if every other check were made perfect, that one check alone caps
specificity at (36 - 17) / 36 = 52.8%. The gate requires 0.90. Qualification
is unreachable, and no amount of re-running moves it.

THE CHECKS ARE NOT DEFECTIVE

Both were read closely before concluding this.
`answerable_from_wording_alone` fires only when the candidate claims a giveaway
AND the quoted cue is verifiably present in the question; when the cue is
absent it ABSTAINS rather than taking the model's word for something it was
asked to demonstrate. `below_declared_difficulty` is a direct comparison of a
declared field against a reported one. Neither misreads a correct reply. This
is not the D017 situation.

WHAT IS ACTUALLY IN DISPUTE

Either the candidate's cognitive-level classification is unreliable, or the
corpus's `pg_entry` items genuinely are recall-level and the difficulty labels
are wrong. That is a medical and pedagogical judgement about the gold corpus,
and every route to settling it is closed to this project:

  * Widening `RECALL_IS_ACCEPTABLE_AT` to include `pg_entry` would make the
    check nearly vacuous and would be a threshold moved after seeing the
    result it blocks. Forbidden outright.
  * Relabelling the corpus difficulties would change `corpus_hash` and would
    be a model editing the gold it is graded against -- precisely the failure
    the benchmark exists to prevent.
  * Expert adjudication is the correct route and needs the qualified reviewer
    pool V1 does not have.

CONCLUSION

    PHASE 1 OUTCOME: NO MODEL QUALIFIED

Not INCOMPLETE: the run was complete enough to establish this. Arm 1 reached
68 of 70 items and the shortfall is nowhere near the 37-point gap between 39%
and 90%. Not FAIL-of-the-model either: what failed its gate is the validator's
conformance calibration against this corpus, and the model conclusion stays
DEFERRED exactly as D018 left it.

Production enforcement is unchanged and remains correct: no qualified model,
so generation refuses. Verified live with the development override removed --
ingestion returns `NoEligibleModel` and generation returns 422 without
inventing anything. The holdout remains at 0 scoring runs of MAX_USES 5.

DECISION REQUIRED FROM THE PROJECT OWNER

Qualification cannot proceed until one of these is chosen, and none of them is
a decision an implementer may take alone:

  1. Commission expert review of the 25 `pg_entry` clean items to establish
     whether their difficulty labels are correct. Evidence-led, slowest.
  2. Accept V1 with NO QUALIFIED MODEL: production deploys and honestly
     refuses generation until a model qualifies. Nothing is weakened.
  3. Re-specify the difficulty taxonomy or `RECALL_IS_ACCEPTABLE_AT` as a
     deliberate, documented specification change made BEFORE any re-run and
     recorded as such -- not as a reaction to this result.
