"""
Finding out something is broken without a user telling you.

WHAT WAS MISSING
----------------
The student engine had a health endpoint that honestly reports what it cannot
do -- verified by execution, it returns `generation: "unavailable"` and
`ai_configured: false` rather than inventing a green tick -- and nothing else.
No record of failures, no ceiling on what an ingestion loop can spend, and no
way to get the data back. Each ends the same way: somebody emails to say it is
broken, or the data is gone.

THE BACKUP TESTS ACTUALLY RESTORE
-----------------------------------
`test_a_backup_survives_a_round_trip` writes rows, backs up, deletes the
original, restores, and reads the rows back. A backup nobody has restored is a
hypothesis, and the file-copy failure mode is specifically invisible: in WAL
mode the `.db` file is not the database, so a copy taken during writes opens
cleanly and is missing the last transactions.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from student import operations as ops
from student.api import StudentAPI
from student.db import Database, new_id, now_iso
from validator.budget import Budget

import tools_backup


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "q.db")


@pytest.fixture
def admin_api(db):
    api = StudentAPI(db)
    token = api.handle("POST", "/auth/register", {},
                       {"email": "ops@example.com", "password": "correct-horse"},
                       None)[1]["token"]
    db.execute("UPDATE users SET role='admin' WHERE email='ops@example.com'")
    return api, token


class TestIncidents:

    def test_a_failure_is_written_down_with_its_context(self, db):
        ops.record(db, operation=ops.INGESTION, error=ValueError("pdf unreadable"),
                   user_id="usr_1", context={"source_id": "src_9"})
        [row] = db.query("SELECT * FROM incidents")
        assert row["operation"] == "ingestion"
        assert row["error_type"] == "ValueError"
        assert json.loads(row["context_json"])["source_id"] == "src_9"

    def test_one_fault_with_many_messages_is_one_group(self, db):
        """
        Grouping on the message would split a single fault into as many groups
        as it has interpolated ids, which is how a hundred instances of one
        problem look like a hundred problems.
        """
        for n in range(5):
            ops.record(db, operation=ops.INGESTION,
                       error=TimeoutError(f"source src_{n} timed out after 30s"))
        report = ops.since(db)
        assert report["total"] == 5
        assert len(report["by_fault"]) == 1
        assert report["by_fault"][0]["count"] == 5

    def test_recording_never_raises(self, db, monkeypatch):
        """
        A monitor that can break the thing it monitors is worse than none. The
        only place in this codebase where swallowing is right.
        """
        def broken(*_a, **_kw):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(db, "execute", broken)
        assert ops.record(db, operation=ops.INGESTION, error=ValueError("x")) == ""

    def test_incidents_cannot_be_deleted(self, db):
        """A table that can be tidied is one whose quiet periods mean nothing."""
        ops.record(db, operation=ops.GENERATION, error=ValueError("x"))
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("DELETE FROM incidents")

    def test_an_alert_fires_only_on_a_repeated_fault(self, db):
        for _ in range(3):
            ops.record(db, operation=ops.GENERATION, error=ValueError("bad json"))
        assert ops.alerts(db, threshold=5) == []
        for _ in range(2):
            ops.record(db, operation=ops.GENERATION, error=ValueError("bad json"))
        [alert] = ops.alerts(db, threshold=5)
        assert alert["count"] == 5

    def test_alerts_are_returned_not_delivered(self):
        """
        Where an alert goes is a deployment decision. A module that picked one
        would be wrong for every deployment that chose another, and delivery
        already belongs to student/notifications.py.
        """
        import ast
        import pathlib
        tree = ast.parse(pathlib.Path("student/operations.py").read_text())
        called = {node.func.attr for node in ast.walk(tree)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        assert not called & {"send", "post", "urlopen", "sendmail", "notify"}


class TestSpendCeilings:

    def test_the_ceiling_uses_the_validator_budget(self, db):
        """
        Reuse, not a second counter. A second counter is a second thing to
        disagree with the first.
        """
        budget = ops.spend_guard(db, user_id="usr_1", max_calls=3)
        assert isinstance(budget, Budget)

    def test_spending_is_refused_once_the_ceiling_is_reached(self, db):
        budget = ops.spend_guard(db, user_id="usr_1", max_calls=2)
        ops.charge(db, user_id="usr_1", operation=ops.GENERATION, budget=budget)
        ops.charge(db, user_id="usr_1", operation=ops.GENERATION, budget=budget)
        with pytest.raises(ops.SpendCeilingReached, match="ceiling of 2"):
            ops.charge(db, user_id="usr_1", operation=ops.GENERATION, budget=budget)

    def test_a_refused_call_is_not_logged_as_spend(self, db):
        """
        A log that records refusals as spend makes the next period's ceiling
        arrive early -- the account is punished twice for one attempt.
        """
        budget = ops.spend_guard(db, user_id="usr_1", max_calls=1)
        ops.charge(db, user_id="usr_1", operation=ops.GENERATION, budget=budget)
        with pytest.raises(ops.SpendCeilingReached):
            ops.charge(db, user_id="usr_1", operation=ops.GENERATION, budget=budget)
        assert db.query_one("SELECT COUNT(*) n FROM spend_log")["n"] == 1

    def test_the_ceiling_is_per_account(self, db):
        budget_a = ops.spend_guard(db, user_id="usr_1", max_calls=1)
        ops.charge(db, user_id="usr_1", operation=ops.GENERATION, budget=budget_a)
        budget_b = ops.spend_guard(db, user_id="usr_2", max_calls=1)
        ops.charge(db, user_id="usr_2", operation=ops.GENERATION, budget=budget_b)
        assert ops.spend_summary(db, user_id="usr_2")["by_operation"]["generation"] == 1

    def test_earlier_spend_in_the_period_counts(self, db):
        """A ceiling that resets on process restart is not a ceiling."""
        b1 = ops.spend_guard(db, user_id="usr_1", max_calls=2)
        ops.charge(db, user_id="usr_1", operation=ops.GENERATION, budget=b1)
        ops.charge(db, user_id="usr_1", operation=ops.GENERATION, budget=b1)
        fresh = ops.spend_guard(db, user_id="usr_1", max_calls=2)
        with pytest.raises(ops.SpendCeilingReached):
            ops.charge(db, user_id="usr_1", operation=ops.GENERATION, budget=fresh)


class TestBackups:

    def _populate(self, db):
        db.create_user("keep@example.com", "correct-horse")
        uid = db.query_one("SELECT id FROM users WHERE email='keep@example.com'")["id"]
        nid = new_id("nb")
        db.execute("INSERT INTO notebooks (id, owner_id, title, subject, created_at)"
                   " VALUES (?,?,?,?,?)", (nid, uid, "Renal", "Medicine", now_iso()))
        return uid, nid

    def test_a_backup_survives_a_round_trip(self, db, tmp_path):
        """
        THE TEST THAT MAKES THE REST WORTH ANYTHING. Write, back up, destroy,
        restore, read back. A backup nobody has restored is a hypothesis.
        """
        _uid, nid = self._populate(db)
        target = tmp_path / "backups" / "q.backup.db"
        ops.backup(db, target)

        db.close()
        (tmp_path / "q.db").unlink()
        for sidecar in ("q.db-wal", "q.db-shm"):
            (tmp_path / sidecar).unlink(missing_ok=True)

        ops.restore(target, tmp_path / "restored.db")
        restored = Database(tmp_path / "restored.db")
        assert restored.query_one("SELECT title FROM notebooks WHERE id=?",
                                  (nid,))["title"] == "Renal"
        assert restored.query_one(
            "SELECT email FROM users LIMIT 1")["email"] == "keep@example.com"

    def test_a_backup_is_consistent_with_writes_in_the_wal(self, db, tmp_path):
        """
        The failure mode a file copy has and this does not: in WAL mode the
        `.db` file is not the database, so a copy taken before a checkpoint is
        valid, openable, and missing the newest rows.
        """
        _uid, nid = self._populate(db)
        db.execute("UPDATE notebooks SET title = ? WHERE id = ?", ("Written late", nid))
        target = tmp_path / "late.db"
        ops.backup(db, target)
        checked = ops.verify(target, expect_tables=("notebooks",))
        assert checked["ok"]
        con = sqlite3.connect(str(target))
        assert con.execute("SELECT title FROM notebooks WHERE id=?",
                           (nid,)).fetchone()[0] == "Written late"
        con.close()

    def test_verify_catches_a_backup_of_the_wrong_database(self, tmp_path):
        other = tmp_path / "not-quintek.db"
        con = sqlite3.connect(str(other))
        con.execute("CREATE TABLE unrelated (x INTEGER)")
        con.commit()
        con.close()
        checked = ops.verify(other, expect_tables=("users", "questions"))
        assert checked["ok"] is False
        assert set(checked["missing_tables"]) == {"users", "questions"}

    def test_verify_catches_a_corrupt_file(self, tmp_path):
        broken = tmp_path / "broken.db"
        broken.write_bytes(b"this is not a database")
        assert ops.verify(broken)["ok"] is False

    def test_restore_refuses_to_overwrite_a_live_database(self, db, tmp_path):
        """
        A restore that clobbers a live file turns a recoverable afternoon into
        an unrecoverable one.
        """
        self._populate(db)
        target = tmp_path / "b.db"
        ops.backup(db, target)
        with pytest.raises(FileExistsError, match="already exists"):
            ops.restore(target, tmp_path / "q.db")

    def test_restore_refuses_an_unhealthy_backup(self, tmp_path):
        broken = tmp_path / "broken.db"
        broken.write_bytes(b"not a database")
        with pytest.raises(ValueError, match="unhealthy backup"):
            ops.restore(broken, tmp_path / "out.db")

    def test_the_backup_file_is_owner_only(self, db, tmp_path):
        import stat
        self._populate(db)
        target = tmp_path / "perm.db"
        ops.backup(db, target)
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    def test_the_command_verifies_what_it_just_wrote(self, db, tmp_path, capsys):
        """`take` exits non-zero if the copy cannot be read back."""
        self._populate(db)
        code = tools_backup.main(["take", "--db", str(tmp_path / "q.db"),
                                  "--out", str(tmp_path / "out")])
        assert code == 0
        assert "verified OK" in capsys.readouterr().out

    def test_the_command_fails_loudly_on_an_unreadable_backup(self, tmp_path, capsys,
                                                              monkeypatch):
        monkeypatch.setattr(tools_backup, "verify",
                            lambda *_a, **_kw: {"ok": False, "integrity": "malformed"})
        db = Database(tmp_path / "q.db")
        db.create_user("x@example.com", "correct-horse")
        assert tools_backup.main(["take", "--db", str(tmp_path / "q.db"),
                                  "--out", str(tmp_path / "out")]) == 1
        assert "treat it as absent" in capsys.readouterr().err


class TestTheOpsRoutes:

    def test_a_learner_cannot_see_operational_data(self, db):
        api = StudentAPI(db)
        token = api.handle("POST", "/auth/register", {},
                           {"email": "l@example.com", "password": "correct-horse"},
                           None)[1]["token"]
        for route in ("/ops/incidents", "/ops/alerts", "/ops/spend"):
            status, _ = api.handle("GET", route, {}, {}, token)
            assert status == 404, f"{route} leaked to a learner"

    def test_an_admin_sees_what_is_failing(self, admin_api, db):
        api, token = admin_api
        ops.record(db, operation=ops.INGESTION, error=ValueError("pdf unreadable"))
        status, out = api.handle("GET", "/ops/incidents", {}, {}, token)
        assert status == 200
        assert out["by_fault"][0]["error_type"] == "ValueError"

    def test_an_outage_is_discoverable_without_a_user_message(self, admin_api, db):
        """The exit condition for this phase, stated as a test."""
        api, token = admin_api
        for _ in range(6):
            ops.record(db, operation=ops.GENERATION,
                       error=TimeoutError("provider timed out"))
        _, out = api.handle("GET", "/ops/alerts", {}, {}, token)
        assert out["alerts"], (
            "six instances of one fault in the window produced no alert; the only "
            "remaining way to learn about this outage is a user complaining")

    def test_health_reports_absence_rather_than_filling_it_in(self, db):
        """
        Verified rather than assumed: this was the one Phase 3 item the brief
        assumed was missing and was not.
        """
        status, out = StudentAPI(db).handle("GET", "/health", {}, {}, None)
        assert status == 200
        assert out["generation"] == "unavailable"
        assert out["ai_configured"] is False
        assert out["database"] is True
