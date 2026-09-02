# Dynamic model discovery, and why it is not the freeze

## The incident this exists because of

On 2026-08-28 a preflight for the validator's Phase 0 experiment probed the two
models the experiment was to be frozen against:

```
meta/llama-3.1-8b-instruct    HTTP 410  "has reached its end of life on 2026-08-26T09:00:00Z"
meta/llama-3.1-70b-instruct   HTTP 410  "has reached its end of life on 2026-08-26T09:00:00Z"
```

The credential was fine — a 410 is the host answering. Nothing in this
repository had noticed, and nothing could have:

- **No code fetched a catalogue.** `benchmark/candidates.py` could *read* a
  discovery snapshot (`load_catalogue`, `load_discovery_snapshot`,
  `apply_probe_results`) and did so well. Nothing *wrote* one. The files in
  `discovery/` were produced out of band on 2026-08-20 and committed.
- **Model ids lived in Python.** `provider_registry.default_registry()` named
  `meta/llama-3.1-8b-instruct` as NVIDIA's default model; `tools_provider_matrix.py`
  named it again in `DEFAULT_MODELS`. Changing either meant editing and
  redeploying.
- **410 was not a category.** `provider_status.classify` had no rule for it, so
  it fell through to `UNKNOWN_ERROR`: retryable once, circuit reopening every
  sixty seconds, forever. A permanently dead model on a permanent drumbeat.
- **`discovery/shortlists.json` was a committed answer.** Three roles with
  their models spelled out. A retirement made the file wrong and nothing said so.

## Two systems, opposite requirements

This is the distinction the whole design rests on, and collapsing it is how you
get either a product that dies with its provider or a benchmark that cannot be
read.

| | production | experiment |
|---|---|---|
| when a model retires | route elsewhere, automatically, no deployment | **stop**; the set is BLOCKED |
| source of truth | the current catalogue, reconciled | the frozen manifest |
| may a model be substituted? | yes, that is the point | never |
| lives in | `benchmark/discovery.py` | `validator/freeze.py` |

They meet in exactly one function,
`DynamicModelRegistry.blocked_experiment_models`, which **reports** a frozen
model that has died and offers nothing in its place. `tools_validator_eval.py
experiments` calls it as a preflight and refuses to start. A discovery tool
that could re-point a frozen manifest would make every number under that digest
unreadable, so no code path does.

## What was added

| file | what it is |
|---|---|
| `benchmark/discovery.py` | the persistent model registry, its states, reconciliation, retirement detection and the production funnel |
| `benchmark/provider_catalogue.py` | the missing producer: per-provider catalogue fetch and minimal availability probe, over an injected transport |
| `configs/discovery.json` | every interval, as configuration |
| `benchmark/capability_probe.py` | the empirical qualification step: what a model can actually do, when its provider will not say |
| `tools_discovery.py` | `catalogue`, `probe`, `capability-probe`, `status`, `shortlist`, `check-experiment` |
| `tests/` | `test_discovery.py`, `test_capability_probe.py`, `test_orchestration_health.py`, `test_selection_safety.py`, `test_no_stale_model_ids.py` — 125 tests, every provider interaction through a fake transport |

and edits to what was already here: `MODEL_RETIRED` and `BILLING_BLOCKED` in
`provider_status.py`; `CircuitBreaker.open_now`; a retirement filter in
`quintek_router.py`; shared health in `orchestration.py`; `rank_key` in
`fitness.py`; `DEPRECATED` reachable from any non-terminal state in
`registry.py`; and the removal of every hard-coded default model id.

## The state vocabulary

```
UNVERIFIED   seen in a catalogue, never called. Every model starts here.
AVAILABLE    a real completion succeeded. Only a probe may set this.
TEMPORARILY_UNAVAILABLE   timeout, egress block
BILLING_BLOCKED           402: the key is valid and the account cannot pay
AUTH_FAILED               401/403
RATE_LIMITED              429: our request rate, not the provider's health
NOT_SERVING               404: this account is not entitled to it
RETIRED                   410, or sustained absence. Terminal.
UNKNOWN                   unclassified
```

`RETIRED` is the only terminal state, and it is the only one excluded from
re-probing — `retired_recheck_seconds` defaults to `null`, because a schedule
that re-probes a withdrawn model is a permanent low-grade waste with no
reachable success. An operator can turn it on.

## Three facts kept apart

**Catalogue presence is not availability.** NVIDIA listed 83 models on
2026-08-28; `openai/gpt-oss-120b` timed out and `mistralai/mistral-7b-instruct-v0.3`
returned 404. A catalogue observation may raise `catalogue_present` and update
metadata. It may never set `AVAILABLE`.

**Absence from a catalogue is not retirement.** The same measurement, the other
way round: `nvidia/nemotron-mini-4b-instruct` and
`nvidia/llama-3.1-nemotron-nano-vl-8b-v1` were `SERVING` on 2026-08-20 and
absent from the 2026-08-28 listing. So retirement needs either an explicit 410
or `absences_before_retired` (default 2) consecutive absences, and the record
says which. A model retired on absence alone and later listed again reopens at
`UNVERIFIED`; a model retired on a 410 does not reopen at all.

**Not entitled is not withdrawn.** A 404 from NVIDIA reads
`Not found for account 'lq1Z…'` — an entitlement fact a billing change
reverses. A 410 reads `end of life`. Collapsing them puts a dead model on a
re-probe schedule and keeps a live one out of the pool.

## A failed fetch is not an empty catalogue

`fetch_catalogue` returns `ok=False` with zero observations rather than
raising, and the caller **must not reconcile against it**. Reconciling an empty
list would mark every model that provider has ever served as absent, and two
such runs would retire the entire catalogue. `tools_discovery.py catalogue`
prints `NOT RECONCILED` and moves to the next provider.

## Selection reads attributes, never names

Nothing in `eligible()` or `sort_key` consults a model's name, vendor, or
reputation. `test_23_no_selection_depends_on_a_model_or_vendor_name` asserts it
by building the same registry twice — once with real ids, once with `m0`/`m1` —
and requiring the same ordering.

Two rules that look alike and are not:

- an unknown capability is **not eligible**, because "we do not know" must not
  read as "yes";
- an unknown capability is **not permanently excluded**, because the record
  stays and a probe can change it. `eligible()` is a view, never a state change.

Pricing has four states rather than a nullable float, because `0.0` answers
three questions and gets two of them wrong: `UNKNOWN` (the provider publishes
no prices), `UNPRICED` (a sentinel — OpenRouter's `-1`, priced at request
time), `FREE` (a real zero) and `PAID`. Neither of the first two may win a
cheapest-first sort, and neither can be shown to be within a budget ceiling.

## History survives retirement

A record is never deleted. `first_seen`, every probe event, `probe_successes`,
`latency_ms_best` and the retirement's own reason and timestamp all persist, and
`benchmark/inference_log.py` — which this module does not touch — keeps every
inference the model ever served. "What was serving generation in March, and why
was it dropped" stays answerable after the model is gone. A retired model simply
cannot be *selected*.

## Running it

```bash
python3 tools_discovery.py catalogue --providers nvidia --snapshot   # 1 request
python3 tools_discovery.py probe --providers nvidia --limit 20       # 1 call each
python3 tools_discovery.py capability-probe --role validation --dry-run   # forecast only
python3 tools_discovery.py capability-probe --role validation       # 3 calls per model
python3 tools_discovery.py status
python3 tools_discovery.py shortlist --role validation --explain
python3 tools_discovery.py check-experiment --freeze reports/validator_runs/..._freeze.json
```

`probe` works from `due_for_recheck`, so it spends calls on models a probe
could tell us something new about rather than on everything every time. A cron
entry running `catalogue` then `probe` is the whole operating loop; the
intervals in `configs/discovery.json` decide what a run actually does.

## Capability provenance: three states, not two

`benchmark/capability_probe.py` closes the gap this document previously listed
as open. A capability claim now carries where it came from:

| | |
|---|---|
| `DECLARED` | the provider's catalogue said so |
| `OBSERVED` | a probe sent a real request and inspected the reply |
| `UNKNOWN` | nobody has said or shown anything — **and that is not `False`** |

A probe that could not run leaves the claim `None` and is counted as
inconclusive. `record_capability_probe` refuses to store `False` for it,
because recording a 410 or a timeout as "cannot do structured output" would
disqualify a model permanently for an outage. A catalogue may never overwrite
an `OBSERVED` claim: the catalogue is what the provider says, the probe is what
happened, and when they disagree the probe is the one that sent a request.

Probes establish a **floor**, never a grade. `structured_output=True` means one
strict-JSON request came back parseable with the requested key. Nothing here
ranks anything. And a name is never evidence:
`nvidia/llama-3.2-11b-vision-instruct` gets the same red PNG sent to it as
everything else and is believed only if it answers "red".

The funnel keeps the bill small — `due_for_capability_probe` skips anything not
`AVAILABLE`, any router, anything already known to lack a required capability,
and anything whose answers are all already in. A failed text-output
prerequisite stops the pass rather than paying for four more probes against a
model that could not answer a one-word question. `long_context` is opt-in: its
input is measured in thousands of tokens where every other probe is in tens.

## Lifecycle is derived, never stored

```
UNVERIFIED -> PROBED -> QUALIFIED -> EVALUATED -> PRODUCTION_ELIGIBLE
                |           |
                |           +-> DISQUALIFIED       (probe showed it cannot)
                +-> TEMPORARILY_UNAVAILABLE -> recovers, resumes
                +-> RETIRED                        (terminal)
```

`benchmark/registry.py` keeps a written state machine for the promotion
*decision*, which belongs there because a promotion is a decision somebody
makes. This is not a decision — it is a summary of what is currently known —
and a stored summary drifts from the facts it summarises. Recovery from an
outage restores `QUALIFIED`, never `PRODUCTION_ELIGIBLE`: the evaluation
evidence is a separate gate and coming back online does not grant it.

`Status.DEPRECATED` is now reachable from every non-terminal state. It was
reachable only from `ELIGIBLE`/`PRODUCTION`, so a candidate sitting
`REGISTERED` whose model the provider withdrew had nowhere to go — the row
could only be left offering a dead model or deleted, losing the history. This
widens only the exit; the route *into* `ELIGIBLE` still runs solely through
`EVALUATING`.

## Orchestration shares the health state

`Orchestrator.generate` kept its `tried` set in a local variable and nowhere
else. Within one call a failed candidate was excluded correctly; the next
independent call started clean and selected it again — so a model answering 410
was re-selected on every request, forever, while `benchmark/batch.py`, which
does consult `health.allows()`, stopped after three.

It now reads and writes the same `HealthRegistry`. One classification, in
`_observe`, drives the execution record, the breaker and the model registry
together; classifying separately in each place is how two of them end up
disagreeing about whether a model is usable. Passing a `DynamicModelRegistry`
as well makes a retirement seen in production outlive the process.

Underneath it was a second defect: `HealthRegistry.observe` sent every
circuit-opening status through the three-strike threshold. A 410, a 401, a 402
and a denied CONNECT are conclusive on the first observation, and the status
policy already said so — `open_circuit and not retryable` — but nothing read
it. `CircuitBreaker.open_now` acts on it. A timeout still needs the threshold,
because one timeout is not evidence of anything.

## Cheap is not a reason

`blend` renormalises over the components actually measured, which is right:
treating "no cost data" as "infinitely expensive" would bury an unpriced model
for a reason that has nothing to do with the model. But it makes scores from
different coverage incomparable, and nothing read the `weight_covered` it
recorded. Measured on this tree:

```
nvidia:cheap    fitness 1.000   from  20% of the weighting   (a price, nothing else)
nvidia:proven   fitness 0.936   from 100%                    (200 observations)
```

and production selected `nvidia:cheap`. `ModelFitness.rank_key` now sorts a
thinly-evidenced score below every fully-measured one, while leaving it
`eligible` so exploration can still reach it — a candidate that can never be
picked can never be measured. Cost is 3–20% of a profile against quality's
30–55%, and latency and success rate are hard constraints applied before the
arithmetic, not weights inside it.

## The guard that catches the next one

Everything above responds to the 2026-08-26 retirement *after the fact*. The
root cause is that a model id written into Python source is a claim nobody
re-checks. `tests/test_no_stale_model_ids.py` is the re-check: it walks the
repository, finds every literal model id in code that could reach a provider,
and fails if one names a model the registry has observed to be gone.

Hard-coded ids stay legitimate where they are a *record* rather than an
*instruction* — `discovery/`, `reports/`, `runs/`, `docs/`, `tests/`, and
docstrings. What is not exempt is a default argument or a spec dict, which is
exactly the shape that went stale: `--validator` defaulted to
`meta/llama-3.1-8b-instruct`, so the main path of a credit-spending tool would
have collected 410s and reported them as validator performance.

`tools_provider_matrix.py`, `tools_adversarial_run.py` and
`tools_seed_model_registry.py` now resolve models from the registry at run
time, and refuse rather than guess when nothing qualifies.

## What is still missing

**Quota is not observable.** None of the three adapters exposes a
remaining-quota figure, so it cannot be measured — which means it must never be
assumed either. The observable proxy is a 429: a `RATE_LIMITED` model is not
`AVAILABLE`, so it is not eligible, and it is re-probed on the backoff schedule
rather than never. `tools_compute_budget.py`'s `paid_only` already refuses to
let a free tier's rate limit quietly underwrite every question the product
sells. What does not exist is a forward-looking "how much of today's quota is
left" signal in the ranking.

**`tools_compute_budget.py` still reads `discovery/shortlists.json`.** That
file is a committed 2026-08-20 snapshot, used there for its *observed prices*
rather than as a routing answer, and NVIDIA publishes no prices for the
registry to replace them with. The cost figures it produces are historical
OpenRouter observations and should be read as such.

**NVIDIA is the only authorized inference provider.** Discovery has exactly
one source. An OpenRouter catalogue source was added and then reverted on
2026-09-02 (see `docs/DECISION_LOG.md` D009): OpenRouter is not an authorized
Quintek provider, and the separate Registry repository belongs to a different
provider/discovery project. Pre-existing OpenRouter references in
`benchmark/candidates.py`, `benchmark/provider_registry.py`,
`tools_provider_matrix.py` and `configs/model_prices.json` predate that change
and are untouched.
