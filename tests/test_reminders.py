"""
Reminders (ADR-031): any number, each a date, a time and the learner's own words.

What these tests pin down, in the order the module docstring states the rules:

  * any number, independent -- creating, editing or cancelling one never
    touches another;
  * the label is opaque -- stored and delivered byte-for-byte as written,
    including leading spaces, newlines and non-Latin text;
  * it fires because its own time arrived -- not because anything is due,
    and firing starts no revision session;
  * a wall-clock time that denotes no instant, or two, is REFUSED;
  * with no sender configured, a due reminder is recorded `failed` with the
    reason, never reported as sent;
  * a reminder is claimed before it is sent, so two runs cannot both send it.

Ownership across learners is covered by the cross-user meta-test
(`test_cross_user_access.py`), which enumerates `/reminders/<id>` like every
other id-bearing route. The service-level scoping is also tested here, below
the API, so that removing the owner clause fails a test even if a route
happened to check ownership some other way.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from student.api import StudentAPI
from student.reminders import (CANCELLED, FAILED, FIRED, LABEL_MAX, NO_SENDER, PENDING,
                               ReminderConflict, ReminderError, ReminderService, to_utc)

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
LATER = datetime(2027, 6, 1, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def svc(any_backend):
    """Every test in this file runs on SQLite AND PostgreSQL: the claim relies on
    `rowcount` from a conditional UPDATE, and the NUL check exists because of a
    PostgreSQL TEXT rule, so a SQLite-only pass would not cover either."""
    db = any_backend.student()
    s = ReminderService(db)
    s.a = db.create_user("a@example.com", "correct-horse")
    s.b = db.create_user("b@example.com", "correct-horse")
    return s


def make(svc, uid=None, label="revise patho", d="2027-01-10", t="20:00", tz="Asia/Kolkata"):
    return svc.create(uid or svc.a, label=label, local_date=d, local_time=t, tz=tz, now=NOW)


# ---------------------------------------------------------------------------
# The label is opaque
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("label", [
    "revise patho",
    "  leading and trailing spaces  ",
    "line one\nline two",
    "मस्तिष्क — revise neuro 🧠",
    "x" * LABEL_MAX,
    "<b>not html</b> & 'quotes' \"too\"",
])
def test_the_label_is_stored_and_listed_verbatim(svc, label):
    r = make(svc, label=label)
    assert r["label"] == label
    assert [x["label"] for x in svc.list(svc.a)] == [label]
    raw = svc.db.query_one("SELECT label FROM reminders WHERE id=?", (r["id"],))["label"]
    assert raw == label


@pytest.mark.parametrize("label, why", [
    ("", "needs some text"),
    ("   \n\t ", "needs some text"),
    (None, "needs some text"),
    (42, "needs some text"),
    ("x" * (LABEL_MAX + 1), "limit is 200"),
    ("bad\x00byte", "NUL"),
])
def test_an_unusable_label_is_refused_with_a_reason(svc, label, why):
    with pytest.raises(ReminderError, match=why):
        make(svc, label=label)
    assert svc.list(svc.a) == []


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------

def test_a_local_time_is_converted_to_the_right_instant():
    # IST has no DST: 20:00 IST is 14:30 UTC, always.
    assert to_utc("2027-01-10", "20:00", "Asia/Kolkata") == datetime(
        2027, 1, 10, 14, 30, tzinfo=timezone.utc)
    # New York is UTC-5 in January and UTC-4 in July; the same wall-clock time
    # must land on different UTC hours.
    assert to_utc("2027-01-10", "08:00", "America/New_York").hour == 13
    assert to_utc("2027-07-10", "08:00", "America/New_York").hour == 12


def test_the_stored_instant_is_what_the_learner_picked(svc):
    r = make(svc, d="2027-01-10", t="20:00", tz="Asia/Kolkata")
    assert r["fire_at"] == "2027-01-10T14:30:00Z"
    assert (r["local_date"], r["local_time"], r["timezone"]) == (
        "2027-01-10", "20:00", "Asia/Kolkata")


def test_a_time_the_clocks_skip_is_refused():
    # Europe/London springs forward at 01:00 on 2026-03-29: 01:30 never happens.
    with pytest.raises(ReminderError, match="does not exist"):
        to_utc("2026-03-29", "01:30", "Europe/London")


def test_a_time_the_clocks_repeat_is_refused():
    # Europe/London falls back at 02:00 on 2026-10-25: 01:30 happens twice.
    with pytest.raises(ReminderError, match="happens twice"):
        to_utc("2026-10-25", "01:30", "Europe/London")


def test_times_either_side_of_a_transition_are_accepted():
    assert to_utc("2026-10-25", "00:59", "Europe/London")
    assert to_utc("2026-10-25", "02:00", "Europe/London")
    assert to_utc("2026-03-29", "02:00", "Europe/London")


@pytest.mark.parametrize("d, t, tz, why", [
    ("2027-13-01", "20:00", "Asia/Kolkata", "YYYY-MM-DD"),
    ("tomorrow", "20:00", "Asia/Kolkata", "YYYY-MM-DD"),
    ("2027-01-10", "25:00", "Asia/Kolkata", "HH:MM"),
    ("2027-01-10", "8pm", "Asia/Kolkata", "HH:MM"),
    ("2027-01-10", "20:00:30", "Asia/Kolkata", "to the minute"),
    ("2027-01-10", "20:00", "Mars/Olympus", "unknown timezone"),
    ("2027-01-10", "20:00", "", "timezone is required"),
    ("2026-09-23", "10:00", "UTC", "already passed"),
])
def test_an_unusable_time_is_refused_with_a_reason(svc, d, t, tz, why):
    with pytest.raises(ReminderError, match=why):
        make(svc, d=d, t=t, tz=tz)


def test_a_time_in_the_past_is_refused_even_by_one_minute(svc):
    with pytest.raises(ReminderError, match="already passed"):
        make(svc, d="2026-09-23", t="12:00", tz="UTC")
    assert make(svc, d="2026-09-23", t="12:01", tz="UTC")["status"] == PENDING


# ---------------------------------------------------------------------------
# Any number, independent
# ---------------------------------------------------------------------------

def test_several_reminders_coexist_and_list_in_time_order(svc):
    late = make(svc, label="revise micro", d="2027-02-01")
    early = make(svc, label="revise patho", d="2027-01-01")
    third = make(svc, label="revise pharm", d="2027-01-15")
    assert [r["id"] for r in svc.list(svc.a)] == [early["id"], third["id"], late["id"]]


def test_editing_one_reminder_leaves_the_others_alone(svc):
    one, two = make(svc, label="one"), make(svc, label="two", d="2027-03-01")
    before = svc.get(svc.a, two["id"])
    edited = svc.update(svc.a, one["id"], label="one, edited", local_time="07:15", now=NOW)
    assert edited["label"] == "one, edited" and edited["local_time"] == "07:15"
    assert edited["fire_at"] == "2027-01-10T01:45:00Z"
    assert svc.get(svc.a, two["id"]) == before


def test_cancelling_one_reminder_leaves_the_others_pending(svc):
    one, two = make(svc, label="one"), make(svc, label="two")
    assert svc.cancel(svc.a, one["id"])["status"] == CANCELLED
    assert svc.get(svc.a, two["id"])["status"] == PENDING
    # Cancelled, not deleted: the learner's list still shows it.
    assert {r["label"]: r["status"] for r in svc.list(svc.a)} == {
        "one": CANCELLED, "two": PENDING}


def test_an_edit_is_validated_like_a_create(svc):
    r = make(svc)
    with pytest.raises(ReminderError, match="does not exist"):
        svc.update(svc.a, r["id"], local_date="2027-03-28", local_time="01:30",
                   tz="Europe/London", now=NOW)
    with pytest.raises(ReminderError, match="needs some text"):
        svc.update(svc.a, r["id"], label="  ", now=NOW)
    assert svc.get(svc.a, r["id"]) == r, "a refused edit changed the reminder"


@pytest.mark.parametrize("finish", ["cancel", "fire"])
def test_a_finished_reminder_cannot_be_edited_or_cancelled_again(svc, finish):
    r = make(svc)
    if finish == "cancel":
        svc.cancel(svc.a, r["id"])
    else:
        svc.run_due(at=LATER)
    with pytest.raises(ReminderConflict):
        svc.update(svc.a, r["id"], label="rewrite history", now=NOW)
    with pytest.raises(ReminderConflict):
        svc.cancel(svc.a, r["id"])


# ---------------------------------------------------------------------------
# Ownership, below the API
# ---------------------------------------------------------------------------

def test_another_learners_reminder_does_not_exist_to_the_service(svc):
    mine = make(svc, uid=svc.a, label="A's own words")
    assert svc.get(svc.b, mine["id"]) is None
    assert svc.update(svc.b, mine["id"], label="B wrote this", now=NOW) is None
    assert svc.cancel(svc.b, mine["id"]) is None
    assert svc.list(svc.b) == []
    assert svc.get(svc.a, mine["id"]) == mine, "B's calls changed A's reminder"


@pytest.mark.parametrize("state", ["cancelled", "fired"])
def test_another_learner_cannot_learn_a_reminders_state_from_the_error(svc, state):
    """B's edit or cancel of A's reminder answers "no such reminder" whatever
    state it is in, and whatever B sent. A 409 "this reminder is cancelled",
    or a 400 about B's own bad input, would tell B the id exists -- the
    UPDATE's own owner clause would still stop the write, which is exactly
    why the SELECT's clause needs a test of its own."""
    mine = make(svc, uid=svc.a)
    if state == "cancelled":
        svc.cancel(svc.a, mine["id"])
    else:
        svc.run_due(at=LATER)
    assert svc.update(svc.b, mine["id"], label="x", now=NOW) is None
    assert svc.cancel(svc.b, mine["id"]) is None


def test_another_learners_bad_input_is_still_no_such_reminder(svc):
    mine = make(svc, uid=svc.a)
    assert svc.update(svc.b, mine["id"], label="", now=NOW) is None
    assert svc.update(svc.b, mine["id"], local_date="2027-03-28", local_time="01:30",
                      tz="Europe/London", now=NOW) is None


def test_the_public_shape_carries_no_user_id(svc):
    assert "user_id" not in make(svc)
    assert all("user_id" not in r for r in svc.list(svc.a))


# ---------------------------------------------------------------------------
# Firing
# ---------------------------------------------------------------------------

def test_with_no_sender_a_due_reminder_is_recorded_failed_not_sent(svc):
    r = make(svc)
    out = svc.run_due(at=LATER)
    assert out == {"due": 1, "sent": 0, "failed": 1}
    got = svc.get(svc.a, r["id"])
    assert got["status"] == FAILED and got["detail"] == NO_SENDER


def test_a_sender_receives_the_label_exactly_as_written(svc):
    label = "  revise patho\n— then micro  "
    sent = []
    svc.sender = lambda p: sent.append(p) or True
    r = make(svc, label=label)
    assert svc.run_due(at=LATER) == {"due": 1, "sent": 1, "failed": 0}
    assert [p["label"] for p in sent] == [label]
    assert sent[0]["reminder_id"] == r["id"] and sent[0]["user_id"] == svc.a
    assert set(sent[0]) == {"user_id", "reminder_id", "label", "fire_at"}, (
        "the payload grew fields -- nothing is added to a learner's reminder")
    assert svc.get(svc.a, r["id"])["status"] == FIRED


@pytest.mark.parametrize("sender, detail", [
    (lambda p: False, "the sender declined it"),
    (lambda p: 1 / 0, "ZeroDivisionError"),
])
def test_a_failing_sender_is_recorded_and_does_not_stop_the_run(svc, sender, detail):
    svc.sender = sender
    a, b = make(svc, label="a"), make(svc, label="b")
    assert svc.run_due(at=LATER) == {"due": 2, "sent": 0, "failed": 2}
    for r in (a, b):
        got = svc.get(svc.a, r["id"])
        assert got["status"] == FAILED and detail in got["detail"]


def test_only_reminders_whose_time_has_come_fire(svc):
    sent = []
    svc.sender = lambda p: sent.append(p["label"]) or True
    make(svc, label="soon", d="2027-01-10")
    make(svc, label="later", d="2028-01-10")
    cancelled = make(svc, label="cancelled", d="2027-01-10")
    svc.cancel(svc.a, cancelled["id"])
    assert [r["label"] for r in svc.due(at=LATER)] == ["soon"]
    assert svc.run_due(at=LATER) == {"due": 1, "sent": 1, "failed": 0}
    assert sent == ["soon"]


def test_a_reminder_is_sent_once_even_if_two_runs_see_it(svc):
    """Both runs read the reminder as due BEFORE either fires it -- the
    overlap a slow cron produces. Only the claim stops the second send."""
    sent = []
    svc.sender = lambda p: sent.append(p["reminder_id"]) or True
    r = make(svc)
    seen_by_first, seen_by_second = svc.due(at=LATER), svc.due(at=LATER)
    first = [svc.fire(x, at=LATER) for x in seen_by_first]
    second = [svc.fire(x, at=LATER) for x in seen_by_second]
    assert sent == [r["id"]]
    assert first[0]["ok"] and second == [{"id": r["id"], "ok": False,
                                          "detail": "already claimed"}]


def test_firing_starts_no_revision_session_and_reads_no_due_state(svc):
    """A reminder is not tied to `due_at` or to any concept. Firing one must
    not create a session, and must not depend on whether anything is due."""
    svc.sender = lambda p: True
    make(svc)
    before = svc.db.query_one("SELECT COUNT(*) n FROM revision_sessions")["n"]
    assert svc.run_due(at=LATER)["sent"] == 1
    assert svc.db.query_one("SELECT COUNT(*) n FROM revision_sessions")["n"] == before


# ---------------------------------------------------------------------------
# Over the API
# ---------------------------------------------------------------------------

@pytest.fixture
def api(any_backend):
    a = StudentAPI(any_backend.student())
    a.tok = a.handle("POST", "/auth/register", {},
                     {"email": "api@example.com", "password": "correct-horse"},
                     None)[1]["token"]
    return a


def call(api, method, path, body=None):
    return api.handle(method, path, {}, body or {}, api.tok)


def test_the_routes_create_list_edit_and_cancel(api):
    body = {"label": "revise patho", "local_date": "2099-01-10",
            "local_time": "20:00", "timezone": "Asia/Kolkata"}
    st, r = call(api, "POST", "/reminders", body)
    assert st == 201 and r["label"] == "revise patho" and r["status"] == PENDING
    st, r2 = call(api, "POST", "/reminders", {**body, "label": "revise micro"})
    assert st == 201
    st, listed = call(api, "GET", "/reminders")
    assert st == 200 and {x["label"] for x in listed["reminders"]} == {
        "revise patho", "revise micro"}
    st, edited = call(api, "PUT", f"/reminders/{r['id']}", {"local_time": "21:30"})
    assert st == 200 and edited["local_time"] == "21:30" and edited["label"] == "revise patho"
    st, gone = call(api, "DELETE", f"/reminders/{r['id']}")
    assert st == 200 and gone["status"] == CANCELLED
    st, again = call(api, "DELETE", f"/reminders/{r['id']}")
    assert st == 409, again
    st, same = call(api, "GET", f"/reminders/{r2['id']}")
    assert st == 200 and same["status"] == PENDING


@pytest.mark.parametrize("body", [
    {"label": "", "local_date": "2099-01-10", "local_time": "20:00", "timezone": "UTC"},
    {"label": "x", "local_date": "2099-01-10", "local_time": "20:00"},
    {"label": "x", "local_date": "2000-01-10", "local_time": "20:00", "timezone": "UTC"},
    {"label": "x", "local_date": "2099-10-25", "local_time": "01:30",
     "timezone": "Europe/London"},
])
def test_a_bad_request_is_a_400_with_the_reason_and_saves_nothing(api, body):
    st, out = call(api, "POST", "/reminders", body)
    assert st == 400 and out.get("error"), out
    assert call(api, "GET", "/reminders")[1]["reminders"] == []


def test_an_unknown_reminder_is_a_404(api):
    for method in ("GET", "PUT", "DELETE"):
        st, _ = call(api, method, "/reminders/rem_nope", {"label": "x"})
        assert st == 404
