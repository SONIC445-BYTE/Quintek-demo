"""
Every inference, recorded, so model selection rests on evidence.

`benchmark/orchestration.py` already appends an `ExecutionRecord` per call.
That was built to answer "what happened in this run". This module answers a
different and harder question:

    How has model X performed over the last 500 clinical questions?

which needs three things the execution log does not carry: the shape of the
task (type and complexity, so like is compared with like), the outcome quality
(did validation accept it), and enough indexing to aggregate hundreds of rows
without reparsing a file. So this is a SQLite ledger rather than a second
JSONL, and it is deliberately append-only.

WHY APPEND-ONLY
---------------
A routing decision made in March was made on the evidence available in March.
If a row can be edited, "why did the router pick that model" becomes
unanswerable, and the scoreboard silently rewrites its own history. Quality
scores arrive later than the inference itself, so `record_outcome` writes to a
SEPARATE row keyed by run_id rather than updating the original -- the same
shape as the attempts-are-immutable rule in the learner schema, for the same
reason.

WHAT IS DELIBERATELY NOT HERE
-----------------------------
No scoring, no ranking, no selection. This module records and queries. A
ledger that also decided things would be a ledger you could not trust to tell
you what it decided.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS inference_run (
    run_id              TEXT PRIMARY KEY,
    timestamp           TEXT NOT NULL,
    task_id             TEXT NOT NULL DEFAULT '',
    -- Identity of what served the call. provider and model are separate
    -- because the same model on two providers is two different latency and
    -- reliability stories, and conflating them is how "the model is slow"
    -- gets recorded when the truth is "this host is slow".
    provider            TEXT NOT NULL,
    model               TEXT NOT NULL,
    model_version       TEXT NOT NULL DEFAULT 'unknown',
    -- Shape of the work, so like is compared with like.
    task_type           TEXT NOT NULL DEFAULT '',
    task_complexity     TEXT NOT NULL DEFAULT '',
    -- Cost and speed.
    prompt_tokens       INTEGER,
    output_tokens       INTEGER,
    latency_ms          REAL,
    time_to_first_token REAL,
    cost_usd            REAL,
    -- Outcome of the CALL, not of the answer.
    success             INTEGER NOT NULL DEFAULT 0,
    timeout             INTEGER NOT NULL DEFAULT 0,
    error               TEXT,
    attempts            INTEGER NOT NULL DEFAULT 1,
    -- Routing provenance.
    routing_mode        TEXT NOT NULL DEFAULT '',   -- production | evaluation | forced
    routing_reason      TEXT NOT NULL DEFAULT '',
    fallback_used       INTEGER NOT NULL DEFAULT 0,
    fallback_from       TEXT,
    -- Structured-output conformance: did the reply parse into the shape the
    -- caller asked for? A model that is right but unparseable is unusable,
    -- and that is a different failure from being wrong.
    structured_ok       INTEGER
);
CREATE INDEX IF NOT EXISTS ix_inf_model ON inference_run(provider, model, timestamp);
CREATE INDEX IF NOT EXISTS ix_inf_task ON inference_run(task_type, timestamp);
CREATE INDEX IF NOT EXISTS ix_inf_time ON inference_run(timestamp);

-- Quality arrives later than the inference. It is a separate row rather than
-- an UPDATE, so the inference record itself is never rewritten.
CREATE TABLE IF NOT EXISTS inference_outcome (
    outcome_id       TEXT PRIMARY KEY,
    run_id           TEXT NOT NULL REFERENCES inference_run(run_id),
    recorded_at      TEXT NOT NULL,
    quality_score    REAL,
    validation_score REAL,
    accepted         INTEGER,
    judged_by        TEXT NOT NULL DEFAULT '',
    notes            TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_outcome_run ON inference_outcome(run_id);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_run_id() -> str:
    return f"inf_{uuid.uuid4().hex[:16]}"


@dataclass
class InferenceRecord:
    provider: str
    model: str
    run_id: str = field(default_factory=new_run_id)
    timestamp: str = field(default_factory=now_iso)
    task_id: str = ""
    model_version: str = "unknown"
    task_type: str = ""
    task_complexity: str = ""
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: float | None = None
    time_to_first_token: float | None = None
    cost_usd: float | None = None
    success: bool = False
    timeout: bool = False
    error: str | None = None
    attempts: int = 1
    routing_mode: str = ""
    routing_reason: str = ""
    fallback_used: bool = False
    fallback_from: str | None = None
    structured_ok: bool | None = None

    @property
    def candidate_key(self) -> str:
        """`provider:model` -- the unit performance is attributed to."""
        return f"{self.provider}:{self.model}"


class InferenceLog:
    """
    Append-only ledger over SQLite. Thread-local connections, matching
    `student/db.py`, because the batch workers below are threads.
    """

    def __init__(self, path: str | Path = "inference.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._connect().executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 30000")
            self._local.conn = conn
        return conn

    # ---------- writing ----------

    def record(self, record: InferenceRecord) -> str:
        conn = self._connect()
        conn.execute(
            "INSERT INTO inference_run (run_id, timestamp, task_id, provider, model,"
            " model_version, task_type, task_complexity, prompt_tokens, output_tokens,"
            " latency_ms, time_to_first_token, cost_usd, success, timeout, error, attempts,"
            " routing_mode, routing_reason, fallback_used, fallback_from, structured_ok)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (record.run_id, record.timestamp, record.task_id, record.provider, record.model,
             record.model_version, record.task_type, record.task_complexity,
             record.prompt_tokens, record.output_tokens, record.latency_ms,
             record.time_to_first_token, record.cost_usd, int(record.success),
             int(record.timeout), record.error, record.attempts, record.routing_mode,
             record.routing_reason, int(record.fallback_used), record.fallback_from,
             None if record.structured_ok is None else int(record.structured_ok)))
        conn.commit()
        return record.run_id

    def record_outcome(self, run_id: str, *, quality_score: float | None = None,
                       validation_score: float | None = None, accepted: bool | None = None,
                       judged_by: str = "", notes: str = "") -> str:
        """
        Attach a later judgement to an inference.

        A new row, never an update. The inference happened; what someone
        later concluded about it is a separate fact with its own timestamp
        and its own author.
        """
        conn = self._connect()
        if conn.execute("SELECT 1 FROM inference_run WHERE run_id = ?", (run_id,)).fetchone() \
                is None:
            raise KeyError(f"no inference {run_id!r} to attach an outcome to")
        outcome_id = f"out_{uuid.uuid4().hex[:16]}"
        conn.execute(
            "INSERT INTO inference_outcome (outcome_id, run_id, recorded_at, quality_score,"
            " validation_score, accepted, judged_by, notes) VALUES (?,?,?,?,?,?,?,?)",
            (outcome_id, run_id, now_iso(), quality_score, validation_score,
             None if accepted is None else int(accepted), judged_by, notes))
        conn.commit()
        return outcome_id

    # ---------- querying ----------

    def runs(self, *, provider: str | None = None, model: str | None = None,
             task_type: str | None = None, since: str | None = None,
             limit: int = 500) -> list[dict]:
        clauses, params = [], []
        if provider:
            clauses.append("provider = ?")
            params.append(provider)
        if model:
            clauses.append("model = ?")
            params.append(model)
        if task_type:
            clauses.append("task_type = ?")
            params.append(task_type)
        if since:
            clauses.append("timestamp >= ?")
            params.append(since)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self._connect().execute(
            f"SELECT * FROM inference_run {where} ORDER BY timestamp DESC, run_id DESC"
            f" LIMIT ?", params).fetchall()
        return [dict(r) for r in rows]

    def with_outcomes(self, **kwargs) -> list[dict]:
        """Runs joined to their latest outcome, for quality aggregation."""
        runs = self.runs(**kwargs)
        if not runs:
            return []
        conn = self._connect()
        ids = [r["run_id"] for r in runs]
        marks = ",".join("?" * len(ids))
        outcomes: dict[str, dict] = {}
        for row in conn.execute(
                f"SELECT * FROM inference_outcome WHERE run_id IN ({marks})"
                " ORDER BY recorded_at ASC", ids):
            outcomes[row["run_id"]] = dict(row)   # last write for a run wins
        for run in runs:
            run["outcome"] = outcomes.get(run["run_id"])
        return runs

    def candidates(self) -> list[str]:
        rows = self._connect().execute(
            "SELECT DISTINCT provider, model FROM inference_run ORDER BY provider, model")
        return [f"{r['provider']}:{r['model']}" for r in rows]

    def count(self, *, provider: str | None = None, model: str | None = None,
              task_type: str | None = None) -> int:
        clauses, params = [], []
        for column, value in (("provider", provider), ("model", model),
                              ("task_type", task_type)):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return self._connect().execute(
            f"SELECT COUNT(*) AS n FROM inference_run {where}", params).fetchone()["n"]

    def coverage_matrix(self) -> dict[str, dict[str, int]]:
        """
        `provider:model` -> task_type -> count.

        The thing that makes "Model A got 80 questions and Model C got 2"
        visible instead of buried.
        """
        matrix: dict[str, dict[str, int]] = {}
        for row in self._connect().execute(
                "SELECT provider, model, task_type, COUNT(*) AS n FROM inference_run"
                " GROUP BY provider, model, task_type"):
            key = f"{row['provider']}:{row['model']}"
            matrix.setdefault(key, {})[row["task_type"] or "(unspecified)"] = row["n"]
        return matrix

    def export_jsonl(self, path: str | Path) -> Path:
        path = Path(path)
        with path.open("w", encoding="utf-8") as handle:
            for run in self.with_outcomes(limit=1_000_000):
                handle.write(json.dumps(run, default=str) + "\n")
        return path
