"""
Free-plan abuse signals: layered, weighted, and deliberately slow to punish.

The constraint that shapes everything here is one you already stated: hostels,
colleges, mobile carriers and families share IP addresses. A medical student
in a hostel is the CORE user, and the naive defence -- rate-limit or ban by IP
-- would hit exactly them while barely inconveniencing someone running twenty
accounts from a VPS.

So no single signal blocks anyone. Signals accumulate into a score, the score
maps to a graduated response, and the response at low confidence is friction
rather than denial:

    signals -> weighted score -> ALLOW | VERIFY | THROTTLE | REVIEW | BLOCK
                                          ^^^^^^^^^^^^^^^^
                                       recoverable by a real user
                                       without contacting anyone

BLOCK is reserved for evidence that does not have an innocent reading, and
even then it is recorded with the signals that produced it, so a support
agent can see WHY rather than being told a number.

WHAT THIS DOES NOT DO
---------------------
No device fingerprinting beyond an opaque client-supplied id, no third-party
data, no cross-site tracking. The signals are all things Quintek already knows
because a user gave them to it or generated them by using the product.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

ALLOW, VERIFY, THROTTLE, REVIEW, BLOCK = ("ALLOW", "VERIFY", "THROTTLE", "REVIEW", "BLOCK")


@dataclass(frozen=True)
class Signal:
    """
    One observation and what it is worth.

    `weight` is deliberately small for anything an ordinary user can trip.
    `innocent_reading` is not documentation -- it is shown to whoever reviews
    a flagged account, because every signal here has one and forgetting that
    is how a hostel full of students gets banned.
    """

    key: str
    weight: int
    description: str
    innocent_reading: str


SIGNALS: dict[str, Signal] = {
    "email_unverified": Signal(
        "email_unverified", 15, "email address has not been confirmed",
        "they have not opened the email yet, which is extremely common"),
    "disposable_email": Signal(
        "disposable_email", 25, "email is from a known disposable provider",
        "some people use forwarding services for privacy"),
    "account_minutes_old": Signal(
        "account_minutes_old", 10, "account was created minutes ago",
        "every legitimate account is new once"),
    "shared_ip_many_accounts": Signal(
        # Low on purpose. This is the hostel signal.
        "shared_ip_many_accounts", 8,
        "many accounts have been seen from this network",
        "a hostel, college, library or carrier NAT shares one address between "
        "hundreds of unrelated people"),
    "many_accounts_one_device": Signal(
        # Much stronger: a device id is not shared the way an IP is.
        "many_accounts_one_device", 30,
        "several accounts share one device identifier",
        "a shared family tablet, or a device someone reset"),
    "burst_generation": Signal(
        "burst_generation", 20, "allowance consumed in an unusually short window",
        "revision before an exam is genuinely bursty"),
    "immediate_exhaustion": Signal(
        "immediate_exhaustion", 15,
        "the daily cap was reached within minutes of signing up",
        "an eager new user who wanted to see what it does"),
    "no_attempts_recorded": Signal(
        # Generating without ever answering is the clearest scraping shape:
        # the questions are the product being taken, not used.
        "no_attempts_recorded", 25,
        "questions generated but never answered",
        "someone generating a set to study later, or offline"),
    "repeated_identical_requests": Signal(
        "repeated_identical_requests", 20,
        "the same generation request repeated many times",
        "retrying after a failure, or not realising it worked"),
}

# Score thresholds. Wide bands at the bottom so ordinary users stay in ALLOW
# or VERIFY, and BLOCK needs several independent signals rather than one loud
# one -- the heaviest single signal is 30, so nothing alone reaches 80.
THRESHOLDS = ((80, BLOCK), (60, REVIEW), (40, THROTTLE), (20, VERIFY))

RESPONSES = {
    ALLOW: "no action",
    VERIFY: "ask the learner to confirm their email before generating more",
    THROTTLE: "reduce the free daily cap temporarily; the account keeps working",
    REVIEW: "queue for a human to look at; the account keeps working meanwhile",
    BLOCK: "refuse generation and require support contact",
}


@dataclass
class Assessment:
    user_id: str
    score: int
    action: str
    triggered: list[Signal] = field(default_factory=list)
    throttle_to: int | None = None

    @property
    def blocking(self) -> bool:
        return self.action == BLOCK

    def as_dict(self) -> dict:
        return {
            "user_id": self.user_id, "score": self.score, "action": self.action,
            "response": RESPONSES[self.action],
            "throttle_to": self.throttle_to,
            "signals": [{"key": s.key, "weight": s.weight, "description": s.description,
                         "innocent_reading": s.innocent_reading} for s in self.triggered],
            # Shown to whoever reviews the account, so a decision is made on
            # evidence rather than on a number.
            "review_note": ("Every signal above has an innocent explanation. Weigh them "
                            "together, not individually."),
        }


class AbuseAssessor:
    """
    Scores an account from what Quintek already knows about it.

    Paid accounts are not assessed at all: someone who paid has given a far
    stronger identity signal than any heuristic here, and putting a payer
    through abuse friction is a worse outcome than the abuse.
    """

    def __init__(self, conn: sqlite3.Connection, *, free_throttle_to: int = 5):
        self.conn = conn
        self.free_throttle_to = free_throttle_to

    def assess(self, user_id: str, *, observations: dict | None = None,
               plan_family: str = "free") -> Assessment:
        if plan_family != "free":
            return Assessment(user_id, 0, ALLOW)

        observations = observations or {}
        triggered = [SIGNALS[key] for key, present in observations.items()
                     if present and key in SIGNALS]
        score = sum(signal.weight for signal in triggered)

        action = ALLOW
        for threshold, candidate in THRESHOLDS:
            if score >= threshold:
                action = candidate
                break

        return Assessment(
            user_id=user_id, score=score, action=action, triggered=triggered,
            throttle_to=self.free_throttle_to if action == THROTTLE else None)

    # ---------- observations Quintek can make on its own ----------

    def observe(self, user_id: str, *, account_created_at: str | None = None,
                email_verified: bool = True, device_id: str = "",
                ip_hash: str = "", at: datetime | None = None) -> dict:
        """
        Gather the signals derivable from Quintek's own tables.

        Everything here comes from usage the account itself generated. No
        external lookup, no third-party data.
        """
        moment = at or datetime.now(timezone.utc)
        stamp = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
        day = moment.strftime("%Y-%m-%d")
        observations: dict[str, bool] = {"email_unverified": not email_verified}

        if account_created_at:
            try:
                created = datetime.strptime(account_created_at, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc)
                observations["account_minutes_old"] = (moment - created) < timedelta(minutes=30)
            except ValueError:
                pass

        used_today = self.conn.execute(
            "SELECT COALESCE(SUM(question_units),0) AS n FROM usage_ledger"
            " WHERE user_id=? AND usage_date=?", (user_id, day)).fetchone()["n"]

        if used_today:
            window = (moment - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
            recent = self.conn.execute(
                "SELECT COALESCE(SUM(question_units),0) AS n FROM usage_ledger"
                " WHERE user_id=? AND created_at >= ?", (user_id, window)).fetchone()["n"]
            observations["burst_generation"] = recent >= max(1, used_today)

        if device_id:
            shared = self.conn.execute(
                "SELECT COUNT(DISTINCT user_id) AS n FROM abuse_observations"
                " WHERE device_id=? AND device_id != ''", (device_id,)).fetchone()["n"]
            observations["many_accounts_one_device"] = shared >= 4

        if ip_hash:
            shared = self.conn.execute(
                "SELECT COUNT(DISTINCT user_id) AS n FROM abuse_observations"
                " WHERE ip_hash=? AND ip_hash != ''", (ip_hash,)).fetchone()["n"]
            # High threshold, low weight. A carrier NAT can legitimately carry
            # hundreds; this only fires well past what a household explains.
            observations["shared_ip_many_accounts"] = shared >= 25

        self.conn.execute(
            "INSERT INTO abuse_observations (user_id, device_id, ip_hash, seen_at)"
            " VALUES (?,?,?,?) ON CONFLICT(user_id, device_id, ip_hash) DO UPDATE SET"
            " seen_at=excluded.seen_at",
            (user_id, device_id, ip_hash, stamp))
        self.conn.commit()
        return observations

    def effective_daily_limit(self, plan_daily_limit: int,
                              assessment: Assessment) -> int:
        """
        The daily cap after any throttle. Never zero for a non-BLOCK action.

        A throttled account keeps working -- that is the difference between
        friction and denial, and the reason a false positive here is
        recoverable without a support ticket.
        """
        if assessment.action == BLOCK:
            return 0
        if assessment.throttle_to is not None:
            return min(plan_daily_limit, assessment.throttle_to)
        return plan_daily_limit
