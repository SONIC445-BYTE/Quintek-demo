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

**Credentials required, and only these:** `QUINTEK_DATABASE_URL`, entered in
the Render dashboard. Supabase's anon key, service-role key and JWT secret are
**not** used — Quintek connects as a PostgreSQL role over TLS and never goes
through PostgREST — and must not be set.

**The correction this ADR also carries:** earlier reports in this project
stated the validator holdout was "0 of 5 used". The ledger contains **one**
entry, an `inspection` dated 2026-08-21, and `validator/holdout.py:212` counts
every entry against `MAX_USES`. It is **1 of 5**. The ledger has been unchanged
since `df99141`; the error was in the reporting, not in the ledger.
