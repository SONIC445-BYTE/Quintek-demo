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
| `database` | the one dependency whose absence makes the server useless |
| `generation` | `available`, `no_qualified_model`, or `unavailable` |

`no_qualified_model` is **healthy**. Refusing to generate when nothing is
qualified is correct behaviour, and a platform must not restart the service
over it. Only an unreachable database returns 503.

## Environment contract

| Variable | Required | Purpose |
|---|---|---|
| `PORT` | supplied by Render | listen port |
| `QUINTEK_DB_PATH` | yes | learner database path |
| `QUINTEK_BILLING_DB` | yes | billing database path |
| `QUINTEK_CORS_ORIGIN` | no (default `*`) | browser origin allowlist. `*` is needed by the Android WebView, whose origin is the literal string `null`; the app authenticates with a bearer header, not cookies |
| `NVIDIA_API_KEY` | **optional** | only the production AI path uses it. Absent, the server starts and generation refuses — which is the current correct state |
| `RAZORPAY_KEY_ID` / `_KEY_SECRET` / `_WEBHOOK_SECRET` | **optional** | absent, the server announces `gateway=NONE` at boot and checkout refuses; everything else works |
| `QUINTEK_DEV_CANDIDATE` | **never in production** | development override. Setting it in a production deployment would let an unqualified scripted provider serve learners |

No secret is read from a file, a commit, or an asset. Every one is an
environment variable entered in the Render dashboard.

## THE PERSISTENCE BLOCKER

**Render's filesystem is ephemeral.** The learner database, the billing
database and the run archive are SQLite files on local disk. Every redeploy
and every restart destroys them: accounts, notebooks, questions, attempts,
progress, revision schedules and billing records all reset.

That is acceptable for a staging deployment with disposable data. It is **not
acceptable for real learners**, and this document does not claim otherwise.

Production persistence requires an external Postgres — Supabase is the
proposed target. The port is real work, and its size was measured rather than
guessed:

* **~170 SQL call sites** across `student/` and `billing/`
* **21 tables** in `student/schema.sql`
* **2 immutability triggers** (`attempts_are_immutable_*`) and one
  `PRAGMA foreign_keys`, all with direct Postgres equivalents
* **a new runtime dependency** (`psycopg`), where the repository's only
  runtime dependency today is `pyyaml`

`student/db.py` anticipates this in its own docstring: *"Swapping it for
Postgres later is a connection string and a dialect pass, because nothing
below uses a SQLite-only feature except the immutability triggers, which have
direct equivalents."*

It is nonetheless an architectural decision — adding the first database driver
dependency to a deliberately stdlib-only codebase — and is recorded as open
rather than taken unilaterally.

## Deploying

1. Connect the repository to Render; `render.yaml` is picked up as a blueprint.
2. Enter any optional secrets in the dashboard. None is required to boot.
3. Deploy. Render polls `/health`.
4. Point the Android app at the service's HTTPS URL.

With HTTPS in place, `usesCleartextTraffic` can be removed from the Android
manifest; it exists only for LAN development against `http://192.168.x.x`.
