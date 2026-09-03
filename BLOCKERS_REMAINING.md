# Quintek Android — Remaining Blockers

## Production blockers (A)

**None found in the Android application code.**

## Functional bugs (B) — both resolved

| ID | Blocker | Resolution |
|---|---|---|
| B-1 | In-app backend guidance names `serve-analytics`, which does not serve the learner routes. Following it leaves the student app dead. | Fix unambiguous — correct the strings. |
| B-2 | One backend setting cannot serve both shipped screens. | **FIXED.** `AnalyticsMount` composes the console's read-only routes into the learner server behind `--with-console`, using the mount pattern `billing/mount.py` already established. One origin, both screens, verified live. |

## Deployment prerequisites (not defects)

1. **Android SDK** — absent here; no APK has been built or verified.
2. **Signing configuration** — `build.gradle.kts` defines no `signingConfigs`; a release AAB needs a keystore.
3. **A reachable backend** — `serve()` binds `127.0.0.1` by default; a phone needs `--host 0.0.0.0` and a LAN-reachable address, or a deployed HTTPS origin.
4. **HTTPS + network-security-config** — required before any release outside a trusted LAN (C-1).
5. **A qualified model** — the learner journey cannot generate content until a candidate is promoted. Phase 0 is INCOMPLETE, so this is currently correct and blocking by design.

## What is NOT blocked

Enforcement, transparency, provenance, credential handling, failure honesty
and the refusal paths are all verified working against live servers.
