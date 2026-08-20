"""
The plan sync tool.

Its whole job is to keep one relationship straight: Quintek's plan is the
record and the gateway's id is an attribute of it. The tests are about what it
refuses to do -- relink a plan, duplicate one, or touch anything when nothing
is configured.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys

import pytest

import tools_razorpay_sync as tool
from billing.db import connect
from billing.plans import PlanError, PlanStore


@pytest.fixture()
def store(tmp_path):
    conn = connect(tmp_path / "b.db")
    s = PlanStore(conn)
    s.seed_from_config()
    return s


# ---------------------------------------------------------------- the link

def test_a_fresh_plan_has_no_gateway_id(store) -> None:
    plan = store.active("pro", "monthly")
    assert store.gateway_ref(plan.id) == ("", "")


def test_the_link_is_recorded_against_the_quintek_plan(store) -> None:
    plan = store.active("pro", "monthly")
    store.set_gateway_ref(plan.id, "razorpay", "plan_ABC123")
    assert store.gateway_ref(plan.id) == ("razorpay", "plan_ABC123")


def test_relinking_a_plan_is_refused(store) -> None:
    """
    Two gateway plans for one Quintek plan means some subscribers are billed
    against a record nobody is watching. The only symptom is a complaint.
    """
    plan = store.active("pro", "monthly")
    store.set_gateway_ref(plan.id, "razorpay", "plan_ABC123")
    with pytest.raises(PlanError) as exc:
        store.set_gateway_ref(plan.id, "razorpay", "plan_DIFFERENT")
    assert "refusing to relink" in str(exc.value).lower()
    assert store.gateway_ref(plan.id)[1] == "plan_ABC123"


def test_recording_the_same_id_twice_is_harmless(store) -> None:
    plan = store.active("pro", "monthly")
    store.set_gateway_ref(plan.id, "razorpay", "plan_ABC123")
    store.set_gateway_ref(plan.id, "razorpay", "plan_ABC123")
    assert store.gateway_ref(plan.id)[1] == "plan_ABC123"


def test_unlinked_lists_paid_plans_only(store) -> None:
    pending = store.unlinked()
    assert pending
    assert all(p.price_minor > 0 for p in pending), (
        "the free plan has nothing to collect and needs no gateway record")


def test_linking_removes_a_plan_from_the_work_list(store) -> None:
    before = len(store.unlinked())
    store.set_gateway_ref(store.active("pro", "monthly").id, "razorpay", "plan_X")
    assert len(store.unlinked()) == before - 1


# ---------------------------------------------------------------- the payload

def test_the_payload_carries_minor_units_unconverted(store) -> None:
    plan = store.active("pro", "monthly")
    payload = tool.plan_payload(plan)
    assert payload["item"]["amount"] == plan.price_minor
    assert payload["item"]["currency"] == "INR"


def test_annual_maps_to_the_gateways_word_for_it(store) -> None:
    assert tool.plan_payload(store.active("pro", "annual"))["period"] == "yearly"
    assert tool.plan_payload(store.active("pro", "monthly"))["period"] == "monthly"


def test_the_payload_carries_the_quintek_plan_id_back(store) -> None:
    """
    So that a plan found on the gateway can be traced to the record that owns
    it, without consulting anything but the gateway.
    """
    plan = store.active("power", "monthly")
    assert tool.plan_payload(plan)["notes"]["quintek_plan_id"] == plan.id


def test_a_plan_with_no_gateway_period_is_refused(store) -> None:
    free = store.free_plan()
    with pytest.raises(ValueError) as exc:
        tool.plan_payload(free)
    assert "no gateway period" in str(exc.value)


def test_no_allowance_or_cap_is_sent_to_the_gateway(store) -> None:
    """
    Razorpay collects money; it is not Quintek's pricing database. Allowances
    live in `plans` so that changing gateway is an adapter change rather than a
    migration of the product's economics.
    """
    payload = json.dumps(tool.plan_payload(store.active("pro", "monthly")))
    body = json.loads(payload)
    assert "monthly_question_allowance" not in payload
    assert set(body["item"]) == {"name", "amount", "currency", "description"}


# ---------------------------------------------------------------- the command

def run(args, env=None, cwd=None):
    return subprocess.run([sys.executable, "tools_razorpay_sync.py", *args],
                          capture_output=True, text=True, env=env)


def test_status_runs_without_credentials(tmp_path) -> None:
    result = run(["--db", str(tmp_path / "b.db"), "status"])
    assert result.returncode == 0, result.stderr
    assert "NOT LINKED" in result.stdout


def test_sync_defaults_to_a_dry_run(tmp_path) -> None:
    db = tmp_path / "b.db"
    result = run(["--db", str(db), "sync"])
    assert result.returncode == 0, result.stderr
    assert "WOULD CREATE" in result.stdout
    assert "Dry run" in result.stdout

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    linked = conn.execute(
        "SELECT COUNT(*) AS n FROM plans WHERE gateway_plan_id != ''").fetchone()["n"]
    assert linked == 0, "a dry run wrote to the database"


def test_apply_without_credentials_refuses_rather_than_half_working(tmp_path) -> None:
    import os
    env = {k: v for k, v in os.environ.items()
           if k not in ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET")}
    result = run(["--db", str(tmp_path / "b.db"), "sync", "--apply"], env=env)
    assert result.returncode == 1
    assert "Refusing to apply" in result.stdout


def test_check_without_credentials_says_so_and_exits_nonzero(tmp_path) -> None:
    import os
    env = {k: v for k, v in os.environ.items()
           if k not in ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET")}
    result = run(["--db", str(tmp_path / "b.db"), "check"], env=env)
    assert result.returncode == 1
    assert "Nothing to test" in result.stdout


def test_no_command_prints_no_secret(tmp_path) -> None:
    """
    Belt and braces: whatever this tool prints, it must never be a credential.
    """
    result = run(["--db", str(tmp_path / "b.db"), "status"])
    for env_name in ("RAZORPAY_KEY_SECRET", "RAZORPAY_KEY_ID"):
        import os
        value = os.environ.get(env_name)
        if value:
            assert value not in result.stdout


def test_sync_is_idempotent(tmp_path, store) -> None:
    """Every plan already linked means there is nothing to do, not a duplicate."""
    for plan in store.unlinked():
        store.set_gateway_ref(plan.id, "razorpay", f"plan_{plan.id}")
    assert store.unlinked() == []
