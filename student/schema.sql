-- Quintek student engine schema.
--
-- Follows docs/QUINTEK_LOGIC.md section 3. Three properties are enforced here
-- rather than left to application code, because each one is a decision the
-- product rests on and application code drifts:
--
--   1. CONCEPTS ARE GLOBAL. There is one `Ferritin` row, referenced from
--      Medicine, Biochemistry and Pathology alike. `UNIQUE(canonical_name)`
--      makes a per-notebook duplicate impossible rather than merely discouraged
--      -- duplicating a concept per notebook is what breaks cross-notebook
--      questions, and it is the kind of thing that creeps in through one
--      careless INSERT.
--
--   2. ATTEMPTS ARE IMMUTABLE. History is the evidence base for every colour,
--      priority and gap the product asserts. A trigger below rejects UPDATE and
--      DELETE outright, so "just fix that one row" is not available to anyone,
--      including a future migration written in a hurry.
--
--   3. A QUESTION BELONGS TO MANY CONCEPTS ACROSS NOTEBOOKS. `question_concepts`
--      is a join table with a role, so a Medicine question can test a
--      Biochemistry concept while staying in the Medicine notebook.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- Identity
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS users (
    id           TEXT PRIMARY KEY,
    email        TEXT NOT NULL UNIQUE,
    name         TEXT NOT NULL DEFAULT '',
    role         TEXT NOT NULL DEFAULT 'learner' CHECK (role IN ('learner', 'admin')),
    timezone     TEXT NOT NULL DEFAULT 'UTC',
    password_salt TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions_auth (
    token      TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_auth_user ON sessions_auth(user_id);

-- ---------------------------------------------------------------------------
-- Knowledge containers
-- ---------------------------------------------------------------------------

-- A notebook is source-oriented, NOT subject-equivalent: a chapter, a single
-- concept, or one lecture can each be a notebook.
CREATE TABLE IF NOT EXISTS notebooks (
    id         TEXT PRIMARY KEY,
    owner_id   TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title      TEXT NOT NULL,
    subject    TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_notebooks_owner ON notebooks(owner_id);

CREATE TABLE IF NOT EXISTS sources (
    id          TEXT PRIMARY KEY,
    notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('pdf','image','link','video','text','note')),
    filename    TEXT NOT NULL DEFAULT '',
    storage_key TEXT NOT NULL DEFAULT '',
    mime_type   TEXT NOT NULL DEFAULT '',
    -- Bytes actually stored. Zero for a source that carries no file (text,
    -- link, video), which is why it cannot double as "was anything uploaded".
    byte_size   INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'uploaded'
                CHECK (status IN ('uploaded','chunking','processing','extracted','failed')),
    page_count  INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    uploaded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sources_notebook ON sources(notebook_id);

-- locator_json is the provenance unit: {page, paragraph, lines} for text,
-- {page, figure, caption} for a figure, {t_start, t_end} for video. It is what
-- makes "show me where this came from" answerable.
CREATE TABLE IF NOT EXISTS source_chunks (
    id           TEXT PRIMARY KEY,
    source_id    TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    ordinal      INTEGER NOT NULL,
    text         TEXT NOT NULL,
    locator_json TEXT NOT NULL DEFAULT '{}',
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','processing','processed','failed')),
    error        TEXT,
    processed_at TEXT,
    UNIQUE (source_id, ordinal)
);
CREATE INDEX IF NOT EXISTS ix_chunks_source_status ON source_chunks(source_id, status);

-- ---------------------------------------------------------------------------
-- The concept graph -- global, not per notebook
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS concepts (
    id             TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL UNIQUE,   -- see 1. above
    subject        TEXT NOT NULL DEFAULT '',
    description    TEXT NOT NULL DEFAULT '',
    first_seen_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS concept_aliases (
    id         TEXT PRIMARY KEY,
    concept_id TEXT NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    alias      TEXT NOT NULL,
    normalized TEXT NOT NULL,
    UNIQUE (normalized)
);

CREATE TABLE IF NOT EXISTS concept_relationships (
    id                   TEXT PRIMARY KEY,
    source_concept_id    TEXT NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    target_concept_id    TEXT NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    relation_type        TEXT NOT NULL,
    confidence           REAL NOT NULL DEFAULT 0.0,
    provenance_source_id TEXT REFERENCES sources(id) ON DELETE SET NULL,
    created_at           TEXT NOT NULL,
    UNIQUE (source_concept_id, target_concept_id, relation_type)
);
CREATE INDEX IF NOT EXISTS ix_rel_source ON concept_relationships(source_concept_id);
CREATE INDEX IF NOT EXISTS ix_rel_target ON concept_relationships(target_concept_id);

CREATE TABLE IF NOT EXISTS notebook_concepts (
    notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
    concept_id  TEXT NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    role        TEXT NOT NULL DEFAULT 'primary' CHECK (role IN ('primary','supporting')),
    PRIMARY KEY (notebook_id, concept_id)
);

CREATE TABLE IF NOT EXISTS source_concepts (
    source_id  TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    concept_id TEXT NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    chunk_id   TEXT REFERENCES source_chunks(id) ON DELETE SET NULL,
    PRIMARY KEY (source_id, concept_id, chunk_id)
);

-- ---------------------------------------------------------------------------
-- Questions
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS question_demos (
    id                  TEXT PRIMARY KEY,
    owner_id            TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title               TEXT NOT NULL,
    question            TEXT NOT NULL,
    question_type       TEXT NOT NULL DEFAULT '',
    difficulty          TEXT NOT NULL DEFAULT '',
    reasoning_depth     TEXT NOT NULL DEFAULT '',
    stem_structure      TEXT NOT NULL DEFAULT '',
    question_target     TEXT NOT NULL DEFAULT '',
    distractor_strategy TEXT NOT NULL DEFAULT '',
    answer_format       TEXT NOT NULL DEFAULT '',
    notes               TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS questions (
    id                      TEXT PRIMARY KEY,
    primary_notebook_id     TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
    family                  TEXT NOT NULL DEFAULT '',
    stem                    TEXT NOT NULL,
    options_json            TEXT NOT NULL,
    correct_index           INTEGER NOT NULL,
    rationale               TEXT NOT NULL DEFAULT '',
    difficulty              TEXT NOT NULL DEFAULT '',
    reasoning_depth         TEXT NOT NULL DEFAULT '',
    source_id               TEXT REFERENCES sources(id) ON DELETE SET NULL,
    chunk_id                TEXT REFERENCES source_chunks(id) ON DELETE SET NULL,
    generated_by_candidate_id TEXT NOT NULL DEFAULT '',
    prompt_version          TEXT NOT NULL DEFAULT '',
    demo_ids_json           TEXT NOT NULL DEFAULT '[]',
    validation_status       TEXT NOT NULL DEFAULT 'pending'
                            CHECK (validation_status IN ('pending','approved','flagged','rejected')),
    validation_json         TEXT NOT NULL DEFAULT '{}',
    validated_by_candidate_id TEXT NOT NULL DEFAULT '',
    generated_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_questions_notebook ON questions(primary_notebook_id);
CREATE INDEX IF NOT EXISTS ix_questions_validation ON questions(validation_status);

CREATE TABLE IF NOT EXISTS question_concepts (
    question_id TEXT NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    concept_id  TEXT NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    role        TEXT NOT NULL DEFAULT 'target' CHECK (role IN ('target','supporting')),
    PRIMARY KEY (question_id, concept_id)
);
CREATE INDEX IF NOT EXISTS ix_qc_concept ON question_concepts(concept_id);

-- ---------------------------------------------------------------------------
-- Evidence: attempts, gaps, scheduling
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS revision_sessions (
    id                        TEXT PRIMARY KEY,
    user_id                   TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    start_time                TEXT NOT NULL,
    end_time                  TEXT,
    recommended_question_count INTEGER NOT NULL DEFAULT 0,
    selected_question_count   INTEGER NOT NULL DEFAULT 0,
    selection_strategy        TEXT NOT NULL DEFAULT 'adaptive',
    selected_question_ids_json TEXT NOT NULL DEFAULT '[]',
    completion_status         TEXT NOT NULL DEFAULT 'in_progress'
                              CHECK (completion_status IN ('in_progress','completed','abandoned'))
);
CREATE INDEX IF NOT EXISTS ix_sessions_user ON revision_sessions(user_id);

CREATE TABLE IF NOT EXISTS attempts (
    id                   TEXT PRIMARY KEY,
    question_id          TEXT NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    session_id           TEXT REFERENCES revision_sessions(id) ON DELETE SET NULL,
    user_id              TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    user_answer          INTEGER,
    correct_answer       INTEGER NOT NULL,
    is_correct           INTEGER NOT NULL,
    user_colour          TEXT NOT NULL CHECK (user_colour IN ('RED','ORANGE','GREEN')),
    concepts_tested_json TEXT NOT NULL DEFAULT '[]',
    knowledge_gaps_json  TEXT NOT NULL DEFAULT '[]',
    source_refs_json     TEXT NOT NULL DEFAULT '[]',
    created_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_attempts_user ON attempts(user_id);
CREATE INDEX IF NOT EXISTS ix_attempts_question ON attempts(question_id);

-- See 2. above. An attempt is evidence; evidence that can be edited is not
-- evidence. Corrections are made by recording a new attempt, never by
-- rewriting an old one.
CREATE TRIGGER IF NOT EXISTS attempts_are_immutable_update
BEFORE UPDATE ON attempts
BEGIN
    SELECT RAISE(ABORT, 'attempts are immutable: record a new attempt instead of updating one');
END;

CREATE TRIGGER IF NOT EXISTS attempts_are_immutable_delete
BEFORE DELETE ON attempts
BEGIN
    SELECT RAISE(ABORT, 'attempts are immutable: they are the evidence base for every colour and priority');
END;

CREATE TABLE IF NOT EXISTS knowledge_gaps (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    label         TEXT NOT NULL,
    normalized    TEXT NOT NULL,
    concept_id    TEXT REFERENCES concepts(id) ON DELETE SET NULL,
    colour        TEXT NOT NULL DEFAULT 'RED' CHECK (colour IN ('RED','ORANGE','GREEN')),
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    resolved_at   TEXT,
    UNIQUE (user_id, normalized)
);
CREATE INDEX IF NOT EXISTS ix_gaps_user ON knowledge_gaps(user_id, resolved_at);

CREATE TABLE IF NOT EXISTS gap_links (
    gap_id      TEXT NOT NULL REFERENCES knowledge_gaps(id) ON DELETE CASCADE,
    question_id TEXT REFERENCES questions(id) ON DELETE CASCADE,
    attempt_id  TEXT REFERENCES attempts(id) ON DELETE CASCADE,
    notebook_id TEXT REFERENCES notebooks(id) ON DELETE CASCADE,
    source_id   TEXT REFERENCES sources(id) ON DELETE SET NULL,
    chunk_id    TEXT REFERENCES source_chunks(id) ON DELETE SET NULL,
    PRIMARY KEY (gap_id, attempt_id)
);

CREATE TABLE IF NOT EXISTS revision_state (
    id                  TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    question_id         TEXT NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    ease_factor         REAL NOT NULL DEFAULT 2.5,
    interval_days       REAL NOT NULL DEFAULT 0,
    due_at              TEXT NOT NULL,
    last_reviewed_at    TEXT,
    last_result         TEXT,
    consecutive_correct INTEGER NOT NULL DEFAULT 0,
    UNIQUE (user_id, question_id)
);
CREATE INDEX IF NOT EXISTS ix_revstate_due ON revision_state(user_id, due_at);

-- Per-concept rollup. Derived from attempts, but stored so priority ranking
-- does not rescan the whole attempt history on every request.
CREATE TABLE IF NOT EXISTS concept_state (
    user_id             TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    concept_id          TEXT NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
    colour              TEXT NOT NULL DEFAULT 'ORANGE' CHECK (colour IN ('RED','ORANGE','GREEN')),
    correct_count       INTEGER NOT NULL DEFAULT 0,
    wrong_count         INTEGER NOT NULL DEFAULT 0,
    consecutive_correct INTEGER NOT NULL DEFAULT 0,
    last_seen_at        TEXT,
    PRIMARY KEY (user_id, concept_id)
);

-- ---------------------------------------------------------------------------
-- Notifications
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS notification_prefs (
    user_id           TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    trigger_time      TEXT NOT NULL DEFAULT '20:00',
    timezone          TEXT NOT NULL DEFAULT 'UTC',
    push_enabled      INTEGER NOT NULL DEFAULT 1,
    email_enabled     INTEGER NOT NULL DEFAULT 0,
    note_text         TEXT NOT NULL DEFAULT '',
    last_status       TEXT NOT NULL DEFAULT '',
    last_sent_at      TEXT,
    next_scheduled_at TEXT
);

CREATE TABLE IF NOT EXISTS notification_log (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    scheduled_at TEXT NOT NULL,
    sent_at      TEXT,
    channel      TEXT NOT NULL,
    status       TEXT NOT NULL,
    detail       TEXT NOT NULL DEFAULT '',
    due_count    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_notiflog_user ON notification_log(user_id, scheduled_at);

-- ---------------------------------------------------------------------------
-- Production AI deployment (the benchmark -> product boundary)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS production_deployments (
    id                  TEXT PRIMARY KEY,
    task_type           TEXT NOT NULL,
    candidate_id        TEXT NOT NULL,
    benchmark_run_id    TEXT NOT NULL,
    outcome             TEXT NOT NULL,
    activated_at        TEXT NOT NULL,
    activated_by        TEXT NOT NULL DEFAULT '',
    signoff_name        TEXT NOT NULL DEFAULT '',
    signoff_rationale   TEXT NOT NULL DEFAULT '',
    deactivated_at      TEXT,
    -- Standing a deployment down is a decision too, and needs an owner.
    deactivated_by      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_deploy_task ON production_deployments(task_type, deactivated_at);
