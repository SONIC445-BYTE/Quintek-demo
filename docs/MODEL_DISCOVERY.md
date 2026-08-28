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
| `tools_discovery.py` | `catalogue`, `probe`, `status`, `shortlist`, `check-experiment` |
| `tests/test_discovery.py` | 56 tests, all against a fake transport |

and three edits to what was already here: `MODEL_RETIRED` and
`BILLING_BLOCKED` in `provider_status.py`, a retirement filter in
`quintek_router.py`, and the removal of the hard-coded default model ids.

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
python3 tools_discovery.py status
python3 tools_discovery.py shortlist --role validation --explain
python3 tools_discovery.py check-experiment --freeze reports/validator_runs/..._freeze.json
```

`probe` works from `due_for_recheck`, so it spends calls on models a probe
could tell us something new about rather than on everything every time. A cron
entry running `catalogue` then `probe` is the whole operating loop; the
intervals in `configs/discovery.json` decide what a run actually does.

## What is still missing

**Capability probing for bare catalogues.** NVIDIA and Cerebras return
`{id, object, created, owned_by}` and nothing else, so every capability is
`None` and `shortlist --role validation` correctly returns **zero** candidates
— structured output and reasoning are unknown, and unknown is not yes.
`benchmark/candidates.py:apply_capability_probe` already merges an *empirical*
capability probe onto bare entries; nothing produces one. Until something does,
role shortlists on those two providers stay empty, which is the honest answer
and not a usable one.

**Provider quota as a routing signal.** `RATE_LIMITED` carries backoff and
fallback, and the breaker stops the bleeding, but no remaining-quota figure
feeds selection. `benchmark/evaluation.py`'s "quota" is the evaluation coverage
matrix, a different thing entirely.

**`benchmark/orchestration.py` does not consult the breaker.** Its failover
loop excludes a failed candidate *within one `generate()` call* and forgets by
the next one. `benchmark/batch.py` does check `health.allows()`. Wiring the
health registry and this registry into the orchestration path is the remaining
piece of automatic failover.
