# Execution Flow

Traced from source, not sketched. Every function name and path below was read
out of the repository. Where a path is **not verified by execution**, it says
so.

Verified by live HTTP against a running server on 2026-09-03 unless marked
otherwise.

## Layout

```
android/app/src/main/java/com/quintek/app/   WebScreenActivity, MainActivity, AdminActivity, Screens, Settings
android/app/src/main/assets/                 pg-revision.html, quintek-admin.html   (generated, gitignored)
frontend/                                    *.dc.html sources + quintek-*.js API modules
student/                                     server, api, db, ai, generation, validation, knowledge, revision, ingestion
benchmark/                                   discovery, analytics_api, analytics_mount, providers/, registry, promotion_api
validator/                                   structural, grounding, judge, conformance, pipeline, freeze, holdout
billing/                                     mount, api, economics
```

---

# 1 · ANDROID → BACKEND

**Not verified by execution.** No APK has been built (ADR-021); this is a
source trace.

```
Launcher icon
  └─ MainActivity : WebScreenActivity          MainActivity.kt   (screen = Screen.STUDENT)
Long-press icon → shortcut (res/xml/shortcuts.xml)
  └─ AdminActivity : WebScreenActivity         AdminActivity.kt  (screen from intent extra)

WebScreenActivity.onCreate()                   WebScreenActivity.kt:74
  ├─ webView.settings: javaScriptEnabled=true, domStorageEnabled=true,
  │                    allowFileAccess=false, allowContentAccess=false
  ├─ webViewClient.shouldInterceptRequest(view, request)          :101
  │     ├─ Settings.backendUrl(context)         Settings.kt:23  (SharedPreferences "quintek"/"backend_url")
  │     ├─ assets.open(screen.asset)            streams ~4.6-4.9 MB, not read into memory
  │     └─ if backend set: SequenceInputStream(prefix, asset)
  │           prefix = <script> window.__QUINTEK_API__ = <url>;
  │                            window.__QUINTEK_STUDENT_API__ = <url>; </script>
  │           quote()                            :171  escapes \ " and <
  ├─ webChromeClient.onShowFileChooser(...)      :148  → fileChooser ActivityResultLauncher
  └─ onBackPressedDispatcher callback            :160  webView.canGoBack() ? goBack() : finish()
```

**Why injection happens at request time:** `quintek-eval-api.js` reads its
global at module-evaluation time, so `onPageFinished` would be too late.

**Auth boundary:** none in Android. The bearer token lives in the WebView's JS
(`quintek-student-api.js` module state). Android holds no credential.

---

# 2 · FRONTEND → HTTP

`frontend/quintek-student-api.js`

```
BASE = window.__QUINTEK_STUDENT_API__ || null            :21
  └─ if (!BASE) throw new BackendError(0, 'no learner backend is configured')   :36
  └─ fetch failure → BackendError(0, 'the learner backend could not be reached') :48
  └─ !res.ok      → BackendError(res.status, payload.error || res.statusText)    :54
```

**No fixtures exist in this module** (ADR-018). `quintek-eval-api.js` has
fixtures but uses them *only* when `__QUINTEK_API__` was never set; a
configured-and-failing backend yields emptiness plus `isOutage` / `loadError`.

| Module | Global | Paths |
|---|---|---|
| `quintek-student-api.js` | `__QUINTEK_STUDENT_API__` | `/auth/*`, `/capabilities`, `/demos`, `/notebooks*`, `/questions*`, `/progress`, `/sources*`, `/ai/benchmark/powering` |
| `quintek-billing-api.js` | `__QUINTEK_STUDENT_API__` + `/billing` | `/me/entitlements`, `/me/usage`, `/me/subscription*` |
| `quintek-eval-api.js` | `__QUINTEK_API__` | `/ai/eval` |
| `quintek-report-api.js` | `__QUINTEK_API__` | `/api/runs`, `/api/gates`, `/api/preflight`, `/api/datasets/*` |
| `quintek-admin-billing.js` | `__QUINTEK_STUDENT_API__` + `/billing/admin` | `/economics*` |

---

# 3 · BACKEND DISPATCH

`student/server.py`

```
ThreadingHTTPServer → Handler                       make_handler(api, billing, analytics)  :132
  do_GET/do_POST/do_PUT → _dispatch(method)         :151
    ├─ raw body read ONCE, before parsing           (webhook signature is over these bytes)
    ├─ length > MAX_REQUEST_BYTES → 413             checked BEFORE reading
    ├─ analytics.owns(path)  → AnalyticsMount.handle(method, path, parse_qs(query))
    │      benchmark/analytics_mount.py  — dict[str, list[str]], NOT the flattened form
    │      non-GET → 405
    ├─ billing.owns(path)    → BillingMount.handle(...)   billing/mount.py:105
    │      is_admin resolved from the bearer token against the student DB, never the body
    └─ else                  → StudentAPI.handle(method, path, params, body, token)
    _send(status, body)  →  Access-Control-Allow-Origin: CORS_ORIGIN (env, default "*")
```

`student/api.py`

```
StudentAPI.handle(...)                              :92
  └─ _route(method, path, params, body, token)      :142
       ├─ seg == ["health"]        → _health()      :101   ← unauthenticated, no session
       ├─ seg[:1] == ["auth"]      → _auth(...)     :356   ← unauthenticated
       ├─ seg == ["capabilities"]                          ← unauthenticated by design
       ├─ seg[0] == "ai"           → _ai(...)       :306   ← GET only; non-GET → 405
       └─ everything else          → _user(token)   :136   ← AUTH BOUNDARY; 401 if absent/invalid
  exceptions: ApiError → (status, payload); anything else → 500 with type+message, never a stack trace
```

---

# 4 · STUDENT JOURNEY (verified by live HTTP)

| # | Step | Frontend fn | Route | Backend fn | Tables |
|---|---|---|---|---|---|
| 1 | Register | `register()` | `POST /auth/register` | `_auth` → `Database.create_user` `:145` → `issue_token` `:180` | `users`, `sessions_auth` |
| 2 | Login | `login()` | `POST /auth/login` | `verify_password` `:168` → `issue_token` | `sessions_auth` |
| 3 | Session | `setToken`/`hasToken` | header | `Database.user_for_token` `:189` | `sessions_auth` |
| 4 | Notebook | `createNotebook()` | `POST /notebooks` | `create_notebook` `:404` | `notebooks` |
| 5 | Add source | `addSource()` | `POST /notebooks/{id}/sources` | `add_source` `:442` → `IngestionEngine` | `sources`, `source_chunks` |
| 6 | Concepts | `waitForSource()` | `GET /notebooks/{id}` | `AIConceptExtractor.extract_for_chunk` `generation.py:76` | `concepts`, `source_concepts` |
| 7 | Generate | `generateQuestions()` | `POST /notebooks/{id}/questions` | `generate_questions` `:534` → `QuestionGenerator.generate` `:282` | `questions`, `question_concepts` |
| 8 | Validate | — | (inline) | `QuestionValidator.validate` `validation.py:86` | `questions.validation_status` |
| 9 | Question bank | `questionBank()` | `GET /questions` | `question_bank` `:625` | `questions` |
| 10 | Attempt | — | `POST /attempts` | `record_attempt` `:759` → `KnowledgeStore.record_attempt` `knowledge.py:74` | `attempts`, `concept_state`, `knowledge_gaps` |
| 11 | Progress | — | `GET /progress` | `progress` `:808` | `attempts`, `concept_state` |
| 12 | Revision | — | `GET /revision/dashboard` | `RevisionEngine.dashboard` `revision.py:185` | `revision_state` |
| 13 | Session | — | `POST /revision/sessions` | `start_session` `:751` → `RevisionEngine.start_session` `:322` | `revision_sessions` |
| 14 | Next | — | `GET /revision/next?session=` | `RevisionEngine.next_question` `:347` | `revision_sessions` |
| 15 | Transparency | `powering()` | `GET /ai/benchmark/powering` | `_ai` → `transparency` `:74` | run archive |

**Verified behaviours worth recording:**

- `record_attempt` **reveals the answer only in its own response** — never
  before. `user_colour` must be `RED`/`ORANGE`/`GREEN`; the error says *"the
  learner chooses it and the system never infers it"*.
- `user_answer` must be an option **index**, not a letter.
- Logout revokes the token; `/me` then returns 401.

---

# 5 · AI ROUTING — where the refusal happens

`student/ai.py`

```
AIEngine.call(task_type, prompt, ...)               :128
  └─ resolve(task_type)                             :100   ← THE GATE
       ├─ active_deployment(task_type)   → (candidate_id, "promoted")
       ├─ Router(registry, archive).select(task)
       │      benchmark/router.py — filters retired, unqualified, unhealthy
       │                          → (candidate_id, "routed")
       ├─ self.development_candidate    → (candidate_id, "development_override")
       └─ raise NoEligibleModel                      :121
            "no model is available for {task}: nothing is promoted, no candidate
             is benchmark-eligible, and no development candidate is configured"
  └─ provider_factory(candidate)  → benchmark/providers/registry.build_provider
  └─ provider.generate(GenerationRequest)  → NVIDIAProvider._call  (or ScriptedProvider)
  └─ _record(execution_id, task_type, candidate_id, source, provider, response, prompt_version)
        ↑ the SOURCE is persisted — this is how provenance answers "why this model"
```

**`NoEligibleModel` is raised at `student/ai.py:121`.** That is the exact point
at which "no qualified model" becomes a refusal. Current state: it fires, and
ingestion/generation refuse. Verified live with the override unset.

**Development override vs production routing:**

| | `promoted` / `routed` | `development_override` |
|---|---|---|
| Source of authority | benchmark evidence | an environment variable |
| Recorded in provenance as | `promoted` / `routed` | `development_override` |
| Enabled by default | n/a | **no** — `QUINTEK_DEV_CANDIDATE` must be set |
| May serve production | yes | **never** — `docs/DEPLOYMENT.md` marks it "never in production" |

---

# 6 · ADMIN (verified by live HTTP, one origin with `--with-console`)

```
GET /ai/discovery, /ai/discovery/retired, /ai/routing/current, /ai/leaderboard,
    /ai/eval/state, /ai/eval/history          → AnalyticsMount → AnalyticsAPI.handle
GET /api/runs, /api/gates, /api/preflight     → AnalyticsAPI → RunsAPI
GET /billing/admin/economics                  → BillingMount → BillingAPI._route → _admin  billing/api.py:236
```

**Authorization boundary:** `billing/api.py:106` — `if seg[0] == "admin"` and
`not is_admin`, it returns **404, not 403**, deliberately: whether an admin
surface exists is itself not disclosed. `is_admin` comes from the user row's
`role`, resolved from the bearer token (`billing/mount.py:128`).

**Promotion is not reachable through the mount** — non-GET returns 405.

---

# 7 · DATABASE

```
Database(path)                                  student/db.py:60
  ├─ connect()                                  :67   PRAGMA foreign_keys=ON per connection,
  │                                                    WAL, busy_timeout — FKs are per-connection
  │                                                    and silently off by default in SQLite
  ├─ initialise() → schema.sql                  :89
  ├─ _apply_additive_migrations(conn)           :95
  └─ execute / query / query_one                :125-134
```

**21 tables:** users, sessions_auth, notebooks, sources, source_chunks,
concepts, concept_aliases, concept_relationships, notebook_concepts,
source_concepts, question_demos, questions, question_concepts,
revision_sessions, attempts, knowledge_gaps, gap_links, revision_state,
concept_state, notification_prefs, notification_log, production_deployments.

**2 triggers:** `attempts_are_immutable_update`, `attempts_are_immutable_delete`
— an attempt, once recorded, cannot be rewritten.

**Persistence boundary:** local SQLite files. On Render this is **ephemeral**
(ADR-020) — this section must be rewritten when Postgres lands.

---

# 8 · WHAT IS NOT VERIFIED

| Path | Status |
|---|---|
| Android activity launch, WebView render, on-device journey | **NOT TESTED** — no APK (ADR-021) |
| HTTPS / TLS to a deployed origin | **NOT TESTED** — nothing deployed |
| Postgres/Supabase path | **DOES NOT EXIST** (ADR-020) |
| Production generation end-to-end | **CANNOT BE TESTED** — no qualified model, by design |
