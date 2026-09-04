# Quintek — Deployment

## What deploys

One web service. `serve-student --with-console` answers both Android screens
from one origin, because the app has one backend setting and its two screens
read two different globals (`__QUINTEK_STUDENT_API__`, `__QUINTEK_API__`).

    Android / browser
        └── HTTPS ──▶ Render: quintek-api
                        ├── StudentAPI      learner routes, /ai/eval, /ai/benchmark/*
                        ├── BillingMount    /billing/*
                        └── AnalyticsMount  /api/*, operator /ai/*
                                └── SQLite (EPHEMERAL) · NVIDIA (optional)

## Health

`GET /health`, unauthenticated, no session. Reports three things:

| Field | Meaning |
|---|---|
| `status` | `ok`, or `degraded` with HTTP 503 when the database is unreachable |
| `persistence` | `postgresql` or `sqlite` — so a deployment that silently fell back to an ephemeral file is visible from outside, not only after the redeploy that loses the data |
| `environment` | `production` or `development` |
| `database` | the one dependency whose absence makes the server useless |
| `generation` | `available`, `no_qualified_model`, or `unavailable` |

`no_qualified_model` is **healthy**. Refusing to generate when nothing is
qualified is correct behaviour, and a platform must not restart the service
over it. Only an unreachable database returns 503.

## Environment contract

| Variable | Required | Purpose |
|---|---|---|
| `PORT` | supplied by Render | listen port |
| `QUINTEK_ENV` | production only | set to `production` to enable the startup checks below |
| `QUINTEK_DATABASE_URL` | **yes in production** | PostgreSQL connection string, including `sslmode=require`. Unset selects SQLite, which is correct locally and fatal in production |
| `QUINTEK_CORS_ORIGIN` | **yes in production** | browser origin allowlist. `*` is needed by the Android WebView, whose origin is the literal string `null`; the app authenticates with a bearer header, not cookies |
| `NVIDIA_API_KEY` | **optional** | only the production AI path uses it. Absent, the server starts and generation refuses — which is the current correct state |
| `RAZORPAY_KEY_ID` / `_KEY_SECRET` / `_WEBHOOK_SECRET` | **optional** | absent, the server announces `gateway=NONE` at boot and checkout refuses; everything else works |
| `QUINTEK_DEV_CANDIDATE` | **never in production** | development override. Setting it in a production deployment would let an unqualified scripted provider serve learners |

No secret is read from a file, a commit, or an asset. Every one is an
environment variable entered in the Render dashboard.

## Persistence

**Managed PostgreSQL, addressed by one variable.** `QUINTEK_DATABASE_URL`
unset selects SQLite; set selects PostgreSQL from a bounded pool. With
`QUINTEK_ENV=production` the server **refuses to start** without it, because
booting onto Render's ephemeral disk looks healthy and loses every account on
the next redeploy.

The port was not a dialect pass. Four incompatibilities were found against a
real PostgreSQL 16, three of them silent — the DDL loaded cleanly and the
failure waited for live data. See ADR-020 for each one; every one has a
regression test verified to fail on the unported code.

| | |
|---|---|
| Schemas | `quintek_student`, `quintek_billing`, `quintek_inference` — never `public` |
| RLS | enabled, no policies, on every table |
| TLS | `sslmode=require` added when the URL does not specify one |
| Pool | bounded (8), connections returned at the end of each request |
| Prepared statements | disabled (`prepare_threshold=None`) for transaction-mode pooling |

### Why the tables are not in `public`

Supabase exposes the `public` schema over PostgREST using the project's anon
key, and **that key is not secret** — it ships inside client applications. A
table in `public` with RLS off is readable by anyone holding the project URL
and that key. For `users` that is email addresses and password hashes.

Two independent fences: the tables are not in `public`, and RLS is on with no
policies. Quintek is unaffected because it connects as the table owner, which
bypasses RLS. `FORCE ROW LEVEL SECURITY` is deliberately not used — it would
apply to the owner too and lock the application out of its own data.

Quintek uses **no Supabase client keys at all**. Anon key, service-role key
and JWT secret are not read anywhere and must not be configured.

## Production startup checks

`QUINTEK_ENV=production` turns on `student/production.py`. The server refuses
to start, listing every problem at once, if any of these hold:

* `QUINTEK_DATABASE_URL` is unset — data would land on an ephemeral disk
* the URL sets `sslmode=disable` — the password would cross the network in clear
* `QUINTEK_CORS_ORIGIN` is unset — it must be a decision, not a default
* `QUINTEK_DEV_CANDIDATE` is set — it would let an **unqualified** model answer
  learners' medical questions

Missing `NVIDIA_API_KEY` and Razorpay keys are **not** boot failures. Refusing
to generate, and refusing to sell, are correct states; turning them into
startup errors would make the honest state unreachable.

No message prints a secret's value. `/health` and the boot banner report
whether each one is *configured*, never what it is.

### CORS

`*` is required by the Android WebView: its bundle loads from
`file:///android_asset/`, so its Origin is the literal string `null`, which
cannot be allowlisted. It is safe here because the app authenticates with a
bearer header rather than cookies, and CORS forbids `*` alongside credentialed
requests. Production requires the value to be **set**, not to be narrow.

## Deploying

1. Create a managed PostgreSQL instance (Supabase or otherwise). Nothing else
   is needed from it — no project keys, no PostgREST configuration.
2. Connect the repository to Render; `render.yaml` is picked up as a blueprint.
3. Enter `QUINTEK_DATABASE_URL` in the Render dashboard. It is `sync: false`
   and is never committed. Include `sslmode=require`.
4. Enter any optional secrets. None is required to boot.
5. Deploy. Render polls `/health`. Confirm it reports
   `"persistence": "postgresql"` — if it says `sqlite`, the variable did not
   reach the process and the deployment must not be used.
6. Point the Android app at the service's HTTPS URL.

## Status

**Not deployed.** Everything above is verified against a local PostgreSQL 16
and two built APKs; no Render service exists, no Supabase project exists, and
no APK has been installed on a device. See ADR-025 for the exact list of what
remains unverified and why.
