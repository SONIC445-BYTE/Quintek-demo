"""
ADR-029: erasing a learner who has answered questions, by SEVERING rather
than deleting their attempts.

Verified the way erasure has to be verified on this project: run it, then READ
THE DATABASE. Every text column of every table is searched for everything
that identified the person -- their address, their id, and every piece of
text they typed or uploaded -- with a positive control proving the search
finds each marker before the erasure. Not by the endpoint's 200.

Both invariants must survive:
  * the learner is gone -- no identifying field, no row pointing at them;
  * the attempts are unchanged evidence -- same rows, same answers, same
    colours -- and the immutability trigger still refuses every edit other
    than the one-way severance.
"""

from __future__ import annotations

import json

import pytest

from persistence import schema as pschema
from student import accounts
from student.api import StudentAPI
from student.db import new_id, now_iso

EMAIL = "forget.me@example.com"
MARKERS = {
    "notebook": "MARK-notebook-title",
    "source": "MARK-uploaded-passage",
    "stem": "MARK-question-stem",
    "option": "MARK-option-text",
    "rationale": "MARK-rationale",
    "validator": "MARK-validator-note",
    "gap_with_attempt": "MARK-gap-typed-with-the-answer",
    "gap_after": "MARK-gap-named-after-the-reveal",
    "reminder": "MARK-reminder-label",
    "report": "MARK-report-note",
    "demo": "MARK-demonstration",
    "name": "MARK-display-name",
}
EVIDENCE = ("id", "question_id", "user_answer", "correct_answer", "is_correct",
            "user_colour", "concepts_tested_json", "created_at")


def everything(db) -> list[tuple[str, dict]]:
    con = db.connect()
    out = []
    for table in sorted(pschema.table_names(con)):
        for row in db.query(f'SELECT * FROM "{table}"'):
            out.append((table, dict(row)))
    return out


def hits(db, needles) -> list[str]:
    found = []
    for table, row in everything(db):
        blob = json.dumps(row, default=str)
        for needle in needles:
            if needle in blob:
                found.append(f"{table}: {needle}")
    return found


def rows_pointing_at(db, uid) -> list[str]:
    return [f"{t}.{k}" for t, row in everything(db) for k, v in row.items() if v == uid]


@pytest.fixture
def world(any_backend):
    db = any_backend.student()
    api = StudentAPI(db)

    def register(email, name=""):
        st, out = api.handle("POST", "/auth/register", {},
                             {"email": email, "password": "correct-horse", "name": name},
                             None)
        assert st in (200, 201), out
        return out["token"], out["user_id"]

    tok, uid = register(EMAIL, MARKERS["name"])
    by_tok, by_uid = register("bystander@example.com")

    def call(method, path, body=None, token=tok):
        st, out = api.handle(method, path, {}, body or {}, token)
        assert st < 300, (method, path, st, out)
        return out

    concept = new_id("cpt")
    db.execute("INSERT INTO concepts (id,canonical_name,normalized_name,subject,first_seen_at)"
               " VALUES (?,?,?,?,?)", (concept, "Renal physiology", "renal physiology",
                                       "renal", now_iso()))

    def learner_material(token, owner, title, marker_stem):
        nid = call("POST", "/notebooks", {"title": title, "subject": "renal"}, token)["id"]
        sid, cid = new_id("src"), new_id("chk")
        db.execute("INSERT INTO sources (id,notebook_id,kind,filename,status,uploaded_at)"
                   " VALUES (?,?,?,?,?,?)", (sid, nid, "text", "notes.txt", "extracted",
                                             now_iso()))
        db.execute("INSERT INTO source_chunks (id,source_id,ordinal,text,locator_json,"
                   "confidence,extraction_method,needs_review,status) VALUES (?,?,?,?,?,?,?,?,?)",
                   (cid, sid, 1, MARKERS["source"] if owner == uid else "bystander passage",
                    json.dumps({"page": 1}), 0.9, "text", 0, "processed"))
        mine = owner == uid
        option = MARKERS["option"] if mine else "bystander option"
        rationale = MARKERS["rationale"] if mine else "bystander rationale"
        note = MARKERS["validator"] if mine else "bystander validator note"
        qids = []
        for i in range(3):
            qid = new_id("q")
            db.execute(
                "INSERT INTO questions (id,primary_notebook_id,family,stem,options_json,"
                "correct_index,rationale,source_id,chunk_id,validation_status,"
                "validation_json,generated_by_candidate_id,prompt_version,generated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (qid, nid, "mcq", f"{marker_stem} {i}",
                 json.dumps([f"{option} A", "B", "C", "D"]), 0,
                 rationale, sid, cid, "approved",
                 json.dumps({"note": note}), "cand", "v1", now_iso()))
            db.execute("INSERT INTO question_concepts (question_id,concept_id,role)"
                       " VALUES (?,?,?)", (qid, concept, "target"))
            qids.append(qid)
        return nid, qids

    nid, qids = learner_material(tok, uid, MARKERS["notebook"], MARKERS["stem"])
    session = call("POST", "/revision/sessions", {"count": 3, "strategy": "unseen"})
    first = call("POST", "/attempts", {"question_id": qids[0], "user_answer": 1,
                                       "user_colour": "RED",
                                       "session_id": session["session_id"],
                                       "gaps": [MARKERS["gap_with_attempt"]]})
    call("POST", f"/attempts/{first['attempt_id']}/gaps", {"gaps": [MARKERS["gap_after"]]})
    call("POST", "/attempts", {"question_id": qids[1], "user_answer": None,
                               "user_colour": "GREEN"})
    call("POST", "/attempts", {"question_id": qids[0], "user_answer": 0,
                               "user_colour": "ORANGE"})
    # qids[2] is never answered: it must be DELETED with the notebook, not kept.
    call("POST", f"/questions/{qids[0]}/reports", {"kind": "other", "note": MARKERS["report"]})
    call("POST", "/reminders", {"label": MARKERS["reminder"], "local_date": "2099-01-01",
                                "local_time": "20:00", "timezone": "Asia/Kolkata"})
    call("POST", "/demos", {"title": MARKERS["demo"], "question": MARKERS["demo"]})

    by_nid, by_qids = learner_material(by_tok, by_uid, "Bystander notebook", "bystander stem")
    call("POST", "/attempts", {"question_id": by_qids[0], "user_answer": 0,
                               "user_colour": "GREEN"}, by_tok)

    w = type("W", (), {})()
    w.api, w.db, w.tok, w.uid, w.qids = api, db, tok, uid, qids
    w.by_tok, w.by_uid, w.by_qids = by_tok, by_uid, by_qids
    return w


def evidence_of(db, where, params):
    return sorted(tuple(dict(r)[k] for k in EVIDENCE)
                  for r in db.query(f"SELECT * FROM attempts WHERE {where}", params))


def test_the_scan_finds_every_marker_before_erasure(world):
    """Positive control. Without it, "nothing found afterwards" could mean
    the scan cannot find anything."""
    needles = [world.uid, EMAIL, *MARKERS.values()]
    found = {h.split(": ", 1)[1] for h in hits(world.db, needles)}
    assert found == set(needles), f"not planted: {set(needles) - found}"


def test_erasure_of_a_used_account_leaves_nothing_that_identifies_them(world):
    before = evidence_of(world.db, "user_id = ?", (world.uid,))
    assert len(before) == 3
    bystander_before = evidence_of(world.db, "user_id = ?", (world.by_uid,))

    st, out = world.api.handle("DELETE", "/account", {}, {}, world.tok)
    assert st == 200, out
    assert out["severed"] == {"attempts": 3, "questions": 2, "question_reports": 1}

    # 1. No identifying text anywhere, and nothing points at the account.
    leftover = hits(world.db, [world.uid, EMAIL, *MARKERS.values()])
    assert leftover == [], f"identifying data survived erasure: {leftover}"
    assert rows_pointing_at(world.db, world.uid) == []

    # 2. The evidence is all still there, unchanged, belonging to nobody.
    after = evidence_of(world.db, "user_id = ?", (accounts.ERASED_USER,))
    assert after == before, "an evidence column changed during severance"
    for r in world.db.query("SELECT * FROM attempts WHERE user_id = ?",
                            (accounts.ERASED_USER,)):
        assert r["session_id"] is None
        assert r["knowledge_gaps_json"] == "[]" and r["source_refs_json"] == "[]"

    # 3. The answered questions are kept as structure only; the unanswered one
    #    went with the notebook.
    kept = {r["id"]: dict(r) for r in world.db.query(
        "SELECT * FROM questions WHERE primary_notebook_id = ?", (accounts.ERASED_NOTEBOOK,))}
    assert set(kept) == {world.qids[0], world.qids[1]}
    for q in kept.values():
        assert q["stem"] == "" and q["rationale"] == "" and q["validation_json"] == "{}"
        assert json.loads(q["options_json"]) == ["", "", "", ""]
        assert q["source_id"] is None and q["chunk_id"] is None
        assert q["correct_index"] == 0 and q["validation_status"] == "approved"
    assert world.db.query_one("SELECT id FROM questions WHERE id = ?", (world.qids[2],)) is None
    concepts = {r["question_id"] for r in world.db.query("SELECT question_id FROM question_concepts")}
    assert {world.qids[0], world.qids[1]} <= concepts, "the kept questions lost their concept tags"

    # 4. The retained report keeps what it said about the QUESTION, nothing more.
    reports = [dict(r) for r in world.db.query("SELECT * FROM question_reports")]
    assert [(r["user_id"], r["kind"], r["note"], r["provenance_json"]) for r in reports] == [
        (accounts.ERASED_USER, "other", "", "{}")]

    # 5. The bystander is untouched.
    assert evidence_of(world.db, "user_id = ?", (world.by_uid,)) == bystander_before
    st, _ = world.api.handle("GET", "/notebooks", {}, {}, world.by_tok)
    assert st == 200


def test_the_placeholder_is_not_a_person_and_not_an_account(world):
    world.api.handle("DELETE", "/account", {}, {}, world.tok)
    row = world.db.query_one("SELECT * FROM users WHERE id = ?", (accounts.ERASED_USER,))
    assert "@" not in row["email"] and row["name"] == ""
    for guess in ("", "correct-horse", "!unusable"):
        st, _ = world.api.handle("POST", "/auth/login", {},
                                 {"email": row["email"], "password": guess}, None)
        assert st in (400, 401)
    st, _ = world.api.handle("POST", "/auth/register", {},
                             {"email": row["email"], "password": "correct-horse"}, None)
    assert st >= 400, "the placeholder's address could be registered"
    with pytest.raises(accounts.AccountError, match="placeholder"):
        accounts.erase(world.db, accounts.ERASED_USER, confirm=accounts.ERASED_USER)


def test_two_erased_learners_share_one_placeholder(world):
    """Not one pseudonym per person: severed rows cannot be regrouped into a
    single anonymous learner's history."""
    world.api.handle("DELETE", "/account", {}, {}, world.tok)
    world.api.handle("DELETE", "/account", {}, {}, world.by_tok)
    owners = {r["user_id"] for r in world.db.query("SELECT user_id FROM attempts")}
    assert owners == {accounts.ERASED_USER}
    assert world.db.query_one("SELECT COUNT(*) n FROM users WHERE id = ?",
                              (accounts.ERASED_USER,))["n"] == 1


# ---------------------------------------------------------------------------
# The immutability invariant survives: only the one-way severance is allowed
# ---------------------------------------------------------------------------

def refused(db, sql, params=()):
    try:
        db.execute(sql, params)
    except Exception as exc:          # sqlite3.IntegrityError / psycopg RaiseException
        assert "immutable" in str(exc), exc
        # A failed statement leaves a Postgres transaction aborted; start clean.
        try:
            db.connect().rollback()
        except Exception:
            pass
        return True
    return False


@pytest.mark.parametrize("sql, why", [
    ("UPDATE attempts SET user_colour = 'GREEN' WHERE user_id = ?", "an edit to a live row"),
    ("UPDATE attempts SET user_id = 'usr_erased', session_id = NULL,"
     " knowledge_gaps_json = '[]', source_refs_json = '[]', user_colour = 'RED'"
     " WHERE user_id = ?", "a severance that also changes the colour"),
    ("UPDATE attempts SET user_id = 'usr_erased', session_id = NULL,"
     " knowledge_gaps_json = '[]', source_refs_json = '[]', is_correct = 1 - is_correct"
     " WHERE user_id = ?", "a severance that also changes correctness"),
    ("UPDATE attempts SET user_id = 'usr_erased' WHERE user_id = ?",
     "a severance that leaves the typed gaps behind"),
    ("DELETE FROM attempts WHERE user_id = ?", "a delete"),
])
def test_the_trigger_refuses_everything_but_a_clean_severance(world, sql, why):
    # The placeholder exists, so a foreign key cannot be what refuses: only
    # the trigger can (refused() also requires its message).
    accounts._ensure_placeholders(world.db)
    before = evidence_of(world.db, "user_id = ?", (world.by_uid,))
    assert refused(world.db, sql, (world.by_uid,)), f"the trigger allowed {why}"
    assert evidence_of(world.db, "user_id = ?", (world.by_uid,)) == before


def test_a_clean_severance_is_the_one_update_allowed(world):
    accounts._ensure_placeholders(world.db)
    before = evidence_of(world.db, "user_id = ?", (world.by_uid,))
    world.db.execute("UPDATE attempts SET user_id = 'usr_erased', session_id = NULL,"
                     " knowledge_gaps_json = '[]', source_refs_json = '[]'"
                     " WHERE user_id = ?", (world.by_uid,))
    assert evidence_of(world.db, "user_id = 'usr_erased'", ()) == before


def test_a_severed_attempt_cannot_be_moved_again_or_back(world):
    world.api.handle("DELETE", "/account", {}, {}, world.tok)
    for sql in ("UPDATE attempts SET user_id = ? WHERE user_id = 'usr_erased'",
                "UPDATE attempts SET user_colour = 'RED' WHERE user_id = 'usr_erased' AND ? = ?"):
        params = (world.by_uid,) if sql.count("?") == 1 else (1, 1)
        assert refused(world.db, sql, params)
    assert refused(world.db, "DELETE FROM attempts WHERE user_id = 'usr_erased'")


def test_the_placeholder_id_in_the_trigger_is_the_one_the_code_uses():
    from pathlib import Path
    sql = Path("student/schema.sql").read_text()
    trigger = sql[sql.index("CREATE TRIGGER IF NOT EXISTS attempts_are_immutable_update"):]
    trigger = trigger[:trigger.index("END;")]
    assert trigger.count(f"'{accounts.ERASED_USER}'") == 2


def test_filling_in_a_blank_answer_during_severance_is_refused(world):
    """The learner's GREEN attempt has user_answer NULL. With `NOT (...)`
    instead of `(...) IS NOT TRUE`, the NULL comparison makes the whole
    condition NULL, the trigger does not fire, and the answer is rewritten."""
    accounts._ensure_placeholders(world.db)
    assert world.db.query_one("SELECT COUNT(*) n FROM attempts WHERE user_id = ?"
                              " AND user_answer IS NULL", (world.uid,))["n"] == 1
    assert refused(world.db,
                   "UPDATE attempts SET user_id = 'usr_erased', session_id = NULL,"
                   " knowledge_gaps_json = '[]', source_refs_json = '[]', user_answer = 3"
                   " WHERE user_id = ? AND user_answer IS NULL", (world.uid,))


def test_an_existing_database_gets_the_new_trigger(tmp_path):
    """`CREATE TRIGGER IF NOT EXISTS` keeps an old trigger. A SQLite database
    made before the change must still end up with the severance rule, or
    erasure keeps failing there exactly as before."""
    import persistence
    from persistence.schema import forget_all
    from student.db import Database
    path = tmp_path / "old.db"
    db = Database(path)
    db.execute("DROP TRIGGER attempts_are_immutable_update")
    db.execute("CREATE TRIGGER attempts_are_immutable_update BEFORE UPDATE ON attempts"
               " BEGIN SELECT RAISE(ABORT, 'attempts are immutable: old rule'); END")
    db.connect().close()
    forget_all()
    reopened = Database(path)
    sql = reopened.query_one("SELECT sql FROM sqlite_master WHERE name ="
                             " 'attempts_are_immutable_update'")["sql"]
    assert "usr_erased" in sql, "the old, absolute trigger survived a restart"


def test_a_retained_row_that_kept_the_id_fails_the_erasure(world, monkeypatch):
    """The final check covers the retained tables too: an anonymisation that
    silently did nothing must make erase() refuse, not report success."""
    world.db.execute("INSERT INTO spend_log (id, user_id, operation, units, note, created_at)"
                     " VALUES (?,?,?,?,?,?)", (new_id("sp"), world.uid, "generation", 1, "",
                                               now_iso()))
    real = world.db.execute

    def skip_spend_anonymisation(sql, params=()):
        if sql.startswith('UPDATE "spend_log"'):
            class Nothing:
                rowcount = 0
            return Nothing()
        return real(sql, params)

    monkeypatch.setattr(world.db, "execute", skip_spend_anonymisation)
    with pytest.raises(accounts.AccountError, match="erasure incomplete"):
        accounts.erase(world.db, world.uid, confirm=world.uid)


def test_a_severance_that_keeps_the_typed_gap_labels_is_refused(world):
    """Everything else about it is a clean severance: session cleared, upload
    pointers cleared, evidence untouched. Only the words the learner typed
    are left in place -- and that alone must be enough to refuse it."""
    accounts._ensure_placeholders(world.db)
    assert world.db.query_one("SELECT COUNT(*) n FROM attempts WHERE user_id = ?"
                              " AND knowledge_gaps_json <> '[]'", (world.uid,))["n"] == 1
    assert refused(world.db,
                   "UPDATE attempts SET user_id = 'usr_erased', session_id = NULL,"
                   " source_refs_json = '[]'"
                   " WHERE user_id = ? AND knowledge_gaps_json <> '[]'", (world.uid,))
