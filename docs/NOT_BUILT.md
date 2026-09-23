# What is not built

Kept so a tester is not surprised, and so a missing thing is not mistaken for
a bug. Everything here is absent deliberately or blocked on something named.

A feature being listed is not a promise it will be built.

---

## Blocked on the hard gate

**No learner other than the author sees model-generated medical content until
the validator clears its holdout.** Nothing below routes around that, and the
one environment variable that could (`QUINTEK_DEV_CANDIDATE`) is refused in
production by `student/production.check()`.

| Not built | Why |
|---|---|
| Question generation for testers | Nothing is qualified. Authoritative state: NO MODEL QUALIFIED / INSUFFICIENT EVIDENCE. The app refuses rather than using an unqualified model. |
| A published accuracy figure | The validator has not completed a scored run. Any number now would be from an incomplete arm. |

## Blocked on people, not code

| Not built | What it needs |
|---|---|
| Adjudicated corpus labels | Two named clinicians. All 100 development items are `label_status: unreviewed`, `provenance: model_authored`, zero reviewers, and at least one CLEAN label is known wrong (`vd-clean-016`). `docs/CLEAN_LABEL_AUDIT.md` narrows it to 28 items worth an hour. |
| Gold difficulty labels | The same. `conformance/below_declared_difficulty` does not gate because the labels it compares against are unreviewed model output; the cost is recorded in `validator/scripted.UNCOVERED_BY_DESIGN`. |
| A second qualified reviewer for M4 | A person. |

## Ingestion

| Not built | Notes |
|---|---|
| OCR for scanned pages or images | `GET /capabilities` reports this honestly; an image source is refused with the reason rather than accepted and silently producing nothing. |
| Link ingestion | Needs outbound fetching, an HTML-to-text pass and a robots decision. Refused with that explanation. Paste the text. |
| Video | Same, plus transcription. |

## Operations

| Not built | Notes |
|---|---|
| Alert delivery | `student/operations.alerts()` returns what has crossed the threshold; nothing emails or webhooks it. Where an alert goes is a deployment decision; no delivery channel exists yet (see *Reminder DELIVERY* below). |
| The spend ceiling is not charged against | **2026-09-17.** `operations.spend_guard()` and `charge()` work, refuse correctly, reload from history rather than memory, are per account, and are tested over HTTP against a running server — and **nothing calls them.** Wiring needs a number: how many generation calls one account may make per period. That is a business decision, and inventing one would put a made-up figure in the path of every learner's spend. The module docstring used to claim this was wired to ingestion and generation; it never was, and the claim is corrected. |
| An operator account | There is no route and no CLI to create one; `role` is set by direct SQL. **No admin exists on the live deployment**, so the report queue, incidents, alerts and spend are not merely unowned (ADR-026) but unreachable, and no tester can be suspended. `docs/INVENTORY.md` §6 has the one-line SQL. |
| Scheduled backups | `tools_backup.py take` is verified and tested by restoring, but nothing runs it on a timer. That is a cron line or a platform job, not application code. |
| Rate limiting that survives a restart or spans workers | `student/throttle.py` is per process and in memory. Two workers mean two limiters and twice the ceiling. A real deployment wants this at the reverse proxy; what exists is a floor that is present by default rather than nothing until someone configures one. |
| Log shipping | One structured line per request on stderr. Collecting it is the platform's job. |

## Accounts and billing

| Not built | Notes |
|---|---|
| Password reset | No email delivery is configured. An account locked out of its password currently needs the author. |
| Email verification | Registration accepts any address. |
| Self-service plan changes | The entitlement engine and ledger exist; the checkout flow does not. |
| The billing `batch_id` write path | Known and deliberately unfixed; recorded in the status docs. |

## Client

| Not built | Notes |
|---|---|
| A packaged mobile app | The Android WebView path is manually tested (`docs/ANDROID_MANUAL_TEST.md`); there is no store build, and **nothing in this repository has ever run on a phone**. |
| A signed release APK | Needs a keystore, which is a credential. `app-debug.apk` builds. |
| Offline use | Every screen needs the backend. An outage is reported as an outage and never as empty data. |
| Reminder DELIVERY and scheduling | **Rewritten 2026-09-23 (ADR-031).** Reminders are real: any number per learner, each the learner's own words at a date and time they chose, created, listed, edited and cancelled on the settings screen and stored server-side. Nothing delivers them and nothing runs the scheduler. `ReminderService` takes an injected `sender`, none is configured, so a due reminder is recorded `failed` with `"no notification sender is configured"` and the screen says delivery is off before a learner relies on one. No OS notification is posted and no permission is requested. ADR-031 has the recommended channel and the scheduling options; each needs a credential or a plan change. |
| A control to resolve a gap | **2026-09-23.** `POST /gaps/<id>/resolve` exists, is owner-scoped and tested, and no screen calls it. A gap the learner names stays on Forgotten / weak until one does. The how-to on Today says so rather than describing a button that is not there. |
| Spaced-repetition intervals on screen | `revision_state` computes SM-2 intervals server-side and the revision queue uses them. No screen shows a learner when a question is next due or why. |
| Mastery and review percentages per notebook | The notebook list payload carries source, concept, question and due counts, and no mastery figure. The tile renders an em dash rather than a number nothing computed. |

## Known defects, recorded rather than fixed

| Defect | Why it is still here |
|---|---|
| TLS errors are typed as `TimeoutError` | In `benchmark/providers/nvidia.py`, a `URLError` becomes `TimeoutError` whatever caused it. Misleading in a traceback; no behavioural consequence, since both are transport failures the retry loop handles identically. |
| Auth rejections are retried three times | A 401 will never succeed on retry. Costs two wasted attempts on a misconfigured credential, and nothing else. |
| `runs.record` names artifacts `{timestamp}_{kind}_{config}.json` | Two runs in the same second with the same config label overwrite each other. Not reachable in the normal flow, where arms carry different labels. |
| **Account erasure fails for any learner who has answered a question** | ADR-029. `DELETE /account` returns 500: `attempts_are_immutable_delete` refuses the delete, correctly, and a learner may also ask to be erased. Both are deliberate and they collide. A policy decision, not a bug with an obvious fix — three options are written out in the ADR. Three `xfail(strict=True)` tests hold the place. **The beta brief now warns testers.** |
| **The operator surface is enumerable by any logged-in learner** | ADR-028. `_require_admin` answers 404 so the route is not advertised, and then the BODY says `no such route` where a genuinely absent path says `no such endpoint: GET /x`. Diffing two strings maps the whole admin API. Grants no access; defeats a control that was built on purpose. Held unfixed under the standing rule that a disclosure route is reported before it is closed. |
| An unknown strategy and an empty queue are both 422 | `student/api.py` maps every `ValueError` out of `start_session` to 422, and the engine raises one both for a strategy name it does not know and for a known strategy that matched no questions. A learner with nothing orange asking for the orange queue gets the same status as a client bug. The message distinguishes them; the code does not, so the screen shows an error where it should show an empty state. |

## Deliberately absent, not missing

| Thing | Why |
|---|---|
| Independent redraw per arm in a validator run | Recorded in `benchmark/journal.FORFEITED_BY_DESIGN`. Arms share observations so the ablation's difference does not carry sampling noise; measuring run-to-run variance now needs its own mechanism. |
| A check for `trivial` defects | `validator/scripted.UNCOVERED_BY_DESIGN`. The only check that saw them compared against unreviewed difficulty labels, so it no longer gates. Four planted defects go uncaught, and that is recorded rather than hidden. |
| Sanitising prompt-injection phrases out of uploaded text | A blocklist over natural language loses to rephrasing. The fence in `student/untrusted.py` makes the boundary unforgeable instead; `structure_markers()` reports suspicious text without filtering it. |
