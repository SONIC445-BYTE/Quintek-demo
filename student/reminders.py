"""
Reminders: any number of them, each a date, a time and the learner's own words.

WHAT A REMINDER IS
------------------
A learner writes a label -- "revise patho", "revise micro" -- and picks a date
and a time. At that moment the system hands the label back to them, verbatim.
That is the whole feature, and each part of that sentence is a rule:

  * **Any number, independent.** Creating one never touches another. There is
    no single per-learner setting any more; the old one-row-per-learner model
    (`notification_prefs`) is retired, not patched. See ADR-031.

  * **The label is opaque.** It is not interpreted, not linked to a concept,
    not validated for content, and not "improved". It is text the learner
    chose and it is delivered as they wrote it. The only checks are the ones a
    database needs: it is not empty, not absurdly long, and carries no NUL
    byte (PostgreSQL's TEXT type rejects one, so accepting it would turn a
    learner's typo into a 500).

  * **It fires because its own time arrived.** Not because a review became due,
    not because a concept turned red. A reminder is not tied to
    `revision_state.due_at` or to any colour; concept-linked scheduling is a
    different feature and deliberately not this one.

TIME
----
The learner enters a LOCAL date and time in a named IANA timezone, because that
is what "remind me at 8pm on the 3rd" means. It is converted to UTC once, at
save time, with `zoneinfo`, and both are stored: the UTC instant is what the
scheduler compares, the local wall-clock values are what the screen shows back.

Two wall-clock times do not map to exactly one instant, and both are REFUSED
rather than guessed:

  * a time inside a spring-forward gap does not exist at all;
  * a time inside a fall-back hour happens twice.

Picking one silently would be the system deciding when the learner meant, and
this codebase does not infer what the learner did not state. The error says
which case it is, so the learner can pick a minute either side.

`next_occurrence` from the old module was NOT reused. It answers "when does
HH:MM next happen, daily", which is a different question from "which instant is
this date and time", and it resolves the ambiguous hour by taking whatever
`astimezone` returns rather than refusing it.

DELIVERY
--------
Built up to the point of calling a sender and no further. `sender(payload) ->
bool` is injected, and none is configured on any deployment, so firing a due
reminder records `failed` with the reason "no notification sender is
configured". That is the honest state, and nothing runs `run_due` in
production either. Both are reported, not built -- see NOT_BUILT.md.

Firing CLAIMS a reminder before sending it (`pending` -> `fired`, conditional
on still being `pending`), so two overlapping runs cannot both send one. The
cost is at-most-once: a crash between the claim and the send loses that
reminder rather than sending it twice. For a reminder the learner can see on
their own list, a missed one is visible and a duplicate is noise; the
trade-off is recorded rather than hidden.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .db import Database, new_id, now_iso

PENDING, FIRED, FAILED, CANCELLED = "pending", "fired", "failed", "cancelled"
STATUSES = (PENDING, FIRED, FAILED, CANCELLED)

#: Long enough for any reminder a person writes; short enough that a list of
#: them is still a list. Counted in characters, not bytes.
LABEL_MAX = 200

NO_SENDER = "no notification sender is configured"

_ISO = "%Y-%m-%dT%H:%M:%SZ"


class ReminderError(ValueError):
    """The request cannot be saved as asked, and the message says why."""


class ReminderConflict(ReminderError):
    """The reminder exists but is no longer pending, so it cannot change."""


def validate_timezone(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ReminderError("a timezone is required, e.g. 'Asia/Kolkata'")
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        raise ReminderError(f"unknown timezone: {name!r}")
    return name


def validate_label(label) -> str:
    if not isinstance(label, str):
        raise ReminderError("the reminder needs some text")
    if not label.strip():
        raise ReminderError("the reminder needs some text")
    if len(label) > LABEL_MAX:
        raise ReminderError(f"the reminder text is {len(label)} characters; "
                            f"the limit is {LABEL_MAX}")
    if "\x00" in label:
        raise ReminderError("the reminder text contains a NUL character")
    return label        # verbatim: never stripped, trimmed or rewritten


def _parse_local(local_date, local_time) -> datetime:
    try:
        d = date.fromisoformat(str(local_date))
    except (TypeError, ValueError):
        raise ReminderError(f"date must be YYYY-MM-DD, got {local_date!r}")
    try:
        t = time.fromisoformat(str(local_time))
    except (TypeError, ValueError):
        raise ReminderError(f"time must be HH:MM, got {local_time!r}")
    if t.second or t.microsecond:
        raise ReminderError("time must be to the minute, HH:MM")
    return datetime.combine(d, t)


def to_utc(local_date, local_time, tz_name: str) -> datetime:
    """The one UTC instant a local date and time denote in `tz_name`.

    Raises `ReminderError` for a wall-clock time that denotes no instant
    (spring-forward gap) or two (fall-back overlap) -- see the module
    docstring for why those are refused rather than resolved.
    """
    tz = ZoneInfo(validate_timezone(tz_name))
    naive = _parse_local(local_date, local_time)
    first, second = naive.replace(tzinfo=tz, fold=0), naive.replace(tzinfo=tz, fold=1)
    if first.utcoffset() != second.utcoffset():
        round_trip = first.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None)
        if round_trip != naive:
            raise ReminderError(
                f"{naive:%Y-%m-%d %H:%M} does not exist in {tz_name}: the clocks go "
                "forward over it. Pick a time outside that hour.")
        raise ReminderError(
            f"{naive:%Y-%m-%d %H:%M} happens twice in {tz_name}: the clocks go back "
            "over it. Pick a time outside that hour so it is clear which one you mean.")
    return first.astimezone(timezone.utc)


def _public(row) -> dict:
    """What a learner is shown about their own reminder. No user id: the
    caller already knows whose it is, and a payload that never carries one
    cannot be the one that leaks it."""
    return {
        "id": row["id"], "label": row["label"],
        "local_date": row["local_date"], "local_time": row["local_time"],
        "timezone": row["timezone"], "fire_at": row["fire_at"],
        "status": row["status"], "detail": row["detail"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "fired_at": row["fired_at"],
    }


class ReminderService:
    def __init__(self, db: Database, *, sender=None):
        self.db = db
        self.sender = sender

    # ---------- the learner's own reminders --------------------------------
    #
    # EVERY query below that names a reminder by id also names the owner, in
    # the same WHERE clause. There is no "fetch by id, then check the owner"
    # step to forget: a reminder that is not yours does not exist as far as
    # these methods can tell, which is the same answer the rest of the API
    # gives (404, "no such reminder").

    def _instant(self, local_date, local_time, tz, *, now: datetime | None) -> str:
        at = to_utc(local_date, local_time, tz)
        moment = now or datetime.now(timezone.utc)
        if at <= moment:
            raise ReminderError(
                f"{local_date} {local_time} in {tz} has already passed; a reminder "
                "must be in the future")
        return at.strftime(_ISO)

    def create(self, user_id: str, *, label, local_date, local_time, tz,
               now: datetime | None = None) -> dict:
        label = validate_label(label)
        fire_at = self._instant(local_date, local_time, tz, now=now)
        rid, stamp = new_id("rem"), now_iso()
        self.db.execute(
            "INSERT INTO reminders (id, user_id, label, local_date, local_time, timezone,"
            " fire_at, status, detail, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (rid, user_id, label, str(local_date), str(local_time)[:5], tz, fire_at,
             PENDING, "", stamp, stamp))
        return self.get(user_id, rid)

    def list(self, user_id: str) -> list[dict]:
        return [_public(r) for r in self.db.query(
            "SELECT * FROM reminders WHERE user_id = ?"
            " ORDER BY fire_at ASC, created_at ASC", (user_id,))]

    def get(self, user_id: str, reminder_id: str) -> dict | None:
        row = self.db.query_one(
            "SELECT * FROM reminders WHERE id = ? AND user_id = ?", (reminder_id, user_id))
        return _public(row) if row else None

    def update(self, user_id: str, reminder_id: str, *, label=None, local_date=None,
               local_time=None, tz=None, now: datetime | None = None) -> dict | None:
        """Change the text or the time of a reminder that has not fired yet.

        Only a PENDING reminder can be edited. One that has fired, failed or
        been cancelled is a record of what happened, and editing it would
        rewrite that record."""
        row = self.db.query_one(
            "SELECT * FROM reminders WHERE id = ? AND user_id = ?", (reminder_id, user_id))
        if row is None:
            return None
        if row["status"] != PENDING:
            raise ReminderConflict(f"this reminder is {row['status']} and can no longer be "
                                "changed; create a new one instead")
        new_label = row["label"] if label is None else validate_label(label)
        new_date = row["local_date"] if local_date is None else str(local_date)
        new_time = row["local_time"] if local_time is None else str(local_time)[:5]
        new_tz = row["timezone"] if tz is None else tz
        fire_at = self._instant(new_date, new_time, new_tz, now=now)
        self.db.execute(
            "UPDATE reminders SET label = ?, local_date = ?, local_time = ?, timezone = ?,"
            " fire_at = ?, updated_at = ? WHERE id = ? AND user_id = ? AND status = ?",
            (new_label, new_date, new_time, new_tz, fire_at, now_iso(),
             reminder_id, user_id, PENDING))
        return self.get(user_id, reminder_id)

    def cancel(self, user_id: str, reminder_id: str) -> dict | None:
        """Cancel, not delete: the row stays so the learner's list can show it
        was cancelled rather than silently shrinking. Erasing the account still
        removes it -- the table carries `user_id`, so `accounts.erase` finds
        it the day it exists."""
        row = self.db.query_one(
            "SELECT status FROM reminders WHERE id = ? AND user_id = ?",
            (reminder_id, user_id))
        if row is None:
            return None
        if row["status"] != PENDING:
            raise ReminderConflict(f"this reminder is already {row['status']}")
        self.db.execute(
            "UPDATE reminders SET status = ?, updated_at = ? WHERE id = ? AND user_id = ?"
            " AND status = ?", (CANCELLED, now_iso(), reminder_id, user_id, PENDING))
        return self.get(user_id, reminder_id)

    # ---------- firing ------------------------------------------------------

    def due(self, *, at: datetime | None = None) -> list[dict]:
        moment = (at or datetime.now(timezone.utc)).strftime(_ISO)
        return [dict(r) for r in self.db.query(
            "SELECT * FROM reminders WHERE status = ? AND fire_at <= ?"
            " ORDER BY fire_at ASC", (PENDING, moment))]

    def fire(self, row: dict, *, at: datetime | None = None) -> dict:
        """Claim one due reminder, then hand its label to the sender.

        The claim is a conditional UPDATE: only the run that moves it out of
        PENDING sends it. See the module docstring for the at-most-once
        trade-off that follows."""
        stamp = (at or datetime.now(timezone.utc)).strftime(_ISO)
        claimed = self.db.execute(
            "UPDATE reminders SET status = ?, fired_at = ?, updated_at = ?"
            " WHERE id = ? AND status = ?",
            (FIRED, stamp, now_iso(), row["id"], PENDING)).rowcount
        if claimed != 1:
            return {"id": row["id"], "ok": False, "detail": "already claimed"}

        # The label exactly as the learner wrote it. Nothing is added to it,
        # summarised from it or inferred about it.
        payload = {"user_id": row["user_id"], "reminder_id": row["id"],
                   "label": row["label"], "fire_at": row["fire_at"]}
        ok, detail = True, "sent"
        if self.sender is None:
            ok, detail = False, NO_SENDER
        else:
            try:
                ok = bool(self.sender(payload))
                detail = "sent" if ok else "the sender declined it"
            except Exception as exc:            # a sender must not stop the run
                ok, detail = False, f"{type(exc).__name__}: {exc}"
        if not ok:
            self.db.execute(
                "UPDATE reminders SET status = ?, detail = ?, updated_at = ? WHERE id = ?",
                (FAILED, detail[:500], now_iso(), row["id"]))
        else:
            self.db.execute("UPDATE reminders SET detail = ? WHERE id = ?",
                            (detail, row["id"]))
        return {"id": row["id"], "ok": ok, "detail": detail}

    def run_due(self, *, at: datetime | None = None) -> dict:
        results = [self.fire(r, at=at) for r in self.due(at=at)]
        return {"due": len(results),
                "sent": sum(1 for r in results if r["ok"]),
                "failed": sum(1 for r in results if not r["ok"]
                              and r["detail"] != "already claimed")}
