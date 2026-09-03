# Quintek V1 — Known Limitations

## AUTHORITATIVE QUALIFICATION RECORD

This is the settled state. It is not provisional, and nothing below is
awaiting a run.

    QUALIFICATION          NO MODEL QUALIFIED
    REASON                 INSUFFICIENT EVIDENCE
    PHASE 0                INCOMPLETE / NOT COMPARABLE
    HOLDOUT                0 scoring runs of MAX_USES 5
    PRODUCTION CANDIDATES  0
    FREEZE                 acd21b3687b9
    CORPUS HASH            4da47a68fce97b8d
    VALIDATOR FINGERPRINT  a438d091e1d435b6

### Four things this record fixes

1. **`answerable_from_wording_alone` received a genuine implementation
   correction** (`bf03cb0`). It asserted that a cue "selects the keyed option"
   while verifying only that the cue appeared somewhere in stem + all options.
   Two of its Phase 0 flags quoted a distractor, which cannot select the key,
   and two quoted the keyed answer itself, which is circular. Those now
   abstain. Stem-grounded cues still report, because the corpus's own planted
   giveaways depend on that case — `vd-def-009`'s defect_note reads "The stem
   now contains the word 'caseating', which appears in no other option".
   False flags on clean items fell 8 → 4; sensitivity to planted giveaways is
   unchanged.

2. **`below_declared_difficulty` is NOT a code defect.** It applies its
   documented rule as a direct field comparison. It is unevaluable on this
   corpus because the difficulty labels are `provenance: model_authored`,
   `gold_standard: false`, `reviewed_by: ""` — on all 100 items. Nothing may
   be concluded from its flags in either direction.

3. **No qualification conclusion may be extracted from the incomplete Phase 0
   arms.** Arms 1 and 3 did not reach every item, so `ABCD - ABD` was not
   computed and Layer C was neither retained nor removed. The model conclusion
   remains DEFERRED.

4. **The experiment is closed.** It must not be re-run automatically. A future
   qualification attempt is a new, explicitly authorized experiment under
   `docs/V2_QUALIFICATION_SPEC.md`, not a repeat of this one.

### What this state is not

It is **not** a verdict on the candidate model's quality. Nothing here
measured that. The gate was not reached because the instrument could not be
evaluated against this corpus, which is a statement about the evidence
available, not about the model.

Production behaviour follows from this record and is correct: no qualified
model, so generation refuses.


Referenced by D012. This is the honest list of what Quintek V1 does not do,
recorded so that nobody has to rediscover it from the code.

A limitation here is a deliberate V1 boundary or an external dependency, not a
defect awaiting a fix. Where something is genuinely broken it is fixed and
recorded in `DECISION_LOG.md` instead.

## What Quintek V1 does NOT guarantee

**It does not establish that any model is medically safe or medically
correct.** Everything Quintek measures is technical: does the model return
usable structured output, does a validator layer catch a defect, does an
independent judge add information the other layers do not already have. A
model that passes every gate here has demonstrated that it behaves, not that
it is fit to teach medicine. That judgement needs qualified human experts,
which V1 does not have.

**Capability is not quality.** A capability probe establishes that a model can
emit JSON and follow a reasoning step. It says nothing about the quality of
its medical judgement, and the two are never combined into one score.

## External dependencies V1 cannot satisfy

| Limitation | Why it stands |
|---|---|
| **No qualified reviewer pool** | The blinded queue, two-rater assignment, sentinel monitoring, senior adjudication and gold-challenge ledger are all built and tested against synthetic labels. They are the mechanism reviewers would use, not a substitute for reviewers. Kappa gates cannot run without at least two qualified raters. See `REVIEW_CAPACITY.md`. |
| **No expert corpus** | The full benchmark expects ~3,850 expert-authored items (800–1,200 hours). A model authoring the gold it will be graded against is precisely the failure the benchmark exists to prevent, so this cannot be closed from inside. |
| **No embedding model** | `BAAI/bge-small-en-v1.5` is not obtainable in this environment. `score_near_duplicate_rate` and `score_family_coverage` are built and tested against synthetic similarity values; no real embedding has been computed. |
| **Contamination battery partial** | Split isolation and holdout-path access are enforced in code. The C1/C2/C6 retrieval checks against public corpora and a temporal holdout need an external corpus. See `CONTAMINATION_PROTOCOL.md`. |

## Limitations of the qualification path as configured

**Judge independence reaches Tier 2 on family, not on provider.** The
independence rule wants a different model family and, where practical, a
different provider. The candidate (`deepseek`) and judge (`ising`) are
different families, which the rule requires and which is enforced in code.
Both are served by NVIDIA because NVIDIA is the only authorized provider, so
the provider half is unmet. A correlated provider-side failure would hit both
seats together. Recorded rather than worked around: adding a provider to
satisfy it is out of V1 scope.

**Model context windows are unknown, so the strict shortlist is empty.**
NVIDIA's catalogue does not publish `context_window`. `ROLE_REQUIREMENTS`
for the validation role asks for at least 32k, and `eligible()` treats an
unknown value as a refusal rather than an assumption — which is the correct
direction, and means `tools_discovery.py shortlist` returns zero validation
candidates. Qualification therefore rests on directly OBSERVED capabilities
(`structured_output`, `reasoning`) rather than on a declared context size.
Nothing is inferred from a value that was never measured.

**The development corpus is marginal for its own specificity gate.** The clean
arm needs at least 35 scored items for even a flawless run to reach a 90%
lower bound, and 53 to clear it while tolerating a single false positive. The
corpus supplies 40. Specificity ≥ 0.90 is therefore reachable only with a
perfect clean arm. This is a property of the corpus, not of any model, and no
re-run changes it.

## Deliberate V1 boundaries

| Item | Decision |
|---|---|
| Six corpus governance fields (`review_status`, `challenge_history`, `corrections`, `adjudication`, `version`, `contamination`) | D012: not added. No gate reads them, and adding them would change `corpus_hash`, which the freeze pins — costing the comparability of every run against this corpus to buy nothing. |
| `GoldChallengeLedger` unwired | D012: it is the Phase 3 human-review mechanism, and V1 has no reviewers to operate it. |
| Generation prompt templates | `benchmark/prompts/` is an empty stub. The scoring side (`score_generation_rubric`) is built and tested; eliciting generations is not V1 work. |
| Exploratory-metrics reporting path | `SAMPLE_SIZE_AND_STATISTICS.md` separates primary gates from exploratory metrics; `report.json` has no field for the latter. Nothing smuggles an exploratory number into PASS/FAIL, because there is nothing to smuggle it from. |
| Automatic failure-record capture | The entities and query layer are built and tested against hand-built records; `runner.py` does not construct them automatically from a live run. |
| `analytics_api.py` as a production stack | Deliberately stdlib-only and framework-agnostic: a reference implementation of the contract, not a recommendation to run `http.server` in production. |
| Other provider adapters | The abstraction supports them; none exist because V1 authorizes one provider. |
| D010 (8B/70B plan vs the same-family judge prohibition) | Moot: both `meta/llama-3.1-8b-instruct` and `meta/llama-3.1-70b-instruct` were withdrawn by the provider (HTTP 410, end-of-life 2026-08-26) and are recorded RETIRED. The contradiction cannot arise. |

## Credential handling

The API key is referenced by environment-variable name only. Freeze
manifests, run records, journals, logs and commits record `credential_ref:
NVIDIA_API_KEY` and never the value; `validator/freeze.py` refuses to freeze a
manifest containing a credential-shaped field, and this is tested. Every
artifact produced by a run in this repository has been scanned for the live
key's value before being committed.
