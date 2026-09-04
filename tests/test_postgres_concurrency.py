"""
The billing invariant that must hold on PostgreSQL, not only on SQLite.

WHY THIS FILE IS SEPARATE
-------------------------
`tests/test_billing_invariants.py` already proves that two simultaneous
200-unit requests cannot both pass against a 300 daily cap. It proves it on
SQLite, where `BEGIN IMMEDIATE` takes a database-wide write lock and makes the
read-check-write one step.

PostgreSQL has no such statement, and the obvious translation -- a plain
`BEGIN` -- does not hold the invariant. Readers do not block readers under
MVCC, so both transactions read the same pre-existing usage, both conclude
there is room, and both commit. Measured before this port: 400 units
authorised against a 300 cap.

So the invariant needs its own test on the backend where it can actually
break. If `QUINTEK_TEST_POSTGRES_URL` is unset these SKIP. A skip is not a
pass: it means nobody checked.
"""

from __future__ import annotations

import threading

import pytest

from billing.entitlements import EntitlementEngine, period_start_for, today_iso
from billing.plans import PlanStore, now_iso
from billing.usage import InsufficientAllowance, UsageService


def _fresh(pg_schema):
    from billing.db import connect
    conn = connect()
    plans = PlanStore(conn)
    plans.seed_from_config("configs/plans.json")
    return conn, plans


def _subscribe(conn, plans, user_id, family="pro"):
    """Put the user on a plan with a 300/day cap, the same shape the SQLite
    invariant test uses."""
    plan = plans.active(family, "monthly")
    conn.execute(
        "INSERT INTO subscriptions (id, user_id, plan_id, billing_interval, status,"
        " created_at, updated_at) VALUES (?,?,?,?, 'ACTIVE', ?,?)",
        (f"sub_{user_id}", user_id, plan.id, "monthly", now_iso(), now_iso()))
    conn.execute(
        "INSERT INTO entitlements (id, user_id, subscription_id, plan_id,"
        " monthly_allowance, daily_limit, session_limit, effective_from, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (f"ent_{user_id}", user_id, f"sub_{user_id}", plan.id,
         plan.monthly_question_allowance, plan.daily_question_limit,
         plan.session_question_limit, today_iso(), now_iso()))
    conn.commit()
    return plan


def test_daily_cap_survives_concurrent_requests_on_postgres(pg_schema):
    """
    Two 200-unit reservations, at once, against a 300/day cap.

    This is the test that fails if `BEGIN IMMEDIATE` is translated naively.
    """
    conn, plans = _fresh(pg_schema)
    plan = _subscribe(conn, plans, "u1")
    assert plan.daily_question_limit == 300, "fixture assumes the Pro daily cap"

    outcomes: list[int] = []
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def attempt():
        from billing.db import connect
        own = connect()
        service = UsageService(own, EntitlementEngine(own, PlanStore(own)))
        try:
            barrier.wait(timeout=20)          # maximise the overlap
            reservation = service.reserve("u1", 200, allow_partial=False)
            outcomes.append(reservation.question_units)
        except InsufficientAllowance:
            outcomes.append(0)
        except BaseException as exc:          # noqa: BLE001 - reported, not swallowed
            errors.append(exc)
        finally:
            own.close()

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, f"a reservation thread raised: {errors!r}"
    assert sum(outcomes) <= 300, (
        f"authorised {sum(outcomes)} units against a 300/day cap: the "
        "read-check-write was not serialised")
    assert sorted(outcomes) == [0, 200], (
        f"expected exactly one grant and one refusal, got {outcomes}")

    held = conn.execute(
        "SELECT COALESCE(SUM(question_units),0) AS n FROM reservations"
        " WHERE user_id = ? AND status = 'HELD'", ("u1",)).fetchone()
    assert held["n"] == 200
    conn.close()


def test_five_way_race_still_respects_the_cap_on_postgres(pg_schema):
    """
    Five simultaneous 100-unit requests against 300/day.

    Two threads can pass by luck; five cannot. This is the one that catches a
    lock scoped to the wrong thing.
    """
    conn, plans = _fresh(pg_schema)
    _subscribe(conn, plans, "u2")

    outcomes: list[int] = []
    barrier = threading.Barrier(5)
    errors: list[BaseException] = []

    def attempt():
        from billing.db import connect
        own = connect()
        service = UsageService(own, EntitlementEngine(own, PlanStore(own)))
        try:
            barrier.wait(timeout=20)
            outcomes.append(service.reserve("u2", 100, allow_partial=False).question_units)
        except InsufficientAllowance:
            outcomes.append(0)
        except BaseException as exc:          # noqa: BLE001
            errors.append(exc)
        finally:
            own.close()

    threads = [threading.Thread(target=attempt) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, f"a reservation thread raised: {errors!r}"
    assert sum(outcomes) <= 300, f"authorised {sum(outcomes)} against a 300 cap"
    assert sum(1 for o in outcomes if o == 100) == 3


def test_two_different_users_do_not_block_each_other(pg_schema):
    """
    The advisory lock is keyed per user, so two learners reserving at the same
    moment both succeed.

    This is the property SQLite's database-wide write lock did NOT have, and
    it is worth a test so a future change back to a global lock is visible as
    a regression rather than as a mysterious slowdown.
    """
    conn, plans = _fresh(pg_schema)
    _subscribe(conn, plans, "a1")
    _subscribe(conn, plans, "b1")

    granted: dict[str, int] = {}
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def attempt(user_id):
        from billing.db import connect
        own = connect()
        service = UsageService(own, EntitlementEngine(own, PlanStore(own)))
        try:
            barrier.wait(timeout=20)
            granted[user_id] = service.reserve(user_id, 200,
                                               allow_partial=False).question_units
        except BaseException as exc:          # noqa: BLE001
            errors.append(exc)
        finally:
            own.close()

    threads = [threading.Thread(target=attempt, args=(u,)) for u in ("a1", "b1")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, f"a reservation thread raised: {errors!r}"
    assert granted == {"a1": 200, "b1": 200}


def test_a_reservation_cannot_be_committed_twice_concurrently(pg_schema):
    """
    Settlement is keyed on the reservation, not the user.

    Two threads committing the same hold must produce ONE usage row. Without
    the lock both read status HELD and both write to the append-only ledger,
    which double-bills a learner and cannot be corrected by editing, because
    the ledger refuses updates by design.
    """
    conn, plans = _fresh(pg_schema)
    _subscribe(conn, plans, "u3")
    service = UsageService(conn, EntitlementEngine(conn, PlanStore(conn)))
    reservation = service.reserve("u3", 50, allow_partial=False)

    results: list[dict] = []
    failures: list[BaseException] = []
    barrier = threading.Barrier(2)

    def settle():
        from billing.db import connect
        own = connect()
        own_service = UsageService(own, EntitlementEngine(own, PlanStore(own)))
        try:
            barrier.wait(timeout=20)
            results.append(own_service.commit(reservation.id))
        except BaseException as exc:          # noqa: BLE001
            failures.append(exc)
        finally:
            own.close()

    threads = [threading.Thread(target=settle) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    rows = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(question_units),0) AS units"
        " FROM usage_ledger WHERE user_id = ?", ("u3",)).fetchone()
    assert rows["n"] == 1, f"the hold was committed {rows['n']} times"
    assert rows["units"] == 50
    assert len(results) == 1 and len(failures) == 1
    conn.close()
