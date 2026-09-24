# Architectural Decision Record

Chronological. Governed by `CHANGE_PROTOCOL.md`.

## How to read this file

`docs/DECISION_LOG.md` (D001–D019) is the **primary contemporaneous record**,
written as decisions were taken. This file does not replace it and does not
restate it in different words. It provides the architectural spine — the
decisions a newcomer needs to understand the system — and cites D-numbers and
commits as evidence.

**Provenance labels used throughout:**

| Label | Meaning |
|---|---|
| `RECONSTRUCTED` | inferred from code, commit messages and artifacts, not from a contemporaneous rationale |
| `CONTEMPORANEOUS` | recorded in `DECISION_LOG.md` or a commit body at the time |
| `RATIONALE NOT RECOVERED` | the *what* is evidenced; the *why* is not. Requires confirmation |

The repository has **80 commits** from **2026-08-21 to 2026-09-03**. Commits
before `032d0f4` (2026-08-28) predate the sessions reconstructable here; for
those, only commit messages and resulting code are available, and they are
labelled accordingly.

---

# PART 1 — HISTORICAL RECONSTRUCTION

## ADR-001 — Quintek's purpose and boundary

**Date/phase:** pre-2026-08-21 · **Status:** IMPLEMENTED · **Provenance:** RECONSTRUCTED

**Decision:** Quintek is trustworthy-AI *infrastructure* for medical revision
content — it decides whether a model may serve a learner — not a tutor and not
a model.

**Evidence:** `docs/MASTER_BUILD_PROMPT_V0_4.md`; the separation of
`benchmark/` (evidence) from `student/` (product); `IMPLEMENTATION_STATUS.md`.

**Consequences:** Every later refusal follows from this. A system whose job is
to gate cannot also be the thing being gated, which is why judge independence
and the model-authored-gold prohibition are structural rather than advisory.

**Deliberately not changed:** the boundary. Requests to make generation "just
work" have been declined on this basis.

## ADR-002 — Four validator layers, A/B/C/D

**Date/phase:** pre-2026-08-21 · **Status:** IMPLEMENTED · **Provenance:** RECONSTRUCTED

**Decision:** A structural (free, no model call), B grounding (key supported by
the passage, with verbatim evidence spans), C independent judge (a different
model answers blind), D conformance (item matches its declared concept and
difficulty).

**Why:** `docs/VALIDATOR.md` records the measured failure that motivated
Layer C's independence requirement — llama-3.1-8b approved a question that
contradicted its own source passage.

**Trade-off:** Four layers cost four times the calls. `--only` and the
ablation design exist to make each layer's contribution measurable rather than
assumed.

**Files:** `validator/structural.py`, `grounding.py`, `judge.py`,
`conformance.py`, `pipeline.py`.

## ADR-003 — Freeze manifests and the validator fingerprint

**Date/phase:** 2026-08-21 (`c3b5e45`) · **Status:** IMPLEMENTED · **Provenance:** CONTEMPORANEOUS

**Decision:** A freeze pins corpus hash, model identities, prompt versions,
thresholds, sampling, retry policy, experiments and budgets under one digest.
The validator fingerprint hashes validator source + configuration.

**Evidence:** commit "Freeze the experiment set, and stop a test double
counting as a measurement".

**Later amended by ADR-012** when the fingerprint was found not to cover the
provider adapters.

## ADR-004 — Candidate/judge seats, not "provider"

**Date/phase:** 2026-08-21 (`dff7672`) · **Status:** IMPLEMENTED · **Provenance:** CONTEMPORANEOUS

**Decision:** The experiment names two *seats*. `assert_independent` refuses a
judge that authored the item or shares its model family (D004: a same-family
judge may never be the sole basis for a PASS).

**Trade-off:** Tier 2 also wants a different *provider* "where practical".
With one authorized provider that half is unmet — recorded as a limitation in
`FINAL_STATUS.md`, not worked around.

## ADR-005 — Budget in outbound attempts, wall clock separate

**Date/phase:** 2026-08-21 → 2026-08-25 (`2de453d`, `0fe8c35`, `868db38`) · **Status:** IMPLEMENTED · **Provenance:** CONTEMPORANEOUS

**Decision:** The canonical unit is the **outbound attempt**, retries included
— not planned calls. The wall clock is a second, independent ceiling.

**Why:** `validator/wallclock.py` states it: measured latencies of 0.6 s and
180.8 s on the same endpoint mean a run can respect every call ceiling and
still take hours. "How much money" and "how long" are different questions.

**Consequences:** A budget stop is a stop — the orchestrator returns without an
answer rather than trying a cheaper model.

## ADR-006 — Dynamic model discovery; retirement is an observation

**Date/phase:** 2026-08-28 (`032d0f4`) · **Status:** IMPLEMENTED · **Provenance:** CONTEMPORANEOUS

**Decision:** Availability is observed, never declared. Capability claims carry
provenance (`OBSERVED` / `DECLARED` / `UNKNOWN`), evidence text, timestamp and
probe version.

**Context:** NVIDIA retired both frozen Phase 0 models mid-project (HTTP 410,
end-of-life 2026-08-26). Eleven models were retired inside one week.

**Rejected alternative:** silently substituting an available model. Explicitly
refused — an arm run against a substituted model is not the arm the other rows
were measured with.

**Files:** `benchmark/discovery.py`, `provider_status.py`, `health.py`.

## ADR-007 — Single authorized provider; OpenRouter rejected

**Date/phase:** 2026-09-02 (`b4efcc0`) · **Status:** IMPLEMENTED · **Provenance:** CONTEMPORANEOUS (D009)

**Decision:** NVIDIA is the only authorized provider. OpenRouter changes
introduced during exploration were reverted; pre-existing references were left
untouched.

**Consequences:** Tier-2 provider independence is unreachable (see ADR-004).
Accepted as a limitation rather than resolved by adding a provider.

## ADR-008 — Deterministic V1 pairing (D011)

**Date/phase:** 2026-09-02 (`1fe7f5c`) · **Status:** IMPLEMENTED · **Provenance:** CONTEMPORANEOUS

**Decision:** Candidate `deepseek-ai/deepseek-v4-flash-0731` (family
`deepseek`), judge `nvidia/ising-calibration-1.5-31b` (family `ising`),
selected by a one-time deterministic rule over OBSERVED capability evidence.

**Note:** D013 records a consequence honestly — the rule forbade latency as an
input and selected the slowest of four qualified models, which contributed to
the first run's inability to finish. The rule was applied correctly; the
consequence is recorded rather than retrofitted.

## ADR-009 — Corpus governance: six fields deliberately absent (D012)

**Date/phase:** 2026-09-02 · **Status:** IMPLEMENTED · **Provenance:** CONTEMPORANEOUS

**Decision:** `review_status`, `challenge_history`, `corrections`,
`adjudication`, `version`, `contamination` are not added for V1.

**Why:** no gate reads them, and adding them would change `corpus_hash` — which
the freeze pins — costing the comparability of every run to buy nothing.

**This later proved double-edged:** the same reasoning that protects the hash
also means the corpus's *unreviewed* provenance could not be fixed in place.
See ADR-015.

## ADR-010 — Holdout protection

**Date/phase:** pre-2026-08-21 · **Status:** IMPLEMENTED · **Provenance:** RECONSTRUCTED

**Decision:** `MAX_USES = 5`, append-only ledger, fingerprint-keyed refusal of
a repeat scoring of the same validator.

**Current state:** **0 scoring runs consumed.** One `inspection` row exists
from Track D construction, recorded with the gap it revealed and explicitly
*not* acted on — the ledger note says a check widened to catch an item the
holdout revealed is a check tuned on the holdout.

## ADR-011 — Android as a WebView host over shipped bundles

**Date/phase:** 2026-08-21 → 2026-09-02 · **Status:** IMPLEMENTED · **Provenance:** RECONSTRUCTED

**Decision:** Two activities (`MainActivity` → STUDENT, `AdminActivity` →
ADMIN) over one `WebScreenActivity` base. Screens are single self-contained
HTML bundles in `assets/`, built by `tools_build_standalone.py`. The backend
origin is injected by intercepting the document request and prepending a
one-line `<script>`.

**Why injection at request time:** `RECONSTRUCTED from the code comment` —
`quintek-eval-api.js` reads its global at module-evaluation time, so injecting
after `onPageFinished` would be too late.

**No JavaScript bridge:** `addJavascriptInterface` is not used anywhere;
`allowFileAccess=false`, `allowContentAccess=false`.

## ADR-012 — Phase 0 attempt 1: INCOMPLETE; two of our own defects (D013)

**Date/phase:** 2026-09-02 (`1d30060`) · **Status:** IMPLEMENTED · **Provenance:** CONTEMPORANEOUS

**Defect 1:** `providers/nvidia.py` read only `message.content`; the candidate
is a reasoning model that leaves it null. **Defect 2:** the fingerprint hashed
only `validator/`, so repairing the adapter would have changed what a run
measures while leaving the digest identical. `benchmark/providers/` is now
inside the fingerprint.

## ADR-013 — Journalling, and the rules that keep a resume honest (D014, D016)

**Date/phase:** 2026-09-02 (`c2edda7`, `28c39ab`) · **Status:** IMPLEMENTED · **Provenance:** CONTEMPORANEOUS

**Decision:** Every reply is fsynced before use so an interrupted run resumes.
Three rules: a recorded outage replays as that outage (never re-asked); each
arm pays for its own calls; spend and elapsed carry across resumes.

**Then corrected by its own evidence (D016):** 32 instant "Connection refused"
failures during a container teardown would have replayed forever as model
outages. `ProviderStatus.UNREACHED` marks failures proving no connection was
established; those are not recorded. **Timeouts still are** — a timeout may
mean the request arrived, and the conservative direction is to record.

**This is Rule 10 in action:** the mechanism built to protect the record
nearly corrupted it, and the fix came from reading the record it kept.

## ADR-014 — The validator was parsing the HTTP envelope (D017)

**Date/phase:** 2026-09-03 (`a7970fb`) · **Status:** IMPLEMENTED · **Provenance:** CONTEMPORANEOUS

**Decision:** `raw_output` is the model's reply, never the transport that
carried it.

**Evidence:** `extract_json` takes the first *balanced* JSON object, and an
HTTP envelope is one. 91 of 94 grounding calls returned the envelope;
`supported` is absent from an envelope, so **every** item was flagged
`not_answerable_from_passage` — specificity 0%, discrimination 0%.

**Why no test caught it:** every test drove a scripted provider.
`validator/scripted.py` always returned the model's reply, so the contract was
never ambiguous — nothing compared the two implementations against the
consumer they share. The new tests are written at that seam.

**Also fixed:** `content_of` concatenated `content` and `reasoning_content`
(184 vs 277 parses over 340 replies); truncated replies were scavenged for
JSON fragments; `max_tokens` 1024 → 4096, measured from 63/340 truncations.

## ADR-015 — Phase 0 final: INCOMPLETE, and Phase 1: NO MODEL QUALIFIED (D018, D019)

**Date/phase:** 2026-09-03 (`5ca9267`, `75fe53e`) · **Status:** IMPLEMENTED · **Provenance:** CONTEMPORANEOUS

**Result:** All three arms ran under freeze `acd21b3687b9`. `ABCD − ABD` **not
computed** — the harness refuses to subtract a run that did not reach every
item. It did not run out of anything: 588/2400 candidate attempts, 189/600
judge, 500/1200 minutes.

**Then Phase 1:** specificity 38.9% against a required 90%, dominated by
`below_declared_difficulty` (17 of ~25 `pg_entry` clean items). Adjudication
established the root cause as **PROVENANCE**: all 100 corpus items are
`provenance: model_authored`, `gold_standard: false`, `reviewed_by: ""`.

**The decisive point:** this is not a model-quality verdict and not a fixable
defect. Measuring a candidate's difficulty judgement against unreviewed
model-authored labels compares two models and calls the disagreement a defect.
Whichever way it came out, it would not have been evidence.

**Rejected alternatives, explicitly:** widening `RECALL_IS_ACCEPTABLE_AT`
(a threshold moved after seeing the result it blocks); relabelling the corpus
(a model editing gold it is graded against); excluding the check to reach a
pass (removing Layer D's contribution to specificity entirely).

## ADR-016 — `answerable_from_wording_alone`: a real defect, narrowly fixed

**Date/phase:** 2026-09-03 (`1c17f13` audit, `bf03cb0` fix) · **Status:** IMPLEMENTED · **Provenance:** CONTEMPORANEOUS

**Problem:** the check asserted a *relation* — a cue "selects the keyed option"
— while verifying only *presence* anywhere in `stem + all options`. All eight
flags on clean items failed the claim: 2 cues lay only in a distractor
(cannot select the key), 2 *were* the key (circular), 4 were stem text.

**Chosen approach:** distractor-only and key-only abstain with named reasons;
stem-grounded cues still report, with a detail string claiming only what was
checked.

**Why not abstain on stem cues too:** the first patch did, and broke three
tests including "catches every planted defect". The corpus's own giveaways
depend on stem-grounding — `vd-def-009`'s `defect_note` reads *"The stem now
contains the word 'caseating', which appears in no other option"*. Abstaining
there would trade a false-positive problem for a false-negative one.

**Result:** false flags on clean items 8 → 4; planted-giveaway sensitivity
unchanged. **This did not move qualification** and was not intended to.

## ADR-017 — One origin serves both Android screens (`AnalyticsMount`)

**Date/phase:** 2026-09-03 (`c14afd0`) · **Status:** IMPLEMENTED · **Provenance:** CONTEMPORANEOUS

**Problem:** the app has one backend setting; its two screens read two globals.
Measured both directions: pointed at `serve-analytics`, every learner route
404'd; pointed at `serve-student`, the console's `/api/*` 404'd.

**Chosen approach:** `AnalyticsMount`, shaped like the existing
`billing/mount.py` (`owns()` + `handle()`), mounted behind `--with-console`.

**Why not fallthrough-on-404:** it would reroute a missing notebook into the
benchmark archive and make it look like a missing run. Ownership is *declared*;
a test parses `student/api.py::_ai` and fails if the two lists drift.

**Why opt-in:** `/api/runs` serves full run reports and `/ai/discovery` the
model registry. Mounting operator routes on the origin a learner's phone
points at is a deliberate choice, not a default.

**Read-only:** any non-GET returns 405, so promotion is unreachable from a
phone origin.

## ADR-018 — Fixture fallback on a configured backend was rejected

**Date/phase:** pre-2026-09-03 · **Status:** IMPLEMENTED · **Provenance:** CONTEMPORANEOUS (module comment)

**Decision:** fixtures are used **only when no backend was ever configured**.
A configured backend that fails yields emptiness plus `isOutage`/`loadError`.

**Why, in the module's own words:** the fixtures name real vendors and real
models and assert "93.4% PASS". `quintek-student-api.js` goes further and
ships no fixtures at all.

## ADR-019 — Production refuses generation when no model is qualified

**Date/phase:** established pre-2026-09-03, verified 2026-09-03 · **Status:** IMPLEMENTED · **Provenance:** CONTEMPORANEOUS

**Decision:** `AIEngine.resolve` ladder is `promoted → routed →
development_override → NoEligibleModel`. The chosen source is recorded with
every execution.

**Verified live** with the override removed: ingestion returns
`NoEligibleModel`, generation returns 422, nothing invented. Zero candidates
are in PRODUCTION.

**The development override is not a qualification:** it records as
`development_override`, never `promoted` or `routed`, and is off unless
explicitly configured. Tested in `tests/test_production_safety.py`.

---

# PART 2 — OPEN AND UNRESOLVED

## ADR-020 — PostgreSQL for production persistence, SQLite retained

**Date/phase:** 2026-09-03 opened (`3a59efb`) · 2026-09-04 implemented
(`9c54fa0`, `edee5b4`) · **Status:** IMPLEMENTED

**Problem:** Render's filesystem is ephemeral. Accounts, notebooks, questions,
attempts, progress, revision schedules and billing reset on every redeploy.

**Decision:** `QUINTEK_DATABASE_URL` unset selects SQLite, set selects
PostgreSQL from a bounded pool. One variable, which is what makes the rollback
real — a bad deployment is reverted by clearing it, not by reverting code —
and what keeps the suite runnable with nothing installed.

### The estimate was wrong, and so was the assumption under it

This ADR previously recorded *"~170 SQL call sites, 21 tables, 2 immutability
triggers"*. Measured: **~215 non-test call sites, 33 tables** (22 learner + 9
billing + 2 inference), **5 triggers**, 724 `?` placeholders.

More seriously, it leaned on `student/db.py`'s docstring: *"nothing below uses
a SQLite-only feature except the immutability triggers."* **That was false.**
An audit against a real PostgreSQL 16 found four incompatibilities, **three of
them silent** — the DDL loaded with no error and the failure waited for live
data:

1. **Nullable column inside a composite `PRIMARY KEY`** (`source_concepts`,
   `gap_links`). SQLite permits NULL there and then treats two such rows as
   distinct, so `INSERT OR IGNORE` never deduplicated — a live SQLite bug, not
   only a portability one. Postgres silently promotes the column to `NOT NULL`
   and rejects the insert. Replaced with partial unique indexes.
2. **`BEGIN IMMEDIATE` has no equivalent**, and translating it to a plain
   `BEGIN` reopens the allowance-overspend bug it exists to prevent: measured,
   two 200-unit requests authorised 400 against a 300 cap. Replaced with
   `pg_advisory_xact_lock` keyed per user — stricter where it matters, looser
   where it does not, since SQLite's database-wide write lock made two
   learners queue behind each other for no reason.
3. **Bare column in `GROUP BY`** (`revision.py:86`) — rejected outright.
4. **32-bit overflow on micro-unit money.** A USD 0.30/M model already in
   `configs/model_prices.json` is 2,550,000,000 micro-paise. Now `BIGINT`.

Two further defects surfaced only under test, not by inspection: a caught
duplicate webhook left a poisoned transaction on the pooled connection (fixed
by matching `billing/db.py`'s existing autocommit semantics), and re-running
`schema.sql` per connection deadlocked threads on `pg_proc` (fixed by
initialising once per process under an advisory lock).

**The lesson worth keeping:** *"it looks like standard SQL"* is not evidence of
portability, and a 1353-test suite proved nothing about it because every test
drove the forgiving engine. Each finding now has a regression test verified to
fail on the unported code.

**Consequences:** `psycopg[binary,pool]` is the first runtime database
dependency; it is imported only when `QUINTEK_DATABASE_URL` is set. Connections
are returned at the end of each request — `ThreadingHTTPServer` starts a thread
per request and the caches are thread-local, so without that the ninth
concurrent request against a pool of eight blocks while the service still looks
healthy.

**Tests:** 1371 passed / 25 skipped on SQLite alone; 1418 passed / 4 skipped
with `QUINTEK_TEST_POSTGRES_URL` set against PostgreSQL 16.13. Postgres tests
SKIP without that variable and are never reported as passing.

**Not verified:** no deployment has been made. Supabase and Render remain
unconfigured, pending credentials. See ADR-025.

## ADR-021 — Android builds; it never had

**Date/phase:** 2026-09-04 (`edee5b4`) · **Status:** RESOLVED, with a caveat

Previously recorded as "BLOCKED — environment: no Android SDK". The SDK was
installable here, and once installed the build revealed the real problem:
**the app had never compiled.**

`Settings.kt` documented route prefixes as `/ai/benchmark/*` and `/api/*`
inside a KDoc block. **Kotlin block comments nest**, unlike Java's, so each
literal slash-star opened a comment that was never closed and swallowed the
rest of the file. Symptom: `Unclosed comment`, plus an unresolved `Settings`
reference from all three activities.

Both variants now build against SDK 34. `app-debug.apk` (15.4 MB) is signed
with the debug key and installable. `app-release-unsigned.apk` (14.3 MB) is
unsigned and stays so — **signing needs a keystore, which is a credential, and
none has been invented.**

**Not verified:** no APK has been installed on a device or emulator, and no
end-to-end run has happened. Building is not running.

## ADR-022 — Cleartext HTTP is scoped to debug builds

**Date/phase:** 2026-09-04 (`edee5b4`) · **Status:** IMPLEMENTED

The blanket `usesCleartextTraffic="true"` applied to every build and every
destination, so a shipped APK would have sent a learner's bearer token and
every answer they gave over plaintext HTTP.

Replaced with a scoped `networkSecurityConfig`: release denies cleartext,
`src/debug` overrides it so LAN development against `http://192.168.x.x` is
unchanged. The capability is scoped rather than removed, because removing it
would have broken the way the app is actually developed.

**Verified in the built binaries, not only in source:** the release APK
compiles `cleartextTrafficPermitted=false`, the debug APK `true`, and neither
declares `usesCleartextTraffic`.

**Related, and NOT a defect:** `QUINTEK_CORS_ORIGIN` is `*` because the WebView
loads from `file:///android_asset/` and its Origin is the literal string
`null`, which cannot be allowlisted. It is safe because the app authenticates
with a bearer header, not cookies, and the CORS specification forbids `*`
alongside credentialed requests. Production requires the value to be SET
explicitly; it does not require it to be narrow.

**Also not a defect:** `AdminActivity` is `exported="true"` because the
launcher shortcut starts it by explicit component from another process. It
carries no privilege — the backend authorises admin routes from the bearer
token and returns 404 rather than 403.

**Related:** `QUINTEK_CORS_ORIGIN` defaults to `*` because the WebView loads
from `file:///android_asset/` and its Origin is the literal string `null`,
which cannot be allowlisted. The app authenticates with a bearer header, not
cookies.

## ADR-023 — No efficiency/cost routing policy exists

**Status:** OPEN — DECISION REQUIRED · **Provenance:** RATIONALE NOT RECOVERED

Model choice is by qualification and lifecycle only. There is no deterministic
cost or latency policy in the repository. Whether one was ever intended is
**not recoverable** from the available evidence.

One consequence is on record: D011's rule forbade latency as an input and
selected the slowest qualified model. Whether that was intentional design or
an unexamined omission — **RATIONALE NOT RECOVERED, requires confirmation.**

No policy has been invented. Documented as a V2 decision.

## ADR-024 — Rule 3 quiz waived for ADR-020 (procedural only)

**Date/phase:** 2026-09-04 · **Status:** RECORDED — governance decision
**Provenance:** CONTEMPORANEOUS — owner instruction, verbatim below

**Decision, as instructed by the project owner:**

> Rule 3 quiz waived for ADR-020 Supabase/PostgreSQL migration by explicit
> owner instruction. The waiver is procedural only and does not constitute
> implementation approval. Existing Phase 1 findings and migration assessment
> remain the technical basis for the decision.

**Why the quiz was waived:** token and time efficiency. The Phase 1 assessment
and the re-quiz were consuming budget out of proportion to what they added.

**What this waiver does NOT do.** It does not waive, weaken or shorten:

* testing — the Phase 1 test plan stands, including a failing-first test for
  every compatibility finding and the concurrent-reservation invariant run
  against PostgreSQL;
* review;
* security checks — including the Supabase schema-exposure and RLS
  requirement;
* any acceptance gate.

**Implementation still requires the owner's explicit authorization.** The
waiver removes the quiz step of `CHANGE_PROTOCOL.md` Rule 3 for this change
and nothing else. `CHANGE_PROTOCOL.md` Rule 4 ("no mutation until authorized")
is unaffected: as of this record, no implementation has begun, and none may
begin without a separate explicit instruction.

**Scope:** this waiver applies to ADR-020 only. It sets no precedent for any
other change, and `CHANGE_PROTOCOL.md` itself is unamended.

**Technical basis unchanged.** The Phase 1 read-only assessment is the
technical record for this migration and is not reinterpreted or altered by
this entry.

## ADR-025 — Nothing is deployed; the remaining blockers are credentials

**Date/phase:** 2026-09-04 · **Status:** OPEN — BLOCKED ON OWNER

The code is ready for a deployment that has not happened. Recorded explicitly
so that "the migration is implemented" is never mistaken for "the service is
running".

**What is genuinely verified:** the PostgreSQL path, against a real
PostgreSQL 16.13 server, including the concurrent-reservation invariant, the
connection pool under more requests than it holds, RLS on every table, and
both APKs' compiled cleartext policy.

**What is NOT verified, and cannot be from here:**

| Item | Blocked on |
|---|---|
| A Supabase project exists | owner |
| The service is reachable at an HTTPS origin | owner |
| `/health` answers from a deployed instance | owner |
| Data survives a real redeploy | a deployment |
| The APK installs and reaches the backend | a device |
| A signed release APK | a keystore, which is a credential |

**CONFIRMED FAILING — the concept graph screen is not wired to the backend.**
Found 2026-09-15 by read-only inspection; not a hypothesis and not blocked on
anything external.

`GET /graph` is real, working and tested — `ConceptStore.graph_for_user`
(`student/concepts.py:217`) returns the user's own concepts and the edges
between them, and computes `cross_subject` server-side. Verified live: HTTP
200, correct shape, `cross_subject: true` on a cardiology↔biochemistry edge.
Covered by `tests/test_student_concepts.py:167,182,183` and
`tests/test_student_e2e.py:214`, including the isolation case.

**Corrected 2026-09-16.** The first version of this entry said the client had
no graph method. That was written against a checkout 23 commits behind the
branch and was wrong. `frontend/quintek-student-api.js:256` has `graph()`, and
`tests/frontend/model_free_surface.test.mjs:91,92,132` cover it.

The gap is one layer further out, and it is the same gap for every item below:
**no screen calls any of it.** The screens are built by
`tools_build_standalone.py` from the `.dc.html` design sources, and
`frontend/PG Revision.dc.html` is dated 2026-08-28 -- untouched by any of the
wiring work. It calls none of `graph()`, `conceptDetail()`,
`notebookQuestions()`, `reportQuestion()`, `myReports()`, `scope()` or
`screenState()`. So the concept graph still renders its hardcoded
`NODES`/`EDGES`, and a learner with their own notebooks sees fourteen
fabricated medical concepts.

The same is true of the content-safety fields: `student/api.py:44-45` returns
`chunk_confidence` and `needs_review` and `student/api.py:43` returns
`source_locator`, and no screen renders any of them.

This is fixture data presented as live data — the failure mode the manual test
plan exists to catch. **Deferred by owner instruction (2026-09-15), to be fixed
in the same batch as concept detail and notebook view.** When it is fixed, the
renderer must take the server's `cross_subject` flag rather than recomputing it
from the node's subject: the client's four-colour palette does not cover every
subject, so its recomputation is wrong for any subject outside that set.

**Credentials required, and only these:** `QUINTEK_DATABASE_URL`, entered in
the Render dashboard. Supabase's anon key, service-role key and JWT secret are
**not** used — Quintek connects as a PostgreSQL role over TLS and never goes
through PostgREST — and must not be set.

**The correction this ADR also carries:** earlier reports in this project
stated the validator holdout was "0 of 5 used". The ledger contains **one**
entry, an `inspection` dated 2026-08-21, and `validator/holdout.py:212` counts
every entry against `MAX_USES`. It is **1 of 5**. The ledger has been unchanged
since `df99141`; the error was in the reporting, not in the ledger.

## ADR-026 — Two open loops that must not be closed with code

**Date/phase:** 2026-09-16 · **Status:** OPEN — PROCESS, NOT CODE
**Provenance:** CONTEMPORANEOUS — owner instruction

Both of these look like software problems and are not. Written here because
the failure mode for both is the same: somebody reads the code, sees the
mechanism present, and concludes the loop is closed.

### 1. The report queue has no operator

`GET /ops/reports`, `GET /ops/reports/gold-candidates` and
`POST /ops/reports/<id>` exist, are tested, and work. **Nobody reads them on a
schedule.** Today that is the project owner, by hand, with curl.

A report path whose queue nobody reads is still an open loop, and it is worse
than no report path at all, because the button implies a loop that closes. A
learner who reports a wrong question and hears nothing has been told, by the
interface, that somebody would look.

This cannot be fixed by writing more code. It needs a named person, a cadence,
and a target — who reads the queue, how often, and how long a report may sit
before it is answered. An alerting rule or an auto-responder would make the
silence *look* addressed, which is the one outcome worse than the current
state. **Do not close this with software.**

Until it has an owner: the report button is live in the app, and the
resolution a learner sees on `GET /reports` will stay `open` indefinitely.

### 2. The 28-item corpus audit needs a clinician

The development corpus is `provenance: model_authored`, `gold_standard:
false`, `reviewed_by: ""` across all 100 items. 28 items are queued for label
audit, and **at least one item labelled clean is known to be wrong** — found
during Track D construction and recorded in the holdout ledger's inspection
entry.

The consequence is precise and worth stating rather than softening: **the
clean arm's specificity is measured against labels at least one of which is
known to be incorrect.** Every specificity figure downstream of that carries
the error, whatever its confidence interval says.

This needs a qualified clinician ruling on those 28 labels. It is not an
adjudication the model, the validator or this session can perform — a
model-authored label re-checked by a model is the same evidence twice, and
`docs/JUDGE_INDEPENDENCE.md` exists because that distinction is the project's
whole basis. **Authoring and adjudication are the owner's, explicitly out of
scope for implementation work.**

Until those labels are ruled on, `NO MODEL QUALIFIED / INSUFFICIENT EVIDENCE`
is not merely the current state — it is the only state the evidence supports.


## ADR-027 — The student app shipped a SyntaxError, and nothing was in a position to notice

**Date/phase:** 2026-09-17 · **Status:** CLOSED — fixed, and the gap that hid
it is closed with a test

`frontend/PG Revision.dc.html` declared `const live` twice in one function
scope: once at the top of `renderVals()` for the object `learnerView()`
returns, and again 113 lines later for `!!s.liveQuestionId`. A `const`
redeclared in one scope is a **parse** error, not a runtime one, so the
component class was never constructed and **every screen in the student app
rendered blank.**

It shipped. Into `frontend/dist/pg-revision.html`, into
`android/app/src/main/assets/`, and into `app-debug.apk`. It was introduced by
the revision-loop commit and survived the four-screens commit on top of it.

### Why nothing caught it

This is the part worth keeping, because the gap was structural rather than bad
luck. At the time there were two kinds of frontend test:

* `tests/frontend/*.test.mjs` imported `quintek-student-api.js`, a real ES
  module. A syntax error there fails on import, immediately and loudly.
* Python tests read the built bundles **as text** and asserted on substrings.
  A file that does not parse satisfies a substring assertion exactly as well
  as one that does.

Between them, **no JavaScript engine was ever asked to look at the component.**
Every claim about the screens — including three manual-test rows marked
"auto-verified" — rested on tests that exercised the client module and the
server. Both of those were fine. The thing between them and the learner was
not.

The caveat written into `docs/ANDROID_MANUAL_TEST.md` was exactly right and
still understated it: "the test drives client and server, not a phone, so a
ticked row can still fail through a WebView difference". It was not a WebView
difference. The file was not JavaScript.

### What now prevents it

`tests/frontend/component_parses.test.mjs` hands every `.dc.html` design
source, every built bundle in `frontend/dist/`, and every bundle in the
android assets to `vm.Script`, which runs V8's parser without executing a
statement. Reintroducing the redeclaration fails all three, which was verified
by doing it.

All three surfaces are checked deliberately. `dist/` and the android assets
are written by the same build but written **separately**, and a rebuild that
updates one and not the other is the "did the emulator pick up my change?"
failure the build stamp already exists for. Checking `dist/` alone would let a
stale, broken asset ship behind a green test.

### The general lesson, stated so it can be applied elsewhere

**A test that reads an artefact as text cannot tell you the artefact works.**
It can only tell you the text is present. Where the artefact has a parser — a
bundle, a schema, a config file, a migration — the cheapest real test is to
run that parser over it. This repository has two other places that read built
or generated artefacts as text; they are not known to be wrong, and they are
also not known to be right.

## ADR-028 — The operator surface is enumerable by any logged-in learner

**Date/phase:** 2026-09-17 · **Status:** CLOSED 2026-09-24 — see *Resolution* at the end of this entry

Held unfixed under the standing stop condition: a new disclosure route is
shown to the owner before it is closed.

### The finding

`_require_admin` raises `404 "no such route"` rather than 403, and its
docstring gives the reason: "a 403 confirms the route exists and that the
caller is merely the wrong person, which is a fact worth not handing out."

The status code hands nothing out. **The body does.** A learner's token gets a
different 404 *message* for an operator route than for a path that does not
exist:

| Request, with an ordinary learner's token | Body | What it tells the caller |
|---|---|---|
| `GET /ops/incidents` | `no such route` | **exists; wrong role** |
| `GET /ops/alerts` | `no such route` | **exists** |
| `GET /ops/spend` | `no such route` | **exists** |
| `GET /ops/reports` | `no such route` | **exists** |
| `GET /ops/reports/gold-candidates` | `no such route` | **exists** |
| `POST /ops/reports/<id>` | `no such route` | **exists** |
| `GET /admin/users/<id>` | `no such route` | **exists** |
| `GET /ops/zzz` | `no such endpoint: GET /ops/zzz` | does not exist |
| `GET /nope` | `no such endpoint: GET /nope` | does not exist |

So the operator API can be mapped by anyone who can register an account and
diff two strings. The control that was deliberately built to prevent exactly
this was defeated one layer below where it was implemented.

**An anonymous caller learns nothing.** `_user()` rejects a missing token
before routing, so every path — real, operator-only or invented — answers
`401` with the same body. That half holds.

### Severity, stated plainly

Low. It discloses the *shape* of the admin API to an authenticated learner and
grants no access: `_require_admin` still refuses, and every ownership filter
checked in the same sweep (`gap_evidence`, `resolve_gap`, `question_bank`,
`notebooks`) correctly scopes by `user_id`. What it costs is the assumption
that the operator surface is unadvertised, which some later decision may lean
on without knowing it is false.

### How it was found, which is the part worth keeping

Not by reading the code. By writing `tests/test_ops_surface_live.py` to assert
the *property* — an operator route is indistinguishable from a nonexistent one
— rather than the *status code*. The first draft asserted `== 404`, failed on
the anonymous case for an unrelated reason (401, from a layer that fires
earlier), and chasing that wrong failure is what surfaced the real one.

An assertion on the literal code would have passed. Both sides are 404.

### The fix, when authorised

Two lines: make `_require_admin` and the two `admin/users` fall-throughs raise
the same `f"no such endpoint: {method} /{path}"` body the generic fallback
uses. No behaviour changes for an operator; the only observable difference is
that the surface stops being enumerable.

`tests/test_ops_surface_live.py` carries the check as `xfail(strict=True)`, so
it **fails the moment the fix lands** and cannot be quietly left behind.

### Resolution (2026-09-24)

One not-found answer, raised from one place: `StudentAPI._no_such_endpoint`,
`404 {"error": "no such endpoint: <METHOD> /<path>"}`. `_require_admin` now
takes the request's method and path and raises exactly that, as do the two
`/admin/users` fall-throughs and the generic fallback.

The held test compared against a control path byte for byte, which a body that
echoes its own path can never satisfy. It now compares each response with its
own path taken out, which still fails on any other difference — including the
original one. Coverage was three GET routes; it is now every route behind
`_require_admin`, with a check that counts the guards in the source so a new
operator route cannot go unlisted. Five mutations, all killed. Billing's
`/admin/*` already answered with the generic body; the console routes mounted
by `--with-console` are the deliberately public, read-only benchmark surface
and hold no learner data.

## ADR-029 — A learner who has used the app cannot delete their account

**Date/phase:** 2026-09-17 · **Status:** PARTLY FIXED · one half OPEN for the owner

Found by writing a test that inspects the DATABASE after `DELETE /account`
rather than the response code. The existing coverage asserted
`status == 200` over HTTP and checked rows only from a direct call to
`accounts.erase()` — and the fixture behind that direct call created a user
and a notebook and **no attempt**, so no test had ever erased an account that
had actually been used.

Three separate defects came out of one test. Two are fixed; the third is not
mine to decide.

### 1. FIXED — erasure was impossible on PostgreSQL, for every account

`tables_holding_user_data()` enumerated the schema with `SELECT name FROM
sqlite_master` and `PRAGMA table_info`. Both are SQLite-only. `erase()` calls
it first, so on PostgreSQL the request died with `UndefinedTable: relation
"sqlite_master" does not exist` before touching a row.

**Production runs on PostgreSQL.** For as long as the Render service has been
live, a learner asking to be forgotten would have received a 500.

This is the **fifth** dialect incompatibility, and it is the first one that was
not in `schema.sql`. ADR-020 translated the DDL and audited the DDL; this was
runtime introspection in application code, which that audit was not looking at.
The compatible helpers — `persistence.schema.table_names` and `.columns_of` —
already existed and were already used by the migration path. This function
simply never called them. It does now.

### 2. FIXED — table names were quoted as string literals

`DELETE FROM 'attempts'` — single quotes. SQLite tolerates that as an
identifier; PostgreSQL reads it as a string and answers `syntax error at or
near "'attempts'"`. Four sites, now double-quoted, which is the SQL standard
and correct on both.

Worth noting as its own class: the first defect hid the second. Fixing
`sqlite_master` is what let the query run far enough to hit the quoting.

### 3. OPEN — the immutability trigger refuses the erasure

With both of the above fixed, an account with **no attempts** now erases
correctly on PostgreSQL, verified row by row. An account **with** an attempt
still fails, on both backends, with:

```
attempts are immutable: they are the evidence base for every colour and priority
```

That is `attempts_are_immutable_delete` doing exactly its job
(`student/schema.sql:269-282`). Both backends refuse identically — SQLite via
`RAISE(ABORT)`, PostgreSQL via the translated `RAISE EXCEPTION` — which is a
small confirmation that the ADR-020 trigger translation is faithful. **The
conflict is in the design, not in the dialect.**

Two deliberate invariants collide:

* An attempt is evidence, and evidence that can be edited or removed is not
  evidence. Every colour, priority and gap in the system derives from
  attempts. Both UPDATE and DELETE are blocked, so the rows cannot even be
  anonymised in place the way `incidents` and `spend_log` are.
* A learner may ask to be erased, and `docs/BETA_TESTER_BRIEF.md` tells a
  tester they can.

**This is not a bug to pick a side of.** It is a policy question about medical
evidence retention against a data-protection promise, and it belongs to the
owner. The options, stated without a recommendation:

1. **Anonymise attempts.** Add `attempts` to `RETAINED_ANONYMISED` and relax
   the trigger to permit clearing `user_id` and nothing else. Keeps every
   derived figure intact; means "erased" does not mean "no row survives".
2. **Delete attempts.** Drop the DELETE half of the trigger, keep the UPDATE
   half. Erasure becomes literal; an erased learner's attempts stop backing
   any aggregate computed from them.
3. **Tell the truth instead.** Leave both as they are and change the brief to
   say attempts are retained, with the reason. Honest, and the weakest of the
   three for anyone who asked to be forgotten.

Until it is decided: **`DELETE /account` returns 500 to any learner who has
answered a question.** `tests/test_erasure_over_http.py` carries three
`xfail(strict=True)` tests, so whichever option is taken, they fail the moment
it lands and cannot be left behind.

## ADR-030 — Revision sessions serve other learners' questions

**Date/phase:** 2026-09-23 · **Status:** CLOSED 2026-09-23 on the owner's instruction — see *Resolution* at the end of this entry

Held under the standing stop condition: a disclosure route is shown to the
owner before it is closed. Forward work on the colour/reminder/onboarding pass
stopped at the point this was found.

### The defect

`RevisionEngine._questions_for_concepts` has no owner scope:

```sql
FROM questions q JOIN question_concepts qc ON qc.question_id = q.id
WHERE qc.concept_id IN (...)
  AND q.validation_status IN ('approved', 'pending')
```

Concepts are **global** rows (`normalized_name` is UNIQUE), so two learners
whose material yields the same concept share its id. `adaptive` — the default
strategy — and `red` / `orange` / `green` all select through this function, so
a learner's session is built partly from other learners' questions, and
`GET /revision/next` returns the stem and options.

Measured by execution, two learners sharing one concept:

| Path | Result |
|---|---|
| `adaptive` session | **serves Bob's questions to Alice** |
| `orange` session (and `red`/`green` when the learner has that colour) | **serves Bob's questions** |
| `due`, `unseen` | owner-scoped — correct |
| `GET /gaps/<id>/questions`, concept question list, `/graph` | owner-scoped — correct |
| `GET /concepts/<id>` → `related` | **names a concept that exists only in Bob's material** |

Two further consequences of the same query:

* **An unvalidated question can be served.** `'pending'` is in the filter, so
  a question the validator has not passed reaches a session — its author's and
  other learners'. That is the hard gate, reached from the side.
* **The learner gets stuck.** `record_attempt` *is* owner-scoped, so answering
  a foreign question is refused with `no such question`, and the session
  cannot advance past it.

### Exposure

**None in production.** Checked against the Render database: 0 questions,
0 concepts, 0 relationships, 0 sessions — generation has never been allowed to
run, so nothing has existed to leak. It becomes live the day generation does.

### Why nothing caught it

`tests/test_cross_user_access.py` runs sessions across two users, but its
fixture never gives both users the same concept. The concept-driven selection
steps therefore never had a foreign question to find, and the coverage was
vacuous. It dates from `a770d4c` (2026-08-19). Found by a render test that
gave three learners one shared concept on purpose.

### Proposed fix — for the owner's decision

1. Scope `_questions_for_concepts` to the learner's own notebooks (join
   `notebooks` on `owner_id = ?`), the way every other selection step is.
2. Scope `related` to concepts present in the learner's own notebooks.
3. **Decide whether `pending` should be servable at all.** Removing it is the
   conservative reading of the hard gate. It may have been deliberate — for
   example, to let an author revise their own material before validation — so
   it is a question, not an assumption.
4. Add a shared-concept case to the cross-user meta-test fixture, so the
   coverage stops being vacuous.

`tests/test_session_isolation.py` holds five `xfail(strict=True)` tests plus a
positive control proving the fixture really shares a concept.

### Resolution (2026-09-23)

**The owner's decision on `pending`:** no. Only validated questions may ever
be served to a learner, with no exception — including an author reviewing
their own material. If that workflow is wanted it is a separate, explicit
feature, not a gap in a filter.

Implemented as one rule stated once, `SERVABLE_STATUS = "approved"` in
`student/api.py`, applied at every point a question's content can leave the
server:

| Path | Before | After |
|---|---|---|
| Concept-driven session steps | any learner's, approved or pending | own notebooks, approved only |
| `unseen` step and adaptive step 8 | own, **any status at all** | own, approved only |
| `due` and previously-incorrect steps | re-served a question after it was rejected | approved at build time |
| `/revision/next` | served whatever the session held | re-checks at serve time |
| `GET /questions/<id>` | stem, options, answer for any status | 403 unless approved |
| `POST /attempts` reveal | answer, rationale, passage for any status | 403 unless approved, before any row is written |
| Bank, notebook, concept and gap lists | stems for any status | row listed, stem withheld |
| `/concepts/<id>` `related` | global | concepts in the learner's own notebooks |

The last five rows were found while fixing the first: all owner-scoped, so not
a second disclosure route, but each one a side door past the hard gate.

Eligibility is enforced twice for sessions — in the query and at the one
choke point every selection step passes through — so each layer is tested on
its own. Seven mutations, one per layer; one survived at first (re-allowing
`pending` in the query) because the only pending question in the fixture
belonged to the other learner. The fixture now includes the learner's own
pending question, and the mutation fails.

`tests/test_cross_user_access.py` now starts a session as the learner who
shares concepts with the other, with a positive control that fails if the two
ever stop sharing one — the fixture blind spot that let this through.

`tests/test_content_safety.py` previously asserted that a **flagged** question
was served with "flagged" on its reveal. That was the design this decision
replaced; the test now asserts a flagged, pending or rejected question has no
reveal at all.

## ADR-031 — Reminders are rows the learner writes, not one setting per learner

**Date/phase:** 2026-09-23 · **Status:** BUILT up to the sender; delivery and scheduling NOT BUILT (see *Delivery* and *Scheduling* below)

### What changed

The old model was one row per learner in `notification_prefs`: a daily trigger
time, two channel toggles and an optional note. It answered "when should the
app nag me every day", which is not what was asked for. The spec is: a learner
writes a label — "revise patho", "revise micro" — and picks a date and a time,
as many times as they like, and at that moment gets their own words back.

`student/reminders.py` implements exactly that, and the old module
(`student/notifications.py`) is deleted rather than extended:

| Rule | How it is held |
|---|---|
| Any number, independent | One row per reminder in `reminders`; create/edit/cancel touch one id. |
| The label is opaque | Stored and delivered verbatim — not trimmed, not rewritten, not linked to a concept. Checks are only what a database needs: non-empty, ≤ 200 characters, no NUL (PostgreSQL TEXT rejects one). |
| It fires because its own time came | Selection is `status = 'pending' AND fire_at <= now`. No reference to `due_at`, colours or concepts, and firing starts no session. |
| Time is the learner's wall clock | A local date, time and IANA timezone are stored with the UTC instant computed once at save. |
| A wall-clock time that is not exactly one instant is refused | A time in a spring-forward gap does not exist; one in a fall-back hour happens twice. Both are refused with a message naming which, rather than resolved by the system choosing for the learner. |
| Ownership | Every by-id query names the owner in the same `WHERE` clause. Another learner's reminder is "no such reminder" in every state and for every body — including a finished one (which would otherwise answer 409) and an invalid edit (which would otherwise answer 400). Both oracles are tested, because the `UPDATE`'s own owner clause hides the missing `SELECT` clause from a write check. |
| Edit and cancel only while pending | A fired, failed or cancelled reminder is a record of what happened. Cancel keeps the row so the list shows it rather than silently shrinking. |

Routes: `GET/POST /reminders`, `GET/PUT/DELETE /reminders/<id>`. The old
`/settings/notifications*` routes are gone. `/reminders/<id>` is enumerated by
the cross-user meta-test like every other id-bearing route.

### Delivery — built up to the sender

`ReminderService.fire` claims a due reminder with a conditional `UPDATE`
(`pending` → `fired`, only if still `pending`), then hands the sender
`{user_id, reminder_id, label, fire_at}` and nothing else. Two overlapping runs
cannot both send one. The cost is at-most-once: a crash between claim and send
loses that reminder rather than doubling it — visible on the learner's list,
which is where a missed reminder should be noticed.

**No sender is configured on any deployment.** A due reminder is therefore
recorded `failed` with the reason `no notification sender is configured`, and
`GET /reminders` returns `delivery_configured: false` so the screen says so
above the form, before anyone relies on a reminder.

**Recommended channel** (not built — each needs a credential):

1. **Push to the Android app via Firebase Cloud Messaging.** The only client
   that exists is the Android WebView build, so this reaches the learner where
   they are. Needs a Firebase project and its server credential in the
   platform's secret store, a device-token table, and the app registering for
   notifications (it requests no permission today).
2. **On-device scheduling instead of server delivery** — worth weighing before
   committing to (1). The app would read the learner's pending reminders and
   schedule them with Android's own alarm and notification APIs through a
   WebView bridge. No push credential and no server scheduler; the trade-off is
   that it fires only on a device that has synced, and "fired" becomes a device
   report rather than a server fact.
3. **Email — not recommended yet.** Registration does not verify addresses
   (`NOT_BUILT.md`), so a reminder could deliver a learner's own words to
   somebody else's inbox. Email verification comes first.

### Scheduling — not built

Something must call `python -m benchmark.cli notify` (which runs `run_due`
once and exits). Nothing does. What exists on Render today: one **free-plan**
web service and a **free-plan** Postgres; no cron job. A free web service
spins down when idle, so an in-process timer would stop exactly when nobody is
using the app — which is when reminders matter. Options:

| Option | What it needs |
|---|---|
| A Render Cron Job running `notify` | A paid service type on Render, `QUINTEK_DATABASE_URL` set on it from the platform's secret store, and the same region as the database. The cron interval is the worst-case lateness of a reminder. |
| A paid always-on web instance with an in-process scheduler | A plan change and code that does not exist (a scheduler thread). The CLI entry point was designed to avoid exactly this. |
| An external scheduler calling an authenticated endpoint | An operator-only "run due reminders" endpoint (not built) and a shared secret. |
| On-device scheduling (channel option 2) | Removes the server scheduler entirely. |

**Do not schedule `notify` before a sender exists.** With no sender, every due
reminder is marked `failed` — honest, but it spends the learner's reminders.

### The retired tables — what they held and what was done

Read from the production database (read-only query, 2026-09-23):
`notification_prefs` — **6 rows, every one at its defaults** (created
automatically at registration), none with a note, none scheduled;
`notification_log` — **0 rows**.

Choices were drop, migrate, or leave:

* **Migrate — rejected.** There is no learner-authored data to carry over, and
  turning a default "20:00 daily" into a reminder would create a reminder
  nobody wrote.
* **Drop — not done here.** Dropping a table from a live database is a
  destructive migration and should be its own deliberate step.
* **Leave, retired — done.** Nothing reads or writes either table; registration
  no longer creates a row; `schema.sql` keeps the definitions under a RETIRED
  comment so existing and new databases agree; erasure still clears them
  because they carry `user_id`.

### Decision (2026-09-24): delivered by the phone

**The owner chose on-device scheduling, not FCM.** It needs no Firebase
credential, no cron job, no paid Render tier and no always-on instance, which
removes every blocker listed above at once. Its weakness — a reminder fires
only on a phone that has synced it — is acceptable for a single-user testing
phase and for a revision reminder, which is not time-critical the way an alert
is. **FCM remains the recorded path** for multi-device or guaranteed delivery.
Only one is built.

How it works:

* **What to schedule** is `reminderSyncPlan` (client module): pending
  reminders whose server UTC instant is in the future, label byte-for-byte. A
  reminder whose time passed before the phone saw it is not fired late; the
  row says it passed. Tested directly, in a non-UTC timezone.
* **Sync** happens every time the Reminders list loads. The page hands the
  plan to `window.QuintekReminders.sync`; the phone arms exactly that set and
  disarms anything else it held — so a cancel or an edit takes effect on the
  next visit.
* **The phone** (`android/.../Reminders.kt`): `setAndAllowWhileIdle` alarms
  (inexact; no exact-alarm permission, may be a few minutes late in Doze),
  a notification whose text is the label verbatim, alarms re-armed after a
  reboot or an app update, and a per-reminder record of *shown* or *blocked*
  that the list displays. The bridge exists only on the learner screen and
  refuses unless that screen's own bundle is loaded.
* **Permission**: POST_NOTIFICATIONS is asked for from the Reminders screen
  (a button beside "Notifications are off…"), never at launch.
* **The server's `notify` path is unchanged** and still scheduled by nothing.
  The warning stands: scheduling it without a sender marks every due reminder
  failed.

What is verified where: the plan and the screen, by
`tests/frontend/device_reminders_live.test.mjs` against a live server (15
mutations, all killed); the Kotlin, by compiling into the APK and the packaged
manifest; the behaviour on a phone, by items W–AA of
`docs/ANDROID_MANUAL_TEST.md` and nothing else — there is no device here.

## ADR-032 — No answer given through the live app's colour buttons was ever recorded

**Date/phase:** 2026-09-23 · **Status:** FIXED the same day. Found while writing the dashboard how-to, not a disclosure route.

### What was wrong

Two defects, one behind the other:

1. **The buttons sent the wrong value.** `JUDGEMENTS` used the display word as
   the value, so pressing Red sent `user_colour: "Red"`. The server accepts
   exactly `RED | ORANGE | GREEN` and refuses anything else rather than guess,
   correctly. So **every answer given through the live app was refused with a
   400**, and the screen showed the error. The same mismatch kept the "Which
   part failed?" panel shut in every mode, because `needGap` compares `'RED'`.
2. **Gaps had nowhere to go.** The attempt is written when the colour is
   chosen, because that response carries the reveal and an attempt is
   immutable. "Which part failed?" can only be answered after the reveal, so
   it came too late to go into the attempt, and a live question offered no way
   to answer it anyway (its chip list was empty). Nothing in the live app
   could create a gap, and the weak list, which is built only from gaps, was
   empty for every real learner.

### Why nothing caught it

Every colour test (A1–A3 included) sent colours to the API directly or set
`judgement` in state. None pressed a button. The server's validation was right
and was tested; the value the UI handed it was not.

### Exposure

None in production: the live database has never held a question, so no
learner reached the buttons.

### Fix

* `JUDGEMENTS` rows are now `[value, colour, note, label]`; the value is the
  server's canonical colour and the label is only displayed. The server stays
  strict.
* `POST /attempts/<id>/gaps` (`KnowledgeStore.tag_gaps`) attaches gaps the
  learner names after the reveal. Owner-scoped in the same `WHERE` clause;
  Red or Orange answers only; the whole batch is validated before anything is
  written; the attempt row is not touched — `gap_links` is the record.
  Declared in the cross-user meta-test, with the database checked after B's
  refused writes.
* In a live session the learner types the gap in their own words (the app
  does not propose gaps from the question's concepts — that would be deciding
  for them) and it is saved at once, not on "Next". The chip appears only
  after the server stored it.

`tests/frontend/answer_buttons_live.test.mjs` presses the real buttons
against a running server. Twelve mutations, including reinstating the
original values; all fail a test.

### Not fixed, recorded

`frontend/PG Revision standalone.dc.html` and `frontend/Quintek PG Revision.html`
are offline design copies last touched 2026-08-19. They carry the old
`JUDGEMENTS` but have no backend client, so the 400 cannot occur in them; they
are not built from source and are left as they are.
