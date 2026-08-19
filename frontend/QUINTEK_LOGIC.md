# Quintek — Application Logic Report

Written to be handed to an engineer (or an AI coding agent) as the single description of
what exists, what it does, and how to wire it to a backend. Everything below is traceable
to a file in this project. Where something is prototype-only it says so explicitly —
nothing here claims functionality that has not been built.

---

## 0. Two systems, one boundary

There are two products in this project. They must not share a data store, credentials, or
a code path.

| | PRODUCTION (student) | BENCHMARK (internal) |
| --- | --- | --- |
| File | `PG Revision.dc.html` | `Quintek Admin.dc.html`, `Quintek Harness.dc.html` |
| Audience | Post-MBBS exam candidates | Engineering / research team |
| Serves | The learner's own uploaded sources | A frozen independent corpus |
| Loop | source → concepts → questions → revision | candidate → evaluation → gates → scorecard |
| Viewport | Mobile, 390×844 | Desktop 1440×900 (harness also mobile) |

**The rule:** a user's uploaded source, and anything the production pipeline generates from
it, may never become benchmark gold and may never recalibrate a benchmark threshold. The
student app must not depend on benchmark execution — it consumes a published result only
(§7).

---

## 1. The core loop (this is the product)

```
SOURCE → NOTEBOOK → CONCEPTS → CONCEPT GRAPH → QUESTIONS → ATTEMPT
  → USER R/O/G JUDGEMENT → KNOWLEDGE GAP → CONCEPT PERFORMANCE
  → CONCEPT PRIORITY → REVISION QUEUE → DAILY TRIGGER → NOTIFICATION
  → REVISION SESSION → NEW EVIDENCE → UPDATED KNOWLEDGE STATE → repeat
```

Every screen exists to serve one arrow of that loop. The differentiator is not question
generation — it is that the learner can say *what* they don't know, *why*, *where it came
from*, and *what should be tested next*.

---

## 2. Screens as built, and the routes behind them

The student app is a single Design Component with a `route` string in state. Bottom tabs:
**Today · Weak · Revise · Graph · More**.

| route | Screen | What its logic does |
| --- | --- | --- |
| `today` | Home | Trigger status, due count, top priority gaps, notebook list |
| `home` | Notebooks | Notebook cards: source/concept/question counts, due badge, mastery bar |
| `notebook` | Notebook detail | Sources ↔ concepts, incremental extraction, per-source "make questions" |
| `make` | Make questions | Count (incl. custom 0–500), kinds, grounding note, generation, start |
| `concept` | Concept page | R/O/G state, priority + why, gaps, questions by notebook, attempts, source |
| `graph` | Concept graph | Force-directed nodes, subject filters, cross-subject edges dashed |
| `revise` | Session | One question, submit, reveal, user R/O/G, gap tagging, next |
| `srcview` | In the source | The exact passage/figure a question or concept came from |
| `dashboard` | Revision dashboard | R/O/G counts, recommended vs selected count, strategy, start |
| `weak` | Forgotten / weak | Gap list filtered by colour, each linking concept + questions + source |
| `progress` | Progress | Mastery split, per-concept recall strength, 12-week heatmap |
| `studio` | Question Studio | Source/concepts/type/difficulty/count/demos + generate, accept/edit/reject |
| `demos` | Demonstration library | Framing records the generator uses for structure, not facts |
| `bank` | Question bank | Generated questions with provenance and validation state |
| `settings` | Settings | Daily trigger time + note, channels, notebooks, account |
| `trust` | Quintek reliability | Published evaluation summary, task routing, leaderboard |
| `onboard` | Onboarding | 3 steps: source type → links → kinds + count, then straight into practice |

Session entry points, each carrying its own count into `sessionCount`:
- `startMade` — from the Make screen, carries `makeCount`
- `startQueued` — from the revision dashboard, carries `qCount`
- `stepNext` at step 3 — from onboarding, carries `obCount` (blocked at 0)

---

## 3. Data model

Illustrative columns, matching what the UI actually reads.

```
users(id, email, name, role[learner|admin], timezone, created_at)

notebooks(id, owner_id, title, subject, created_at)
  -- source-oriented, NOT subject-equivalent. A chapter, a concept, or one
  -- lecture can each be a notebook.

sources(id, notebook_id, kind[pdf|image|link|video|text|note], filename,
        storage_key, mime_type, status[uploaded|chunking|processing|extracted|failed],
        page_count, uploaded_at)

source_chunks(id, source_id, ordinal, text, locator_json, status, processed_at)
  -- locator_json is the provenance unit: {page, paragraph, lines} for text,
  -- {page, figure, caption} for a figure, {t_start, t_end} for video.
  -- Resumability lives here: a failed chunk 87 does not reset 1-86.

concepts(id, canonical_name, subject, description, embedding, first_seen_at)
  -- GLOBAL. One 'Ferritin' row referenced from Medicine, Biochemistry and
  -- Pathology. Never duplicated per notebook.

concept_aliases(id, concept_id, alias)

concept_relationships(id, source_concept_id, target_concept_id, relation_type,
                      confidence, provenance_source_id)
  -- relation_type: related_to | prerequisite_of | causes | caused_by |
  --   mechanism_of | manifestation_of | diagnostic_feature_of |
  --   complication_of | treatment_of | differential_of | contrasts_with |
  --   measured_by | associated_with   (extensible — store as text + lookup)

notebook_concepts(notebook_id, concept_id, role[primary|supporting])
source_concepts(source_id, concept_id, chunk_id)

questions(id, primary_notebook_id, family, stem, options_json, correct_index,
          rationale, difficulty, reasoning_depth, source_id, chunk_id,
          generated_by_candidate_id, prompt_version, demo_ids_json,
          validation_status[pending|approved|flagged], generated_at)
question_concepts(question_id, concept_id, role[target|supporting])
  -- many-to-many: a Medicine question can test a Biochemistry concept and
  -- stays in the Medicine notebook.

question_demos(demo_id, title, question, question_type, difficulty,
               reasoning_depth, stem_structure, question_target,
               distractor_strategy, answer_format, notes, source_id, created_at)

attempts(id, question_id, session_id, user_id, user_answer, correct_answer,
         is_correct, user_colour[RED|ORANGE|GREEN], concepts_tested_json,
         knowledge_gaps_json, source_refs_json, created_at)
  -- IMMUTABLE. Never updated, never deleted. History is the evidence base.

knowledge_gaps(id, user_id, label, concept_id, colour, first_seen_at,
               last_seen_at, resolved_at)
gap_links(gap_id, question_id, attempt_id, notebook_id, source_id, chunk_id)

revision_state(id, user_id, question_id, ease_factor, interval_days, due_at,
               last_reviewed_at, last_result, consecutive_correct)

revision_sessions(id, user_id, start_time, end_time, recommended_question_count,
                  selected_question_count, selection_strategy,
                  selected_question_ids_json, completion_status)

notification_prefs(user_id, trigger_time, timezone, push_enabled, email_enabled,
                   note_text, last_status, last_sent_at, next_scheduled_at)

candidate_manifests(id, provider, model_id, model_version, system_prompt_hash,
                    decoding_config, code_commit, manifest_hash UNIQUE)
benchmark_runs(id, candidate_manifest_id, run_id, outcome, report_json, imported_at)
production_deployments(id, candidate_manifest_id, benchmark_run_id, role,
                       activated_at, activated_by, signoff_name, signoff_rationale,
                       deactivated_at)
```

---

## 4. Pipelines

### 4.1 Ingestion (background job, never in an HTTP request)

```
upload → status=uploaded → enqueue
  chunk source                       (never send a large source in one call)
  for each chunk, resumably:
    extract candidate concepts + relationships
    persist chunk.status
  synthesize across chunks
  resolve against global concepts    (name + embedding similarity)
  write concepts, relationships, notebook_concepts, source_concepts
  status=extracted
```

Concept resolution is the risky step: a **false merge silently corrupts the user's graph**.
Use the same precision thinking as the benchmark's resolution track — prefer leaving two
concepts separate over merging wrongly.

The UI reflects this honestly: the notebook screen shows page-by-page progress with
concepts appearing as they are found, not a spinner.

### 4.2 Generation

Input to the generator, all of it required:

```
source passages (grounding) + target concepts + related concepts
  + cross-notebook context + question type + difficulty + reasoning depth
  + demonstration examples (structure only) + user constraints
```

Demonstrations supply **style, structure, reasoning depth and distractor strategy** — never
facts. Every stored question keeps `source_id`, `chunk_id`, `generated_by_candidate_id`,
`prompt_version` and `demo_ids`, which is what makes the "In the source" screen possible.

### 4.3 Validation — a separate call

Runs as its own job against a different model config than generation used (mirrors the
benchmark's judge-independence rule). Checks factual correctness, source grounding, the
keyed answer, distractor quality, ambiguity, concept alignment, unsupported claims,
duplication, PG-level suitability. **Flags, never silently discards.** The validator must
not see the generator's reasoning.

---

## 5. The three rules that shape the learner logic

### 5.1 The user owns R/O/G

The system never picks the colour. Order is fixed and enforced in the UI:

```
answer → submit → reveal (your answer, correct answer, explanation,
  concepts tested, source refs) → USER chooses RED / ORANGE / GREEN
  → gap tagging → next
```

RED = forgot / seriously weak. ORANGE = partial / uncertain. GREEN = confident.
Analytics may sit alongside the colour; they must never overwrite it. The answer is not
revealed before submission.

### 5.2 Gaps are specific, never "Anemia = weak"

A wrong answer or a RED/ORANGE judgement records named gaps:

```
Concept: Anaemia
Gaps: ferritin interpretation · transferrin saturation · iron absorption ·
      IDA vs ACD differentiation
```

Each gap links to concept, question, attempt, notebook and source chunk — which is why the
Weak screen can offer a source jump per gap.

### 5.3 Priority is deterministic code, not an LLM opinion

Rank concepts from stored signals only:

- current user colour (RED > ORANGE > GREEN)
- wrong frequency and repeated failure across attempts
- unresolved gap count
- recency / overdue interval
- successful retrieval streak, and improvement trend
- question coverage across families

Same inputs must always give the same order. The concept page shows *why* a concept sits
where it does, from those signals.

---

## 6. Revision engine

Default selection order (configurable):

```
1 RED knowledge gaps
2 RED concepts
3 ORANGE knowledge gaps
4 ORANGE concepts
5 previously incorrect questions
6 due concepts
7 full-section coverage
8 unseen questions
```

Two behaviours that matter:
- **Do not just re-serve the failed question.** Test the same concept with new items where
  they exist.
- **Weak + full section.** If `ferritin interpretation` is RED, the session pulls ferritin
  interpretation, iron studies, iron metabolism, related concepts, selected previous wrong
  questions, and new questions — targeted weakness *and* section coverage.

Session persistence: on start, write a `revision_sessions` row with
`recommended_question_count`, `selected_question_count`, `selection_strategy` and the exact
`selected_question_ids`. The served set must be reproducible afterwards.

On completion compute accuracy, correct/incorrect, R/O/G split, weak concepts, unresolved
gaps, improved and deteriorating concepts, next targets — then update knowledge state and
emit an exact read list:

```
READ / REVISE
1 Ferritin interpretation      → source ref
2 Transferrin saturation       → source ref
3 Iron absorption              → source ref
4 IDA vs ACD                   → source ref
```

Never "revise Anemia".

### Daily trigger

The user picks one time (and timezone). At that time, every day: recalculate revision
state → build/update the queue → send a real notification carrying the user's own note →
send email if configured. The system never changes the chosen time on its own. Settings
exposes trigger time, timezone, push/email toggles, a test send, last status and next
scheduled trigger.

---

## 7. Benchmark → production boundary

The student app shows a **published** evaluation only, read from stored fields: overall
reliability, per-track scores with status, sample size, confidence interval, evaluation
date, current configuration, and which config serves which task. It computes none of it.
Missing data renders "evaluation unavailable" — never a placeholder number.

Promotion gate (`POST /admin/deployments`) must, in code:

1. look up the referenced `benchmark_run`
2. reject unless `outcome === 'PASS'`, or `'CONDITIONAL'` **with** a stored sign-off name
   and rationale
3. reject if the run's `candidate_manifest_id` differs from the manifest being promoted —
   you cannot promote candidate A on candidate B's run
4. on success deactivate the previous row (never delete) and insert the new one
5. the pipeline reads "current production candidate" as **data at call time**, so a bad
   promotion is reversible without a redeploy

---

## 8. API surface

### Learner

```
POST   /notebooks
POST   /notebooks/:id/sources              multipart or {kind, url, text}; enqueues extraction
GET    /notebooks/:id                      sources + concepts + counts
GET    /sources/:id/progress               chunk-level extraction state (poll or SSE)
POST   /sources/:id/questions              {count 0..500, families[], other_kind}
GET    /concepts/:id                       state, priority, why, gaps, questions by notebook
GET    /concepts/:id/graph?depth=          nodes + edges, cross-subject flagged
GET    /concepts/:id/source                passage/figure + locator + context lines
GET    /gaps?colour=                       the Weak screen
GET    /revision/dashboard                 R/O/G counts, recommended_question_count
POST   /revision/sessions                  {selected_question_count, strategy} → session_id + items
GET    /revision/next?session=
POST   /attempts                           {question_id, session_id, answer, user_colour, gaps[]}
POST   /revision/sessions/:id/complete      → analysis + read list
GET    /progress
GET    /search?q=                          notebooks, concepts, questions, gaps, sources, attempts, sessions
GET    /settings/notifications  ·  PUT same  ·  POST /settings/notifications/test
GET    /demos  ·  POST /demos
POST   /studio/generate                    full config → drafts with provenance
POST   /questions/:id/accept | /reject | /regenerate
```

### Admin — already contracted, see `quintek-report-api.js`

```
POST   /api/runs                    async; returns {run_id, status:"queued"}
GET    /api/runs                    list, paginated
GET    /api/runs/:run_id            the unmodified report.json
GET    /api/runs/:run_id/integrity
GET    /api/runs/:run_id/report.md
POST   /api/datasets/validate       re-validated server-side; 422 on invalid
GET    /api/datasets/:hash
GET    /api/gates                   the registry
GET    /api/preflight?dataset=      cost projection + outcome ceiling, before running
```

**The load-bearing rule.** If `outcome` is `INVALID_RUN` or `INCOMPLETE`, the API returns
`scores: null` — never `{}`, never partially filled, never numbers beside a `withheld`
flag. A consumer reading `scores.A_medical_qa` on a suppressed run must get a
null-reference error, not a stale figure. `assertSuppression()` in
`quintek-report-api.js` enforces this at the boundary; mirror it with an API-layer test.

Access notes: reject non-`.jsonl` and oversized uploads before the validator sees them;
`split=holdout` items must never be servable to any frontend under any role — filter at
the query layer, not the UI.

---

## 9. What is real vs prototype in the files today

Everything visual is built and interactive. Nothing behind it is a live engine.

| Feature | State |
| --- | --- |
| All screens, routes, navigation, empty states | **Built** |
| Concept graph force simulation | **Built** — real physics, fixture nodes |
| R/O/G judgement flow, gap tagging, immutable attempt push | **Built in-session** — `this.attempts` array, not persisted |
| Question count threading into a session | **Built** — `sessionCount` from all three entry points |
| Source jump with locator + figure treatment | **Built** — locators in a `DOC_LOC` fixture |
| Admin scorecard, integrity, registry, routing, failure analysis | **Built**, reading the report.json contract |
| Suppression rule (`scores: null`) | **Built and asserted** |
| Counts, mastery %, streak, "30 due today", 188 concepts | **Prototype values, labelled "Demo data" in the UI** |
| Extraction, generation, validation, SM-2 scheduling, notifications | **Not implemented** — UI simulates timing only |
| Provider adapters, real benchmark execution | **Not implemented** — deliberately, per the wiring brief |

The UI never presents a fabricated number as live: prototype figures carry a visible
"Demo data" banner, and unavailable evaluation data renders an explicit unavailable state.

---

## 10. Build order

1. **Foundations** — auth, notebooks CRUD, source upload to object storage, chunker,
   extraction job skeleton (stub the model call). Wire the notebook screen to real rows.
2. **Concept graph** — real extraction + resolution, `concept_relationships`, global
   concepts. Wire the graph and concept page.
3. **Questions + revision** — generation job, separate validation job, SM-2
   `revision_state`, sessions, attempts, gaps, deterministic priority. Wire session,
   dashboard, weak, progress.
4. **Admin** — import `report.json`, candidate manifests, comparison, run history.
   (`quintek-report-api.js` is the contract; swap `BASE` and delete `FIXTURES`.)
5. **Promotion gate** — §7 in code, `candidate_manifest_id` threaded through every AI call
   site, deployment audit trail.

Do not build 5 before 4 exists to feed it real runs — a gate with no runs to reference is
a hardcoded bypass waiting to happen.

---

## 11. Non-functional

- **Cost tracking** — log tokens, latency and estimated cost per AI call, tagged by
  `candidate_manifest_id`, so cost-per-question-generated is queryable.
- **Observability** — structured logs per job (`source_id`, job type, duration, outcome);
  surface queue health on the admin console.
- **Security** — uploaded sources may contain identifiable case material. Encrypt at rest,
  scope to the owning user, never use for anything but that user's notebooks.
- **Separation** — production credentials and database fully separate from anything the
  benchmark touches.
- **Video** — not in scope now, but keep the interfaces open: `source_chunks.locator_json`
  already carries a timestamp shape, and the source view already branches on medium.

---

## Files in this project

| File | What it is |
| --- | --- |
| `PG Revision.dc.html` | The student app (mobile) |
| `PG Revision standalone.dc.html` | Offline single-file copy of the above |
| `Quintek Admin.dc.html` | Benchmark console (desktop) |
| `Quintek Harness.dc.html` | Harness, mobile |
| `Quintek Audit.dc.html` | Audit view |
| `Quintek PG Revision.html` | Earlier standalone student build |
| `quintek-report-api.js` | report.json contract, endpoints, gate registry, suppression assertion |
| `quintek-eval-api.js` | Published evaluation data the student trust screen and admin analytics read |
| `github.md` | Source repo association and screen map |
