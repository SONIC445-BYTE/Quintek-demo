# Change Protocol

**This is the project's operating rule. It outranks tool permissions.**

Claude Code is operated here with bypass permissions. That is a statement
about what the tooling *can* do, not about what is *authorized*. Permission to
write a file is not authorization to change the system.

> **Bypass permissions do NOT override this protocol.**

## Rule 1 — Inspection is free

No proposal and no quiz are needed to: read code, search files, read git
history, run read-only tests, trace execution, read logs, identify defects, or
propose changes.

Inspection is encouraged. It is how defects get found.

## Rule 2 — Before any mutation, stop

Before modifying **any** of:

source code · tests · database schema · migrations · configuration ·
dependencies · Android code · deployment files · prompts · provider
configuration · routing policy · validator behaviour · corpus · thresholds ·
freeze · documentation recording a finalized decision

Claude must first present a **CHANGE PROPOSAL** answering all eighteen:

1. What is changing?
2. Why is it changing?
3. Current behaviour?
4. Desired behaviour?
5. Exact files affected?
6. Dependencies affected?
7. Risks?
8. Alternatives?
9. Why this approach?
10. What could break?
11. How will it be tested?
12. Does it change architecture?
13. Does it change security?
14. Does it change data persistence?
15. Does it change qualification semantics?
16. Does it change production behaviour?
17. Does it require credentials/secrets?
18. Does it change a previous architectural decision?

Then **STOP**.

## Rule 3 — Quiz the user

After the proposal, Claude quizzes the user on the *actual* proposed change:
why it is necessary, what it changes, what it does **not** change, trade-offs,
and security / persistence / production / rollback implications.

| Change class | Minimum questions |
|---|---|
| Small | 3 |
| Architectural, security, database, provider, deployment | 5 |
| High-impact | 7 |

The quiz tests understanding. It is not a hazing ritual — do not make it
artificially difficult, and do not ask about things the proposal did not say.

## Rule 4 — A pass is required

**No mutation until the user has answered the quiz and passed.**

Authorization looks like `PASS`, `Proceed`, `All correct, execute` — *after*
the quiz has been answered and evaluated.

- **Failed:** explain the wrong answers, reteach, give another short quiz. Do
  not execute.
- **Unanswered:** stop. Do not execute.

## Rule 5 — Document before executing

On a pass:

1. Add or update the decision in `DECISIONS.md`.
2. Mark it `STATUS: APPROVED — IMPLEMENTING`.
3. Only then change code.

## Rule 6 — After executing

1. Run the appropriate tests.
2. Report exact results — real numbers, including failures.
3. `DECISIONS.md` → `STATUS: IMPLEMENTED`.
4. Update `FLOW.md` if execution behaviour changed.
5. Record commit, files changed, tests, observed behaviour, remaining
   limitations.

## Rule 7 — Discussion is not authorization

"what if…", "maybe…", "could we…", "should we…", "I think…" are **analysis**.
None of them is permission. Only explicit post-quiz authorization permits
mutation.

## Rule 8 — Record material discussions as they happen

If a discussion changes or clarifies architecture, database, deployment,
provider, model, routing, security, product behaviour, validation,
qualification, Android or infrastructure — update the relevant document then,
not at the end of the project.

## Rule 9 — Signal, not noise

An ADR is for a durable architectural, behavioural, security, data,
infrastructure or product rule.

Typos, formatting, obvious test corrections, comments and mechanical
zero-behaviour refactors go in implementation notes, not ADRs.

## Rule 10 — No silent architectural drift

If implementation reveals the current architecture is wrong or insufficient,
**stop** and present:

```
CURRENT DECISION → NEW EVIDENCE → CONFLICT → OPTIONS → RECOMMENDATION
```

Then quiz before changing it. This has already happened twice in this project
(D014 → D016, and the first `answerable_from_wording_alone` patch), so it is
not hypothetical.

## Rule 11 — Never invent history

Reconstruct only from recoverable evidence: the repository, git history,
existing documentation, commit messages, implementation artifacts.

Where something cannot be established, write **`RATIONALE NOT RECOVERED —
requires confirmation`** or **`UNKNOWN / NOT RECOVERED`**.

Never manufacture dates, reasons, discussions, user approvals, or
architectural intentions. A plausible-sounding rationale that nobody actually
had is worse than an admitted gap, because the gap invites a question and the
invention closes it.

## Rule 12 — Living records

| Document | Updated when |
|---|---|
| `DECISIONS.md` | after every material finalized change |
| `FLOW.md` | when execution behaviour changes |
| `CHANGE_PROTOCOL.md` | only when governance itself changes |

## What this protocol is protecting

This project's authoritative state is **NO MODEL QUALIFIED / INSUFFICIENT
EVIDENCE**. That is a safe state only because a series of specific refusals
hold: the promotion gate, the retirement filter, the holdout ledger, the
freeze digest, the capability provenance.

Every one of those could be "fixed" by a well-meaning change that made
something work. The quiz exists so that a human sees the trade before that
happens.
