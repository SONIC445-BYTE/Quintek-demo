# Frontend — wiring to the benchmark backend

The design files in this directory are the Quintek engineering dashboard and student app.
They are vendored here unchanged except for the two API modules, which gained a live-backend
seam (see below).

| File | What it is |
| --- | --- |
| `Quintek Admin.dc.html` | Benchmark console (desktop) — the admin UI |
| `PG Revision.dc.html` | Student app (mobile) |
| `PG Revision standalone.dc.html` | Offline single-file copy of the student app |
| `Quintek Harness.dc.html`, `Quintek Audit.dc.html` | Harness and audit views |
| `Quintek PG Revision.html` | Earlier standalone student build |
| `quintek-report-api.js` | Run-centric contract — `report.json`, gates, datasets, preflight |
| `quintek-eval-api.js` | Candidate-centric contract — published evaluation, leaderboard, history |
| `QUINTEK_LOGIC.md` | Product logic reference (authored upstream, not by this repo) |

## Going live

Both API modules default to their built-in fixtures, so every file still opens standalone
with no server running. Point them at a backend by setting one global **before** the module
is imported:

```html
<script>window.__QUINTEK_API__ = 'http://127.0.0.1:8420';</script>
```

Then start the backend from the repository root:

```
python -m benchmark.cli serve-analytics --port 8420
```

`quintek-eval-api.js` fetches `GET /ai/eval` once at module load (a module-level `await`,
because the screens read `api.candidates` synchronously as soon as `import()` resolves) and
re-exports the live payload under the same names it previously exported fixtures under.
`quintek-report-api.js` fetches per call.

Check `isLive` / `isConfigured` to tell which source a screen is showing. If the backend is
unreachable both modules fall back to fixtures and record why on `loadError` / `lastError`
rather than throwing — but a dashboard showing demo data must say so, so surface that flag
rather than letting stale figures pass as live.

## Endpoints these modules consume

Run-centric (`quintek-report-api.js`):

```
GET  /api/runs                     paginated summaries, newest first
GET  /api/runs/:run_id             the unmodified report.json
GET  /api/runs/:run_id/integrity   integrity block
GET  /api/runs/:run_id/report.md   rendered scorecard (text/markdown)
GET  /api/gates                    the gate registry
GET  /api/datasets/:hash           a recorded validation result
GET  /api/preflight?dataset=       cost projection + outcome ceiling
POST /api/datasets/validate        re-validates server-side; 422 if unscoreable
POST /api/runs                     501 unless an execution backend is configured
```

Candidate-centric (`quintek-eval-api.js`):

```
GET /ai/eval                       everything below, in one response
GET /ai/eval/state                 ok | empty | incomplete
GET /ai/eval/overview?candidate=
GET /ai/eval/tracks?candidate=
GET /ai/eval/track-detail?candidate=
GET /ai/eval/candidates
GET /ai/eval/history
GET /ai/eval/runs
GET /ai/eval/failures
GET /ai/eval/overall-by-candidate
```

## Fields that report `null` on purpose

The backend never substitutes a plausible number for one it does not have. Three fields the
UI renders will be `null` until something real supplies them, and the UI already has a null
state for each:

- **`costPer1k`** — needs `configs/model_costs.json`. Copy `configs/model_costs.example.json`
  and fill in figures from your own provider contract. A price is a commercial fact, not
  something a benchmark run can derive.
- **`latencyMs`** — the median of real orchestrator executions, from `executions.jsonl`. A
  candidate that has never been executed reports `null`, not `0`.
- **`invalid` / `unsafe` / `failedValidation` / `humanReview`** inside `trackDetail[].outcomes` —
  the gate engine records a pass count and an `n`, not a taxonomy of failure modes.

Zero would assert "we looked and found none". `null` says "not measured". Do not render a
`null` as `0`.

## Suppressed runs

When `outcome` is `INVALID_RUN` or `INCOMPLETE` the report carries `scores: null` — never
`{}`, never partially filled. `assertSuppression()` in `quintek-report-api.js` enforces this
at the boundary and is exercised against real backend output by `tests/test_runs_api.py`.
`safety` and `reliability` are also `null` on those runs: a run whose controls failed
measured nothing, including safety, and an absent block must never read as "zero events".
