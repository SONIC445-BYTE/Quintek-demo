"""
The daily revision trigger.

The product requirement is specific: the learner picks one time, and at that
time every day the system interrupts them. Three rules follow from it, and each
is enforced here rather than assumed:

  * **The system never changes the chosen time.** Nothing in this module
    reschedules, snoozes, or "optimises" a trigger. If a send fails, the next
    one is still at the time the learner chose. A revision habit is built on a
    fixed hour, and an app that quietly moves it is not helping.

  * **The notification does not start a session.** It says the queue is ready.
    When to actually revise stays the learner's decision, so `due_count` is
    computed and delivered, and nothing else happens.

  * **A missed day is not silently swallowed.** Every firing is written to
    `notification_log` with its status, so "did it actually send" is
    answerable. A trigger that fails quietly is indistinguishable from a
    learner ignoring it.

Timezones are real here. A learner in IST who sets 20:00 means 20:00 where they
are, and computing that in UTC-with-an-offset breaks twice a year. `zoneinfo`
is stdlib, so this costs nothing.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .db import Database, new_id, now_iso

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class NotificationError(ValueError):
    pass


def validate_time(value: str) -> str:
    if not _TIME_RE.match(value or ""):
        raise NotificationError(f"trigger time must be HH:MM in 24-hour form, got {value!r}")
    return value


def validate_timezone(name: str) -> str:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        raise NotificationError(f"unknown timezone: {name!r}")
    return name


def next_occurrence(trigger_time: str, tz_name: str, *, after: datetime | None = None) -> datetime:
    """
    The next moment `trigger_time` occurs in `tz_name`, as UTC.

    Computed in the learner's own zone and then converted, rather than by
    adding a fixed offset to UTC -- an offset is wrong for half the year
    anywhere that observes daylight saving.
    """
    validate_time(trigger_time)
    tz = ZoneInfo(validate_timezone(tz_name))
    now = (after or datetime.now(timezone.utc)).astimezone(tz)
    hour, minute = (int(p) for p in trigger_time.split(":"))
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


class NotificationService:
    """
    Owns preferences and firing. `sender(payload) -> bool` is injected so the
    delivery channel (push, email, a test double) is not this module's problem.
    """

    def __init__(self, db: Database, *, sender=None):
        self.db = db
        self.sender = sender

    # ---------- preferences ----------

    def get_prefs(self, user_id: str) -> dict:
        row = self.db.query_one("SELECT * FROM notification_prefs WHERE user_id = ?", (user_id,))
        if row is None:
            self.db.execute("INSERT INTO notification_prefs (user_id) VALUES (?)", (user_id,))
            row = self.db.query_one("SELECT * FROM notification_prefs WHERE user_id = ?",
                                    (user_id,))
        prefs = dict(row)
        prefs["push_enabled"] = bool(prefs["push_enabled"])
        prefs["email_enabled"] = bool(prefs["email_enabled"])
        return prefs

    def set_prefs(self, user_id: str, *, trigger_time: str | None = None,
                  tz: str | None = None, push: bool | None = None,
                  email: bool | None = None, note: str | None = None) -> dict:
        current = self.get_prefs(user_id)
        trigger_time = validate_time(trigger_time) if trigger_time is not None else current[
            "trigger_time"]
        tz = validate_timezone(tz) if tz is not None else current["timezone"]

        self.db.execute(
            "UPDATE notification_prefs SET trigger_time=?, timezone=?, push_enabled=?,"
            " email_enabled=?, note_text=?, next_scheduled_at=? WHERE user_id=?",
            (trigger_time, tz,
             int(current["push_enabled"] if push is None else push),
             int(current["email_enabled"] if email is None else email),
             current["note_text"] if note is None else note,
             next_occurrence(trigger_time, tz).strftime("%Y-%m-%dT%H:%M:%SZ"),
             user_id))
        return self.get_prefs(user_id)

    # ---------- firing ----------

    def due_users(self, *, at: datetime | None = None) -> list[str]:
        moment = (at or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = self.db.query(
            "SELECT user_id FROM notification_prefs WHERE next_scheduled_at IS NOT NULL"
            " AND next_scheduled_at <= ? AND (push_enabled = 1 OR email_enabled = 1)",
            (moment,))
        return [r["user_id"] for r in rows]

    def fire(self, user_id: str, *, at: datetime | None = None) -> dict:
        """
        Send one learner's trigger and record the outcome.

        The next firing is always recomputed from the learner's chosen time,
        never from when this one happened to run -- a delayed send must not
        drag the schedule with it.
        """
        from .knowledge import KnowledgeStore

        prefs = self.get_prefs(user_id)
        due_count = KnowledgeStore(self.db).due_count(user_id)
        scheduled = prefs["next_scheduled_at"] or now_iso()

        payload = {
            "user_id": user_id,
            "due_count": due_count,
            "note": prefs["note_text"],
            # Says the queue is ready; does not start it.
            "message": (f"Your Quintek revision is ready — {due_count} due."
                        if due_count else "Your Quintek revision is ready."),
            "channels": [c for c, on in (("push", prefs["push_enabled"]),
                                         ("email", prefs["email_enabled"])) if on],
        }

        ok, detail = True, "sent"
        if self.sender is None:
            ok, detail = False, "no notification sender is configured"
        else:
            try:
                ok = bool(self.sender(payload))
                detail = "sent" if ok else "sender declined"
            except Exception as exc:
                ok, detail = False, f"{type(exc).__name__}: {exc}"

        for channel in payload["channels"] or ["none"]:
            self.db.execute(
                "INSERT INTO notification_log (id, user_id, scheduled_at, sent_at, channel,"
                " status, detail, due_count) VALUES (?,?,?,?,?,?,?,?)",
                (new_id("ntf"), user_id, scheduled, now_iso() if ok else None, channel,
                 "sent" if ok else "failed", detail, due_count))

        self.db.execute(
            "UPDATE notification_prefs SET last_status=?, last_sent_at=?, next_scheduled_at=?"
            " WHERE user_id=?",
            ("sent" if ok else "failed", now_iso() if ok else prefs["last_sent_at"],
             next_occurrence(prefs["trigger_time"], prefs["timezone"], after=at)
             .strftime("%Y-%m-%dT%H:%M:%SZ"),
             user_id))
        return {"ok": ok, "detail": detail, **payload}

    def run_due(self, *, at: datetime | None = None) -> dict:
        results = [self.fire(uid, at=at) for uid in self.due_users(at=at)]
        return {"fired": len(results),
                "sent": sum(1 for r in results if r["ok"]),
                "failed": sum(1 for r in results if not r["ok"])}

    def history(self, user_id: str, limit: int = 30) -> list[dict]:
        return [dict(r) for r in self.db.query(
            "SELECT * FROM notification_log WHERE user_id = ? ORDER BY scheduled_at DESC"
            " LIMIT ?", (user_id, limit))]
