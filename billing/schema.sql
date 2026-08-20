-- Quintek billing and usage.
--
-- Three rules the shapes here enforce, because prose cannot:
--
--   1. Money is integer minor units (paise). There is no REAL column holding
--      a price anywhere in this file. Floating-point money accumulates error
--      and the error lands in someone's invoice.
--
--   2. The usage ledger is append-only, guarded by triggers. A usage record
--      that can be edited makes "how much did this user actually consume"
--      unanswerable, and that number is the basis of both the entitlement
--      check and the economics dashboard.
--
--   3. Allowances live in `plans`, never in code. The launch numbers are
--      configuration, not promises: the benchmark will eventually recalibrate
--      them, and that must not need a client release.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- Plans -- the source of truth for every allowance
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS plans (
    id                          TEXT PRIMARY KEY,
    family                      TEXT NOT NULL,        -- free | student | pro | power
    name                        TEXT NOT NULL,
    billing_interval            TEXT NOT NULL CHECK (billing_interval IN ('none','monthly','annual')),
    -- Minor units. 499 rupees is 49900.
    price_minor                 INTEGER NOT NULL DEFAULT 0,
    currency                    TEXT NOT NULL DEFAULT 'INR',
    monthly_question_allowance  INTEGER NOT NULL,
    daily_question_limit        INTEGER NOT NULL,
    session_question_limit      INTEGER NOT NULL,
    -- Rollover ceiling as a percentage of the monthly allowance, so the rule
    -- "at most 50% of next month's normal allowance" is data, not a constant
    -- buried in a renewal function.
    rollover_percent            INTEGER NOT NULL DEFAULT 0,
    -- A plan is versioned rather than edited. A subscription points at the
    -- exact version it was sold under, so changing the launch numbers never
    -- silently rewrites what an existing customer bought.
    version                     INTEGER NOT NULL DEFAULT 1,
    active                      INTEGER NOT NULL DEFAULT 1,
    sort_order                  INTEGER NOT NULL DEFAULT 0,
    created_at                  TEXT NOT NULL,
    updated_at                  TEXT NOT NULL,
    UNIQUE (family, billing_interval, version)
);
CREATE INDEX IF NOT EXISTS ix_plans_active ON plans(active, family);

-- ---------------------------------------------------------------------------
-- Subscriptions
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS subscriptions (
    id                       TEXT PRIMARY KEY,
    user_id                  TEXT NOT NULL,
    plan_id                  TEXT NOT NULL REFERENCES plans(id),
    billing_interval         TEXT NOT NULL,
    gateway                  TEXT NOT NULL DEFAULT '',
    gateway_customer_id      TEXT NOT NULL DEFAULT '',
    gateway_subscription_id  TEXT NOT NULL DEFAULT '',
    -- Quintek's OWN states, mapped from the gateway's. A gateway vocabulary
    -- that leaks through the application is a gateway you cannot replace.
    status                   TEXT NOT NULL CHECK (status IN (
                                 'TRIALING','ACTIVE','PAST_DUE','CANCEL_AT_PERIOD_END',
                                 'CANCELLED','EXPIRED','PAYMENT_FAILED','PENDING')),
    current_period_start     TEXT,
    current_period_end       TEXT,
    cancel_at_period_end     INTEGER NOT NULL DEFAULT 0,
    -- A downgrade takes effect at the next cycle, so the intent is stored
    -- rather than applied.
    scheduled_plan_id        TEXT REFERENCES plans(id),
    scheduled_effective_at   TEXT,
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_subs_user ON subscriptions(user_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS ix_subs_gateway
    ON subscriptions(gateway, gateway_subscription_id)
    WHERE gateway_subscription_id != '';

-- ---------------------------------------------------------------------------
-- Entitlements -- what the user has RIGHT NOW
-- ---------------------------------------------------------------------------
-- Separate from the plan because entitlement can diverge from it: a rollover
-- balance, a promotion, a future top-up. `source` exists from day one so that
-- adding top-ups later does not require rebuilding this table.
CREATE TABLE IF NOT EXISTS entitlements (
    id                  TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL,
    subscription_id     TEXT REFERENCES subscriptions(id),
    plan_id             TEXT NOT NULL REFERENCES plans(id),
    source              TEXT NOT NULL DEFAULT 'subscription'
                        CHECK (source IN ('subscription','topup','addon','promotion')),
    monthly_allowance   INTEGER NOT NULL,
    daily_limit         INTEGER NOT NULL,
    session_limit       INTEGER NOT NULL,
    rollover_balance    INTEGER NOT NULL DEFAULT 0,
    effective_from      TEXT NOT NULL,
    effective_until     TEXT,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ent_user ON entitlements(user_id, effective_from);

-- ---------------------------------------------------------------------------
-- Reservations -- the concurrency guard
-- ---------------------------------------------------------------------------
-- Two simultaneous 200-question requests against a 300 remaining balance must
-- not both succeed. Capacity is RESERVED before any generation starts, and the
-- reservation is what later requests see. Committing writes the real usage;
-- releasing returns unused capacity.
CREATE TABLE IF NOT EXISTS reservations (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    batch_id        TEXT NOT NULL DEFAULT '',
    question_units  INTEGER NOT NULL,
    compute_units   INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL CHECK (status IN ('HELD','COMMITTED','RELEASED','EXPIRED')),
    usage_date      TEXT NOT NULL,          -- the day this counts against
    period_start    TEXT NOT NULL,          -- the billing period it counts against
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    settled_at      TEXT
);
CREATE INDEX IF NOT EXISTS ix_res_user_open ON reservations(user_id, status, usage_date);
CREATE INDEX IF NOT EXISTS ix_res_batch ON reservations(batch_id);

-- ---------------------------------------------------------------------------
-- Usage ledger -- append only
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS usage_ledger (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    batch_id        TEXT NOT NULL DEFAULT '',
    operation_id    TEXT NOT NULL DEFAULT '',
    reservation_id  TEXT REFERENCES reservations(id),
    question_units  INTEGER NOT NULL DEFAULT 0,
    compute_units   INTEGER NOT NULL DEFAULT 0,
    question_type   TEXT NOT NULL DEFAULT '',
    usage_date      TEXT NOT NULL,
    period_start    TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_usage_user_date ON usage_ledger(user_id, usage_date);
CREATE INDEX IF NOT EXISTS ix_usage_user_period ON usage_ledger(user_id, period_start);

CREATE TRIGGER IF NOT EXISTS usage_ledger_no_update
BEFORE UPDATE ON usage_ledger
BEGIN
    SELECT RAISE(ABORT, 'usage_ledger is append-only: record a correcting entry instead');
END;
CREATE TRIGGER IF NOT EXISTS usage_ledger_no_delete
BEFORE DELETE ON usage_ledger
BEGIN
    SELECT RAISE(ABORT, 'usage_ledger is append-only: usage records are never deleted');
END;

-- ---------------------------------------------------------------------------
-- Provider cost ledger -- the real economics
-- ---------------------------------------------------------------------------
-- What an operation actually cost, in minor units, next to what it produced.
-- This is what makes "cost per 500 accepted questions" an observation rather
-- than an estimate.
CREATE TABLE IF NOT EXISTS cost_ledger (
    id                   TEXT PRIMARY KEY,
    user_id              TEXT NOT NULL DEFAULT '',
    plan_family          TEXT NOT NULL DEFAULT '',
    batch_id             TEXT NOT NULL DEFAULT '',
    operation            TEXT NOT NULL DEFAULT '',   -- generation | validation | ...
    provider             TEXT NOT NULL,
    model                TEXT NOT NULL,
    input_tokens         INTEGER,
    output_tokens        INTEGER,
    cached_tokens        INTEGER,
    -- Provider-reported price per million tokens, in micro-minor-units so a
    -- fraction of a paise survives without a float.
    price_in_micro       INTEGER,
    price_out_micro      INTEGER,
    -- MICRO minor units (millionths of a paise), not paise.
    --
    -- A single generation call costs a fraction of a paise. Storing that as
    -- an integer number of paise forces a round per row, and rounding up per
    -- row inflated a measured 10,000-call total from Rs 30 to Rs 100 -- a
    -- 3.3x over-report of the single line the economics dashboard exists to
    -- get right. Precision is kept here and rounded ONCE at aggregation.
    cost_micro           INTEGER NOT NULL DEFAULT 0,
    currency             TEXT NOT NULL DEFAULT 'INR',
    compute_units        INTEGER NOT NULL DEFAULT 0,
    questions_produced   INTEGER NOT NULL DEFAULT 0,
    questions_accepted   INTEGER NOT NULL DEFAULT 0,
    questions_rejected   INTEGER NOT NULL DEFAULT 0,
    regenerations        INTEGER NOT NULL DEFAULT 0,
    latency_ms           REAL,
    created_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_cost_created ON cost_ledger(created_at);
CREATE INDEX IF NOT EXISTS ix_cost_model ON cost_ledger(provider, model, created_at);
CREATE INDEX IF NOT EXISTS ix_cost_plan ON cost_ledger(plan_family, created_at);

CREATE TRIGGER IF NOT EXISTS cost_ledger_no_delete
BEFORE DELETE ON cost_ledger
BEGIN
    SELECT RAISE(ABORT, 'cost_ledger is append-only');
END;

-- ---------------------------------------------------------------------------
-- Gateway webhook events -- idempotency
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS webhook_events (
    id                 TEXT PRIMARY KEY,
    gateway            TEXT NOT NULL,
    gateway_event_id   TEXT NOT NULL,
    event_type         TEXT NOT NULL,
    payload            TEXT NOT NULL,
    signature_valid    INTEGER NOT NULL DEFAULT 0,
    received_at        TEXT NOT NULL,
    processed_at       TEXT,
    processing_status  TEXT NOT NULL DEFAULT 'RECEIVED'
                       CHECK (processing_status IN ('RECEIVED','PROCESSED','FAILED','IGNORED')),
    error              TEXT,
    -- The uniqueness that makes replay harmless. A gateway retrying a webhook
    -- must not grant a second month of access.
    UNIQUE (gateway, gateway_event_id)
);
CREATE INDEX IF NOT EXISTS ix_webhook_status ON webhook_events(processing_status, received_at);

-- ---------------------------------------------------------------------------
-- Compute unit weights -- configuration, not economics
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS compute_unit_weights (
    question_type  TEXT PRIMARY KEY,
    weight         INTEGER NOT NULL,
    note           TEXT NOT NULL DEFAULT '',
    updated_at     TEXT NOT NULL
);
