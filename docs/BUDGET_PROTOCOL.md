# Compute and Cost Budget v0.4

## Why

A benchmark that requires tens of thousands of expensive API calls per candidate can become economically impossible to run, which creates pressure to skip tracks.

## Three modes

### Smoke
Purpose: developer feedback only.
- DEV only
- 20-50 items/track
- no official PASS
- no holdout access

### Standard
Purpose: candidate iteration.
- DEV + VALIDATION
- representative sample
- full scoring
- no official PASS

### Full
Purpose: official qualification.
- all required tracks
- registered sample sizes
- holdout + adversarial
- human adjudication
- official gates

## Budget controls

Config must specify:
- maximum model calls
- maximum input tokens
- maximum output tokens
- maximum wall-clock time
- maximum estimated cost in provider currency
- maximum retries

Budget scope MUST be declared as `per_candidate` or `global`. See `configs/v0_4.yaml`.

### The v0.3 defect

v0.3 specified a single ceiling of 25,000 calls and never stated its scope. Measured against v0.3's
own registered sample sizes, one full run costs approximately:

| Component | Calls |
|---|---:|
| Base candidate calls across all tracks | ~5,850 |
| Tier-2 judge calls on semantic tracks | ~2,450 |
| Variance sentinel re-runs (10%) | ~585 |
| Retries (5%) | ~290 |
| **Total per candidate** | **~9,200** |

So the undeclared ceiling silently capped a bake-off at two candidates — in a benchmark whose stated
purpose is comparing candidates against each other. Token ceilings were consistent with the same
limit, supporting roughly 2.7 candidates.

v0.4 declares per-candidate and global budgets separately and requires a preflight cost projection.

### Preflight requirement

The runner MUST project cost before executing and abort if the projection exceeds budget.
Discovering exhaustion mid-track produces a partially-scored run, which is the state most likely to
be reported as though it were complete.

## Budget failure

If a candidate exceeds budget:
> INCOMPLETE — BUDGET EXHAUSTED

Never convert incomplete coverage into a passing score.

## Cost normalization

Reports must include:
- total calls
- successful calls
- failed calls
- input tokens
- output tokens
- estimated cost
- cost per evaluated item
- cost per passing item/track where meaningful

## Efficient evaluation

Use:
- deterministic tests first
- cached immutable responses
- stratified samples
- judge only where semantic evaluation is necessary
- human review only for required cases
- no repeated calls when an immutable cached result is valid

Cache keys include candidate ID + benchmark version + item ID + prompt hash + execution config.
