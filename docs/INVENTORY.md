# Quintek — where the project actually stands

**Written 2026-09-17.** One document, so picking this project back up takes one
message rather than a re-derivation. Everything below is either verified by
execution or marked UNVERIFIED. Nothing is rounded up.

Supersedes nothing. Read alongside `docs/DECISIONS.md` (the ADRs, which carry
the reasoning) and `docs/NOT_BUILT.md` (the list of absences).

---

## 1. The one-paragraph answer

The backend is built, migrated to PostgreSQL, deployed, and verified running.
The learner-facing app now reads live data on every screen — and until this
session it did not render at all, because the component had a syntax error that
shipped into the APK. **Generation is still refused**, correctly, because no
model has qualified; that gate is the whole project and it is blocked on two
things only you can do. Two defects are open and deliberately unfixed: one is a
policy decision, one is held under the report-before-fix rule.

---

## 2. Verified by execution

### Test suites, this session, on this commit

| Suite | Result |
|---|---|
| Python, SQLite | **1823 passed**, 29 skipped, 6 xfailed |
| Python, PostgreSQL 16.13 (real server) | **1847 passed**, 5 skipped, 6 xfailed |
| Frontend, `node --test` | **24 passed**, 0 failed |

The 6 xfailed are the two open defects in §4, held as `strict=True` so they
fail the moment either is resolved. The 24 extra passes on PostgreSQL are the
dialect-compatibility tests that skip without a server.

### The deployment

`https://quintek-demo.onrender.com` — live.

| Checked | How, specifically |
|---|---|
| Serving on PostgreSQL in production mode | `/health` → `"persistence": "postgresql"`, `"environment": "production"`. The second field is the one that proves the startup guard ran. |
| Generation refuses | `/health` → `"generation": "no_qualified_model"`. Correct state, not a fault. |
| Data is genuinely in PostgreSQL | An account registered over HTTPS, then **read back with `SELECT` against the Render database**, independently of the API's own response. |
| Schema initialised | 25 tables in the `quintek_student` schema. |
| Rate limiting is live | 5 registrations accepted; the 6th and 7th answered `429`. |
| The APK's bundles are valid JavaScript | Extracted from `app-debug.apk` and parsed with V8, not assumed. |

### The migration

Dual backend: `QUINTEK_DATABASE_URL` unset → SQLite, set → PostgreSQL. Verified
against a real PostgreSQL 16.13 server, including the concurrent-reservation
invariant, the pool under more requests than it holds, and RLS on every table.

**Five dialect incompatibilities have now been found.** Four were in
`schema.sql` and were closed by ADR-020. The fifth was found this session, in
`student/accounts.py` — see §4.

---

## 3. What changed this session

### The app did not parse

`frontend/PG Revision.dc.html` declared `const live` twice in one scope. A
`const` redeclared in one scope is a **parse** error, so the component class was
never constructed and **every screen rendered blank** — in `frontend/dist`, in
the android assets, and in `app-debug.apk`, from the revision-loop commit
onward.

Nothing caught it because nothing had ever asked a JavaScript engine to look at
the component: the frontend tests import the client *module* (a real ES module,
so a syntax error fails on import) and the Python tests read the built bundles
*as text*. `tests/frontend/component_parses.test.mjs` now parses all three
surfaces. ADR-027.

### Item T is closed in code

Every screen `APP_BEHAVIOUR.md` §2.6–2.8 called FIXTURE now reads live data or
says why it cannot:

| Screen | Reads |
|---|---|
| Progress — mastery split | `/progress` `colour_counts`, `due_count` |
| Progress — 12-week heatmap | `/progress` `activity`, keyed by date |
| Progress — per-concept recall | `/concepts` |
| Notebook list | `/notebooks` |
| Question bank / studio | `/questions` |
| Demonstrations | `/demos` |
| Reminders | `/reminders` — list, create, edit, cancel (ADR-031; the single-trigger `/settings/notifications` it replaced is gone) |
| Revision dashboard | `/revision/dashboard` |

Three pieces of arithmetic moved into `quintek-student-api.js` so they could be
tested against inputs, and each is mutation-tested against the specific wrong
implementation a careless test would not catch:

* **`activityGrid`** — `activity` is sparse and newest-first, so a positional
  `.map()` paints three real study days as three adjacent squares at the wrong
  end of the grid and presents that as twelve weeks of history.
* **`masteryPercent`** — `null`, not `0%`, for a concept nobody has been asked
  about. A genuine 0-of-4 still shows 0%.
* **`studyStreak`** — consecutive days, not a row count.

All four mutations (including a local-time variant of the grid) fail the tests.

Two things the sweep was supposed to find and did:

* **The strategy picker was inert.** It offered `WEAKNESS_FIRST`,
  `WEAK_PLUS_SECTION` and `COVERAGE`; the server accepts
  `adaptive/red/orange/green/due/unseen` and rejects the rest with 422 — and
  `startRevision` passed the literal `'adaptive'` regardless.
* **Every screen subtitle carrying a number counted a fixture** while the body
  below it rendered the learner's own data.

### Content safety reaches the lists

`_provenance_of` covered the two paths that hand over one question. The three
that hand over a **list** — the bank, a notebook's questions, a concept's
questions — all go through `question_bank`, which selected no chunk at all. A
stem is the question. `chunk_needs_review` and `chunk_confidence` now travel
with the list, three-valued: `null` means no chunk and therefore no verdict,
which must not render as "reviewed and fine".

The report path read `liveQuestionId`, set only inside a revision session, so
the only screen a learner could report from was the reveal. Any screen showing
a question can now report it.

### Operations, tested through the routes — and one of them was never called

`/ops/incidents`, `/ops/alerts` and `/ops/spend` had **no test**. The functions
behind them were covered thoroughly; the three routes that are the only way an
operator reaches them were not — the same shape as ADR-026. Now driven over a
socket against a running server: faults grouped by type rather than message,
the threshold not firing below it and not pooling two distinct faults, the
ceiling refusing and the refusal **not** written as spend, the ceiling reloaded
from history rather than reset per process, and per account.

Writing those tests surfaced something worse than a missing test.
`student/operations.py` — the incident table, the fault grouping, the alert
threshold and the spend ceiling — **had no production call site at all.**
`grep` for `operations.record`, `spend_guard` and `charge` outside `tests/`
returned only `tools_backup.py`, which uses the unrelated backup half. So:

* Every ingestion failure was recorded on the **source row**, where only the
  learner who uploaded it would ever see it. `/ops/incidents` was permanently
  empty in production and `/ops/alerts` could not fire however badly ingestion
  was failing.
* Nothing ever charged against the spend ceiling, so nothing was ceilinged.

`operations.py`'s own module docstring asserted that "`spend_guard()` wraps it
for the ingestion and generation paths". It never did. That sentence is
corrected.

**Fixed:** `IngestionEngine._record_incident` now writes an incident on both
failure paths, naming the affected account and the filename — never the
file's contents, since an incident is read by an operator who is not that
learner. Removing the two call lines fails the new test.

**Still open:** the spend ceiling. Connecting it needs a number — how many
generation calls one account may make per period — and that is a business
decision, not an implementation detail. Inventing one would put a made-up
figure in the path of every learner's spend. See §5.

---

## 4. Open defects

### 4.1 A learner who has answered a question cannot delete their account

**ADR-029. Half fixed. The other half is your decision, not mine.**

Found by writing a test that inspects the database after `DELETE /account`
rather than the response code. The old coverage asserted `status == 200` over
HTTP and checked rows only from a direct call — behind a fixture that created a
user, a notebook, and **no attempt**. No test had ever erased an account that
had been used.

**Fixed:** `tables_holding_user_data` enumerated the schema with
`sqlite_master` and `PRAGMA table_info`, so on PostgreSQL erasure died with
`UndefinedTable` before touching a row. **For as long as the service has been
live, a learner asking to be forgotten got a 500.** This is the fifth dialect
incompatibility and the first outside `schema.sql` — runtime introspection in
application code, which the ADR-020 audit was not looking at. The compatible
helpers already existed in `persistence/schema.py`; this function never called
them. Also fixed: table names quoted `'like this'`, which PostgreSQL reads as
a string literal.

**Open:** an account *with* an attempt still cannot be erased, on either
backend, because `attempts_are_immutable_delete` refuses — correctly. Two
deliberate invariants collide: an attempt is evidence, and evidence that can be
removed is not evidence; and a learner may ask to be erased. Both UPDATE and
DELETE are blocked, so the rows cannot even be anonymised in place the way
`incidents` and `spend_log` are.

Three options, written out in ADR-029 without a recommendation: anonymise
attempts (relax the trigger to permit clearing `user_id` only), delete them
(drop the DELETE half of the trigger), or retain them and say so in the brief.
This is a question about medical evidence retention against a data-protection
promise. **It is yours.**

The beta brief now warns testers rather than promising deletion.

### 4.2 The operator surface is enumerable by any logged-in learner

**ADR-028. Found, reported, NOT fixed — held under the standing rule that a
disclosure route is shown before it is closed.**

`_require_admin` answers 404 rather than 403 so the route is not advertised.
The **body** gives it away: an operator route says `no such route`, a genuinely
absent path says `no such endpoint: GET /x`. Seven paths are enumerable by
diffing two strings.

Severity: low. It discloses the *shape* of the admin API to an authenticated
learner and grants no access — every ownership filter checked in the same sweep
(`gap_evidence`, `resolve_gap`, `question_bank`, `notebooks`) correctly scopes
by `user_id`, and an anonymous caller learns nothing (every path answers 401
identically).

**The fix is two lines** and is described in the ADR. Say the word.

Worth keeping: it was found by asserting the *property* (indistinguishable from
a nonexistent route) rather than the status code. `assert == 404` would have
passed — both sides are 404.

### 4.3 Smaller, recorded in `NOT_BUILT.md`

* An unknown strategy and an empty queue both return 422, so a learner with
  nothing orange asking for the orange queue sees an error where an empty state
  belongs.
* Notification **delivery** is not built. Settings, schedule and log are real;
  no sender is configured, so "Send test" honestly reports that.

---

## 5. Blocked on you

| # | What | Why it cannot be done here |
|---|---|---|
| 1 | **Decide ADR-029** — how erasure and attempt-immutability resolve | A policy question about evidence retention vs a data-protection promise. Three options are written out. |
| 2 | **Authorise the ADR-028 fix** | Held under the report-before-fix rule. Two lines, ready. |
| 3 | **The validator rerun** | Needs a funded DeepSeek API key (~$0.20). The registration is built. |
| 4 | **The 28-item clean-label audit** | Needs a qualified clinician. A model re-checking a model-authored label is the same evidence twice — `docs/JUDGE_INDEPENDENCE.md` exists because that distinction is the project's basis. **At least one item labelled clean is known to be wrong**, so every specificity figure downstream carries the error. |
| 5 | **Create an admin account on the deployment** | There is no route and no CLI for it; `role` is set by direct SQL. **No admin exists on production right now**, so nobody can suspend a tester, read the report queue, or see incidents, alerts or spend. See §6. |
| 6 | **A device run** | Nothing in this repository has ever touched a phone. |
| 7 | **A signed release APK** | Needs a keystore, which is a credential. |
| 8 | **Delete 5 junk accounts I created** | See §6. |
| 9 | **Choose a per-account generation ceiling** | `spend_guard`/`charge` work, are tested against a running server, and are called by nothing. Wiring them needs a number, which is a business decision rather than an implementation detail. |

---

## 6. Two things I did to the live deployment

Stated plainly rather than buried.

**I created 6 accounts on production.** One (`persist-check@example.com`) was
the persistence test you asked for. Five (`throttle-probe-1..5@example.com`,
password `password123`) were created to verify the rate limiter fires on the
real service — it does, 5 accepted and the 6th refused with 429. Deleting them
afterwards was blocked by this environment's permission layer, so **they are
still there**. To remove them, either log in as each and `DELETE /account` (they
have no attempts, so erasure works), or from the Render database:

```sql
DELETE FROM quintek_student.users WHERE email LIKE 'throttle-probe-%@example.com';
```

**No admin account exists.** To make one, after registering the account
normally:

```sql
UPDATE quintek_student.users SET role = 'admin', name = '<your name>'
 WHERE email = '<your email>';
```

Until that happens the report queue is not merely unowned, as ADR-026 records —
it is unreachable, and so are incidents, alerts and spend.

---

## 7. State of the thing the project is actually about

**NO MODEL QUALIFIED / INSUFFICIENT EVIDENCE.** Unchanged, and correct.

* The gate registry is **uncalibrated** — 14 gates whose thresholds are
  explicitly engineering starting points, not calibrated values.
* The validator holdout is **1 of 5 uses spent**. The ledger holds one
  `inspection` entry dated 2026-08-21, and `validator/holdout.py:212` counts
  every entry against `MAX_USES = 5`. (Earlier reports in this project said
  "0 of 5". That was a reporting error, not a ledger change.) The holdout is
  53 clean / 30 defect / 10 edge / 30 mutation items.
* The development corpus is `model_authored`, `gold_standard: false`,
  `reviewed_by: ""` across all 100 items.
* `QUINTEK_DEV_CANDIDATE` and `QUINTEK_DEV_VALIDATOR_CANDIDATE` both bypass
  promotion. With `QUINTEK_ENV=production` the server refuses to start if
  either is set, and names both at once rather than one per redeploy. Production
  has neither.

Nothing in this session's work moves any of that, and nothing in it should be
read as progress towards it. The app being real makes the refusal visible to a
learner; it does not make the refusal any closer to lifting.

---

## 8. What I would look at next, if you want a view

Not started, not authorised, listed so the question does not have to be
re-derived:

1. **Decide 4.1 and 4.2.** Both are one message from you and both are blocking
   an honest beta.
2. **A device run.** It is the largest single unverified surface, and three
   manual-test rows are marked auto-verified with a caveat that says exactly
   this.
3. **Give me a per-account generation ceiling** and the spend guard goes live
   in one change. Everything else about it already works.
4. **`render.yaml` describes a service that is not the one running** —
   `plan: starter` with a health check, versus the live `plan: free` without
   one. Either reconcile it or mark it as intent rather than record.
