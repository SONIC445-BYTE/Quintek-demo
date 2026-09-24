"""
The first admin: created out of band, with no password, and given one only by
the operator through a prompt.

Why this shape: production had no admin, so the operator routes were
unreachable and no tester could be suspended. The fix must not put a password
anyone else chose into production, and must not add an HTTP path to privilege.
"""

from __future__ import annotations

import pytest

from benchmark import cli
from student import accounts
from student.api import StudentAPI
from student.db import UNUSABLE_PASSWORD_HASH
from student.server import BOOTSTRAP_ADMIN_ENV, bootstrap_admin_from_env

EMAIL = "operator@quintek.invalid"
GOOD = "a long enough operator passphrase"


@pytest.fixture
def api(any_backend):
    return StudentAPI(any_backend.student())


def login(api, email, password):
    return api.handle("POST", "/auth/login", {}, {"email": email, "password": password}, None)


def test_the_bootstrap_makes_an_admin_nobody_can_log_into(api):
    out = accounts.bootstrap_admin(api.db, EMAIL)
    assert out["created"] is True
    row = api.db.query_one("SELECT role, password_hash FROM users WHERE id = ?",
                           (out["user_id"],))
    assert row["role"] == "admin" and row["password_hash"] == UNUSABLE_PASSWORD_HASH
    for guess in ("", "password123", UNUSABLE_PASSWORD_HASH, "!", GOOD):
        st, _ = login(api, EMAIL, guess)
        assert st == 401, f"the password-less admin accepted {guess!r}"


def test_the_bootstrap_is_idempotent(api):
    first = accounts.bootstrap_admin(api.db, EMAIL)
    again = accounts.bootstrap_admin(api.db, EMAIL.upper())
    assert again == {"user_id": first["user_id"], "created": False,
                     "outcome": "already an admin"}
    assert api.db.query_one("SELECT COUNT(*) n FROM users")["n"] == 1


def test_the_bootstrap_never_promotes_an_existing_learner(api):
    """Otherwise registering the address before the operator deploys would be
    a way to be handed admin rights."""
    st, reg = api.handle("POST", "/auth/register", {},
                         {"email": EMAIL, "password": "correct-horse"}, None)
    assert st in (200, 201)
    with pytest.raises(accounts.AccountError, match="never promotes"):
        accounts.bootstrap_admin(api.db, EMAIL)
    assert api.db.query_one("SELECT role FROM users WHERE email = ?",
                            (EMAIL,))["role"] == "learner"


def test_set_password_opens_the_account_and_the_operator_routes(api):
    accounts.bootstrap_admin(api.db, EMAIL)
    accounts.set_password(api.db, EMAIL, GOOD)
    st, out = login(api, EMAIL, GOOD)
    assert st == 200, out
    st, body = api.handle("GET", "/ops/incidents", {}, {}, out["token"])
    assert st == 200, body
    assert login(api, EMAIL, GOOD + "x")[0] == 401


def test_set_password_signs_out_every_existing_session(api):
    accounts.bootstrap_admin(api.db, EMAIL)
    accounts.set_password(api.db, EMAIL, GOOD)
    tok = login(api, EMAIL, GOOD)[1]["token"]
    accounts.set_password(api.db, EMAIL, GOOD + " rotated")
    st, _ = api.handle("GET", "/me", {}, {}, tok)
    assert st == 401, "a session from before the reset still works"


@pytest.mark.parametrize("email, password, why", [
    (EMAIL, "short", "at least 12"),
    (EMAIL, None, "at least 12"),
    ("nobody@quintek.invalid", GOOD, "no account"),
])
def test_set_password_refuses_with_a_reason_and_changes_nothing(api, email, password, why):
    accounts.bootstrap_admin(api.db, EMAIL)
    with pytest.raises(accounts.AccountError, match=why):
        accounts.set_password(api.db, email, password)
    assert api.db.query_one("SELECT password_hash FROM users WHERE email = ?",
                            (EMAIL,))["password_hash"] == UNUSABLE_PASSWORD_HASH


def test_the_environment_hook(api, monkeypatch, capsys):
    monkeypatch.delenv(BOOTSTRAP_ADMIN_ENV, raising=False)
    assert bootstrap_admin_from_env(api.db) is None
    assert api.db.query_one("SELECT COUNT(*) n FROM users")["n"] == 0

    monkeypatch.setenv(BOOTSTRAP_ADMIN_ENV, EMAIL)
    assert bootstrap_admin_from_env(api.db)["created"] is True
    assert bootstrap_admin_from_env(api.db)["created"] is False
    printed = capsys.readouterr().out
    assert "admin bootstrap" in printed and GOOD not in printed


def test_a_refused_bootstrap_does_not_stop_the_server(api, monkeypatch, capsys):
    api.handle("POST", "/auth/register", {},
               {"email": EMAIL, "password": "correct-horse"}, None)
    monkeypatch.setenv(BOOTSTRAP_ADMIN_ENV, EMAIL)
    out = bootstrap_admin_from_env(api.db)        # must not raise
    assert out["created"] is False
    assert "REFUSED" in capsys.readouterr().out


def test_the_cli_reads_the_password_from_a_prompt(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "cli.db"
    from student.db import Database
    accounts.bootstrap_admin(Database(db_path), EMAIL)
    answers = iter([GOOD, GOOD])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    assert cli.main(["set-password", "--email", EMAIL, "--db", str(db_path)]) == 0
    assert Database(db_path).verify_password(EMAIL, GOOD)
    assert GOOD not in capsys.readouterr().out


def test_the_cli_changes_nothing_when_the_two_entries_differ(tmp_path, monkeypatch):
    db_path = tmp_path / "cli.db"
    from student.db import Database
    accounts.bootstrap_admin(Database(db_path), EMAIL)
    answers = iter([GOOD, GOOD + "?"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    assert cli.main(["set-password", "--email", EMAIL, "--db", str(db_path)]) == 1
    assert Database(db_path).verify_password(EMAIL, GOOD) is None


def test_the_unusable_marker_can_never_be_a_password_hash():
    """The guarantee `verify_password` rests on. Its explicit check for the
    marker is a second layer and, while this holds, removing it changes no
    behaviour -- so this property is what is pinned. A marker that could be a
    PBKDF2 hex digest would make some password log into the account."""
    import string
    assert any(c not in string.hexdigits for c in UNUSABLE_PASSWORD_HASH)
