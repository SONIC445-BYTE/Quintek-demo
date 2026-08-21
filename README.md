# Quintek

A PG medical revision app that writes questions from a student's own notes, and a frozen,
independent benchmark that decides which AI is allowed to write them.

## Status — read this first

The engine, the learner app, the benchmark layer and the billing system are built and tested.
What is **not** established is that any candidate model is good enough, and that distinction is the
whole point of the project.

Built and covered by tests:

| Layer | What runs |
|---|---|
| `student/` | auth, file upload, ingestion, concept extraction, generation, validation, attempts, revision scheduling, notifications, question bank, transparency, HTTP transport |
| `benchmark/` | gate registry, scoring, routing, health, fitness, evaluation scheduling, promotion, corpus, adversarial battery, provider status and candidate funnel |
| `billing/` | plans, entitlements, append-only usage ledger, atomic reservations, provider cost ledger, subscriptions, webhooks, economics, compute budget, anti-abuse |
| `frontend/` | learner app, admin console, public pricing page, four API clients |
| `android/` | WebView shell over the built single-file bundles |

Three things remain true and must not be papered over:

1. **The gold corpus does not exist.** Roughly 3,850 expert-authored items are required, at an
   estimated 800–1,200 hours of qualified medical author time. `corpus/development.jsonl` is
   model-authored and every item in it carries `gold_standard: false`, enforced in code with no
   override flag. No model may author or verify the gold it will be graded against. See
   `docs/REVIEW_CAPACITY.md`.
2. **The thresholds are uncalibrated.** Every gate value is an engineering starting point that has
   never been validated against real candidate behaviour. Per the gate registry, a run against
   uncalibrated thresholds cannot yield an official PASS.
3. **Only some source kinds can be read.** `GET /capabilities` reports which,
   derived from the same conditions ingestion branches on. Text and PDF work;
   links need outbound fetching and an HTML-to-text pass, photos need OCR, and
   video needs a transcript source. The picker marks the rest unavailable with
   the reason, before the learner commits a file rather than after.
4. **No validator has passed the adversarial battery.** The measured 8B validator caught 11 of 20
   planted defects and false-flagged 9 of 10 clean items. It is not fit for purpose and is not
   presented as if it were.

Do not claim the benchmark is built until `docs/IMPLEMENTATION_ACCEPTANCE.md` passes in full.

## Running it

```bash
python3 -m pytest -q -m "not browser"     # the suite (~870 tests)
python3 -m pytest -q -m browser           # the pricing page, through real Chromium
node tests/frontend/data_origin.test.mjs  # the frontend's data-origin rules

python3 -c "from student.server import serve; serve()"   # API + billing on :8500
python3 tools_build_standalone.py                        # single-file bundles + Android assets
python3 tools_compute_budget.py --revenue 100000 --paying 240
python3 tools_razorpay_sync.py check                     # gateway credential diagnosis
```

The server mounts billing under `/billing`, so the learner's profile (`/me`) and their usage
(`/billing/me/usage`) cannot collide. Credentials are read from the environment
(`RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`) and are never committed.
With none configured the payment surface is disabled rather than armed and broken.

## The absolute rule

```
User pipeline:  source -> notebook -> concept graph -> questions -> revision
Benchmark:      frozen independent corpus -> candidate -> evaluation -> scorecard
```

The first can never write to the second. A model may never create the gold answer and then grade
itself.

## What it measures

Medical factual QA, concept extraction, concept resolution, relationship extraction, cross-subject
linking, PG-level question generation, question validation, fake-mastery resistance, robustness,
prompt injection, and critical medical error rate — each gated independently, with no aggregate
score that could average a failed track away.

## Start here

`docs/INDEX.md` gives the reading order and authority hierarchy.

`configs/gate_registry_v0_4.json` is the single source of truth for every threshold. No other file
may state one.

## Scope limitation

This is an engineering qualification instrument for an educational tool. A PASS means a candidate
configuration met pre-registered engineering gates on a frozen corpus. It is **not** a claim of
clinical safety, clinical validity, or regulatory fitness, and must never be reported as one.
