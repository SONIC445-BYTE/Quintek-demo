"""
One regression test per incompatibility the PostgreSQL audit found.

Every test here was written to FAIL against the pre-port code, and each one
says in its docstring what the failure was. That is the point of the file: the
four defects it covers were all invisible to a 1353-test suite because every
one of those tests drove SQLite, and SQLite forgave what Postgres does not.

Three of the four were SILENT -- the DDL loaded without error and the failure
waited for live data. A test that only proves the schema loads would have
passed on all three.

The Postgres half SKIPS without QUINTEK_TEST_POSTGRES_URL. A skip is not a
pass, and the summary says so.
"""

from __future__ import annotations

import threading

import pytest

from persistence import dialect

# ---------------------------------------------------------------------------
# FINDING 1 -- a nullable column inside a composite PRIMARY KEY
# ---------------------------------------------------------------------------
# SQLite permits NULL in a PRIMARY KEY column (a documented legacy quirk) and
# then treats two such rows as distinct, so `INSERT OR IGNORE` silently failed
# to deduplicate. PostgreSQL promotes the column to NOT NULL when the table is
# created -- WITHOUT an error -- and then rejects the insert on live data.


def test_source_concepts_accepts_a_link_with_no_chunk(any_backend):
    """
    The application's own call: `link_to_source(..., chunk_id=None)`.

    Fails on the pre-port schema under Postgres with
    `null value in column "chunk_id" violates not-null constraint`.
    """
    db = any_backend.student()
    _seed_source_and_concept(db)
    from student.concepts import ConceptStore
    ConceptStore(db).link_to_source("s1", "c1", None)

    rows = db.query("SELECT * FROM source_concepts WHERE source_id = ?", ("s1",))
    assert len(rows) == 1
    assert rows[0]["chunk_id"] is None


def test_a_chunkless_link_is_recorded_once_not_twice(any_backend):
    """
    The half of finding 1 that was a live SQLite bug, not just a Postgres one.

    `INSERT OR IGNORE` did not deduplicate rows whose NULL was part of the key,
    so re-ingesting a source grew the table. Fails on the pre-port schema under
    SQLITE, with 2 rows.
    """
    db = any_backend.student()
    _seed_source_and_concept(db)
    from student.concepts import ConceptStore
    store = ConceptStore(db)
    store.link_to_source("s1", "c1", None)
    store.link_to_source("s1", "c1", None)

    rows = db.query("SELECT * FROM source_concepts WHERE source_id = ?", ("s1",))
    assert len(rows) == 1, "a chunkless link was recorded twice"


def test_a_link_naming_a_chunk_is_still_distinct_from_one_without(any_backend):
    """The dedupe must not go too far: these are two different facts."""
    db = any_backend.student()
    _seed_source_and_concept(db)
    db.execute("INSERT INTO source_chunks (id, source_id, ordinal, text)"
               " VALUES (?,?,?,?)", ("ch1", "s1", 1, "text"))
    from student.concepts import ConceptStore
    store = ConceptStore(db)
    store.link_to_source("s1", "c1", None)
    store.link_to_source("s1", "c1", "ch1")

    assert len(db.query("SELECT * FROM source_concepts WHERE source_id = ?", ("s1",))) == 2


def test_deleting_a_chunk_nulls_the_link_rather_than_failing(any_backend):
    """
    The second, non-INSERT half of finding 1.

    `ON DELETE SET NULL` onto a PRIMARY KEY column fails at DELETE time on
    Postgres, because the column it must set to NULL is NOT NULL.
    """
    db = any_backend.student()
    _seed_source_and_concept(db)
    db.execute("INSERT INTO source_chunks (id, source_id, ordinal, text)"
               " VALUES (?,?,?,?)", ("ch1", "s1", 1, "text"))
    from student.concepts import ConceptStore
    ConceptStore(db).link_to_source("s1", "c1", "ch1")

    db.execute("DELETE FROM source_chunks WHERE id = ?", ("ch1",))

    rows = db.query("SELECT * FROM source_concepts WHERE source_id = ?", ("s1",))
    assert len(rows) == 1 and rows[0]["chunk_id"] is None


# ---------------------------------------------------------------------------
# FINDING 3 -- a bare column in GROUP BY
# ---------------------------------------------------------------------------


def test_concept_priority_rows_are_computable(any_backend):
    """
    `PriorityEngine._signals` selects concept_state columns while grouping only
    by `c.id`. Postgres rejects it outright:
    `column "cs.colour" must appear in the GROUP BY clause`.
    """
    db = any_backend.student()
    uid = db.create_user("p@example.com", "password123")
    _seed_source_and_concept(db, owner=uid)
    db.execute("INSERT INTO notebook_concepts (notebook_id, concept_id, role)"
               " VALUES (?,?,?)", ("n1", "c1", "primary"))

    from student.revision import PriorityEngine
    rows = PriorityEngine(db)._signals(uid)

    assert len(rows) == 1
    assert rows[0]["concept_id"] == "c1"
    assert rows[0]["colour"] == "ORANGE"       # the COALESCE default
    assert rows[0]["correct_count"] == 0


def test_concept_priority_reads_the_learners_own_state(any_backend):
    """Grouping must not lose the joined row -- the values have to be real."""
    db = any_backend.student()
    uid = db.create_user("p2@example.com", "password123")
    _seed_source_and_concept(db, owner=uid)
    db.execute("INSERT INTO notebook_concepts (notebook_id, concept_id, role)"
               " VALUES (?,?,?)", ("n1", "c1", "primary"))
    db.execute("INSERT INTO concept_state (user_id, concept_id, colour,"
               " correct_count, wrong_count) VALUES (?,?,?,?,?)",
               (uid, "c1", "RED", 3, 7))

    from student.revision import PriorityEngine
    row = PriorityEngine(db)._signals(uid)[0]
    assert (row["colour"], row["correct_count"], row["wrong_count"]) == ("RED", 3, 7)


# ---------------------------------------------------------------------------
# FINDING 4 -- 32-bit integer overflow on micro-unit money
# ---------------------------------------------------------------------------


def test_a_real_configured_price_can_be_recorded(any_backend):
    """
    USD 0.30 per million output tokens -- a model in configs/model_prices.json
    today -- is 0.30 * 8500 * 1_000_000 = 2,550,000,000 micro-paise, past the
    2,147,483,647 ceiling of a 32-bit INTEGER.

    Fails on the pre-port schema under Postgres: `integer out of range`.
    """
    conn = any_backend.billing()
    from billing.costs import CostLedger, ModelPrice, OperationCost

    price = ModelPrice.from_usd_per_million("nvidia", "big", 0.30, 0.30,
                                            usd_to_inr_paise=8500)
    assert price.output_per_million_micro == 2_550_000_000

    ledger = CostLedger(conn)
    ledger.set_price(price)
    ledger.record(OperationCost(provider="nvidia", model="big",
                                input_tokens=1000, output_tokens=1000))

    row = conn.execute("SELECT price_out_micro FROM cost_ledger").fetchone()
    assert row["price_out_micro"] == 2_550_000_000


def test_configured_prices_do_not_silently_exceed_the_column(any_backend):
    """Every price the repository ships must be storable, not just the one above."""
    import json
    from pathlib import Path
    config = json.loads(Path("configs/model_prices.json").read_text(encoding="utf-8"))
    rate = config["usd_to_inr_paise"]
    biggest = max(int(round(p[k] * rate * 1_000_000))
                  for p in config["prices"]
                  for k in ("usd_in_per_million", "usd_out_per_million"))

    conn = any_backend.billing()
    conn.execute("INSERT INTO cost_ledger (id, provider, model, price_in_micro,"
                 " price_out_micro, cost_micro, created_at) VALUES (?,?,?,?,?,?,?)",
                 ("c1", "p", "m", biggest, biggest, biggest, "2026-01-01T00:00:00Z"))
    conn.commit()
    assert conn.execute("SELECT price_in_micro FROM cost_ledger").fetchone()[
        "price_in_micro"] == biggest


# ---------------------------------------------------------------------------
# Immutability -- the triggers must still refuse, in the same words
# ---------------------------------------------------------------------------


def test_attempts_cannot_be_updated_on_either_backend(any_backend):
    """
    The trigger's refusal arrives as sqlite3.IntegrityError on one backend and
    SQLSTATE P0001 on the other -- deliberately NOT unified, because a
    trigger's refusal is not an integrity violation. The MESSAGE is what both
    preserve, and the message is what evidence depends on.
    """
    db = any_backend.student()
    uid = _seed_attempt(db)
    with pytest.raises(Exception, match="attempts are immutable"):
        db.execute("UPDATE attempts SET is_correct = 0 WHERE user_id = ?", (uid,))


def test_attempts_cannot_be_deleted_on_either_backend(any_backend):
    db = any_backend.student()
    uid = _seed_attempt(db)
    with pytest.raises(Exception, match="attempts are immutable"):
        db.execute("DELETE FROM attempts WHERE user_id = ?", (uid,))
    # And the evidence is still there afterwards.
    assert db.query_one("SELECT COUNT(*) AS n FROM attempts WHERE user_id = ?",
                        (uid,))["n"] == 1


def test_usage_ledger_stays_append_only_on_either_backend(any_backend):
    conn = any_backend.billing()
    conn.execute("INSERT INTO usage_ledger (id, user_id, question_units, usage_date,"
                 " period_start, created_at) VALUES (?,?,?,?,?,?)",
                 ("u1", "usr", 5, "2026-01-01", "2026-01-01", "2026-01-01T00:00:00Z"))
    conn.commit()
    with pytest.raises(Exception, match="append-only"):
        conn.execute("UPDATE usage_ledger SET question_units = 1 WHERE id = ?", ("u1",))
    conn.rollback()
    with pytest.raises(Exception, match="append-only"):
        conn.execute("DELETE FROM usage_ledger WHERE id = ?", ("u1",))


# ---------------------------------------------------------------------------
# Foreign keys -- SQLite needs a PRAGMA for these; Postgres always enforces
# ---------------------------------------------------------------------------


def test_foreign_keys_are_enforced_on_either_backend(any_backend):
    db = any_backend.student()
    with pytest.raises(Exception):
        db.execute("INSERT INTO notebooks (id, owner_id, title, created_at)"
                   " VALUES (?,?,?,?)", ("n9", "nobody", "t", "2026-01-01T00:00:00Z"))


def test_cascade_delete_works_on_either_backend(any_backend):
    db = any_backend.student()
    uid = db.create_user("cascade@example.com", "password123")
    db.execute("INSERT INTO notebooks (id, owner_id, title, created_at)"
               " VALUES (?,?,?,?)", ("n1", uid, "t", "2026-01-01T00:00:00Z"))
    db.execute("DELETE FROM users WHERE id = ?", (uid,))
    assert db.query("SELECT * FROM notebooks WHERE id = ?", ("n1",)) == []


# ---------------------------------------------------------------------------
# The dialect translator itself
# ---------------------------------------------------------------------------


def test_placeholders_inside_string_literals_are_data_not_placeholders():
    assert dialect.rewrite_placeholders("SELECT 'what? no', ?") == "SELECT 'what? no', %s"


def test_a_literal_percent_is_doubled_even_inside_quotes():
    """psycopg's placeholder parser is not quote-aware. Established by running it."""
    assert dialect.rewrite_placeholders("SELECT ? , 'x%'") == "SELECT %s , 'x%%'"


def test_insert_or_ignore_becomes_on_conflict_do_nothing():
    out = dialect.translate_statement("INSERT OR IGNORE INTO t (a) VALUES (?)")
    assert out == "INSERT INTO t (a) VALUES (%s) ON CONFLICT DO NOTHING"


def test_pragmas_are_dropped_not_translated():
    assert "PRAGMA" not in dialect.schema_to_postgres(
        "PRAGMA foreign_keys = ON;\nCREATE TABLE t (a TEXT);")


def test_a_sqlite_trigger_becomes_a_plpgsql_trigger():
    out = dialect.schema_to_postgres(
        "CREATE TRIGGER IF NOT EXISTS no_edit BEFORE UPDATE ON t\n"
        "BEGIN\n    SELECT RAISE(ABORT, 'nope');\nEND;")
    assert "RAISE EXCEPTION 'nope'" in out
    assert "CREATE TRIGGER no_edit BEFORE UPDATE ON t" in out
    assert "RAISE(ABORT" not in out


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _seed_source_and_concept(db, owner: str | None = None) -> str:
    owner = owner or db.create_user("seed@example.com", "password123")
    db.execute("INSERT INTO notebooks (id, owner_id, title, created_at)"
               " VALUES (?,?,?,?)", ("n1", owner, "Nb", "2026-01-01T00:00:00Z"))
    db.execute("INSERT INTO sources (id, notebook_id, kind, uploaded_at)"
               " VALUES (?,?,?,?)", ("s1", "n1", "pdf", "2026-01-01T00:00:00Z"))
    db.execute("INSERT INTO concepts (id, canonical_name, normalized_name, first_seen_at)"
               " VALUES (?,?,?,?)", ("c1", "Ferritin", "ferritin", "2026-01-01T00:00:00Z"))
    return owner


def _seed_attempt(db) -> str:
    uid = _seed_source_and_concept(db)
    db.execute("INSERT INTO questions (id, primary_notebook_id, stem, options_json,"
               " correct_index, generated_at) VALUES (?,?,?,?,?,?)",
               ("q1", "n1", "stem", "[]", 0, "2026-01-01T00:00:00Z"))
    db.execute("INSERT INTO attempts (id, question_id, user_id, correct_answer,"
               " is_correct, user_colour, created_at) VALUES (?,?,?,?,?,?,?)",
               ("a1", "q1", uid, 0, 1, "GREEN", "2026-01-01T00:00:00Z"))
    return uid
