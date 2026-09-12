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
| Alert delivery | `student/operations.alerts()` returns what has crossed the threshold; nothing emails or webhooks it. Where an alert goes is a deployment decision and `student/notifications.py` owns delivery. |
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
| A packaged mobile app | The Android WebView path is manually tested (`docs/ANDROID_MANUAL_TEST.md`); there is no store build. |
| Offline use | Every screen needs the backend. An outage is reported as an outage and never as empty data. |

## Known defects, recorded rather than fixed

| Defect | Why it is still here |
|---|---|
| TLS errors are typed as `TimeoutError` | In `benchmark/providers/nvidia.py`, a `URLError` becomes `TimeoutError` whatever caused it. Misleading in a traceback; no behavioural consequence, since both are transport failures the retry loop handles identically. |
| Auth rejections are retried three times | A 401 will never succeed on retry. Costs two wasted attempts on a misconfigured credential, and nothing else. |
| `runs.record` names artifacts `{timestamp}_{kind}_{config}.json` | Two runs in the same second with the same config label overwrite each other. Not reachable in the normal flow, where arms carry different labels. |

## Deliberately absent, not missing

| Thing | Why |
|---|---|
| Independent redraw per arm in a validator run | Recorded in `benchmark/journal.FORFEITED_BY_DESIGN`. Arms share observations so the ablation's difference does not carry sampling noise; measuring run-to-run variance now needs its own mechanism. |
| A check for `trivial` defects | `validator/scripted.UNCOVERED_BY_DESIGN`. The only check that saw them compared against unreviewed difficulty labels, so it no longer gates. Four planted defects go uncaught, and that is recorded rather than hidden. |
| Sanitising prompt-injection phrases out of uploaded text | A blocklist over natural language loses to rephrasing. The fence in `student/untrusted.py` makes the boundary unforgeable instead; `structure_markers()` reports suspicious text without filtering it. |
