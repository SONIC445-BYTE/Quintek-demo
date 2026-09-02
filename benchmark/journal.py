"""
A durable record of every model reply, so an interrupted run resumes instead
of starting over.

WHY THIS EXISTS
---------------
Phase 0 attempt 2 ran for 118 minutes against a frozen configuration and was
destroyed by a container restart. Nothing was recoverable: the runner flushes
at experiment boundaries, so an arm that has not finished has written nothing.
The API spend was real and the evidence was gone. A third attempt that is
merely a fourth roll of the same dice is not a plan.

So the reply to every request is written down, fsynced, before it is used.
A resumed invocation replays what is on disk and pays only for what was never
asked.

WHY THIS IS NOT A CHANGE TO THE EXPERIMENT
------------------------------------------
This module is deliberately outside `validator/` and `benchmark/providers/`,
the two trees `validator.holdout.validator_fingerprint` hashes. It does not
alter a prompt, a threshold, which layers run, how a reply is unpacked, or
what is scored. A resumed run recomputes its verdicts through the real
pipeline from the real replies; nothing is rehydrated from a summary, because
a lossily reconstructed verdict is fabricated evidence.

THE THREE RULES THAT KEEP A RESUME HONEST
-----------------------------------------
1. AN OUTAGE REPLAYS AS AN OUTAGE. A recorded failure is handed back as the
   same failed response and the model is NOT called again. Re-asking only the
   questions that failed, and keeping the answers if they come back better, is
   selective retry -- the single most effective way to manufacture a score
   that will not reproduce.

2. EACH ARM PAYS FOR ITS OWN CALLS. The key includes the arm, so a reply
   recorded for A+B+D is never served to A+B+C+D even when the request is
   byte-identical. Sharing would remove between-arm sampling variation from
   `ABCD - ABD`, which is a different and quieter experiment than the frozen
   one. Cheaper is not the objective.

3. THE CEILINGS BOUND THE SET, NOT THE INVOCATION. Spend and elapsed time are
   carried across resumes, so the frozen `budget_max_calls` and
   `max_wall_minutes` mean what the freeze says they mean. Resetting the
   counters at each restart would turn a 2400-attempt ceiling into 2400 per
   crash.

4. A REQUEST THAT NEVER REACHED THE PROVIDER IS NOT WRITTEN DOWN. Rule 1
   makes a recorded outage permanent, which is right for an outage that is an
   observation of the run and catastrophic for one that is not. "Connection
   refused" at 0.1s while this container's network is being torn down is not
   the model failing to answer; it is us failing to ask. Freezing those into
   the journal would make a fact about the harness permanent as a fact about
   the model.

   The line between the two is drawn narrowly, by `ProviderStatus.UNREACHED`:
   only failures that PROVE the TCP connection never came up. A timeout is
   ambiguous -- the request may have arrived -- so a timeout is recorded. The
   conservative direction is always to record.

   Rows already written under an earlier rule are skipped at load rather than
   deleted, so the file remains a complete and auditable account of what
   happened, and the rule that filters it is the one under test.

A replay makes no outbound attempt, so it is not charged: the journal wraps
the metered provider from outside, and on a hit the meter is never reached.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from benchmark.provider_status import ProviderStatus, classify
from benchmark.providers.base import GenerationResponse

#: Bumped when a recorded line stops meaning what it meant. A journal written
#: under a different version is refused rather than reinterpreted.
JOURNAL_VERSION = "journal/1.0.0"


class JournalMismatch(RuntimeError):
    """The journal on disk belongs to a different run than the one starting."""


def reached_provider(response) -> bool:
    """
    Did this request actually get to the provider?

    A success obviously did. A failure did unless it proves otherwise: the
    burden is on the evidence to show the connection never came up, so
    anything ambiguous counts as reached and is recorded.
    """
    error = getattr(response, "error", None) if not isinstance(response, dict) \
        else response.get("error")
    if not error:
        return True
    return classify(error=error) != ProviderStatus.UNREACHED


def key_for(*, arm: str, role: str, model: str, request) -> str:
    """
    Identity of one request, exactly.

    Every input that could change the reply is in here, so a prompt that has
    drifted by a character misses and is paid for. The failure mode this
    forecloses is the expensive one: silently serving a cached answer to a
    question nobody asked.
    """
    payload = {
        "arm": arm, "role": role, "model": model,
        "item_id": request.item_id,
        "system": request.system,
        "prompt": request.prompt,
        "max_tokens": request.max_tokens,
        "temperature": request.temperature,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass
class Journal:
    """An append-only log of replies, plus the running cost of producing them."""

    path: Path
    freeze: str
    replies: dict = field(default_factory=dict)
    spent: Counter = field(default_factory=Counter)
    elapsed_seconds: float = 0.0
    replayed: int = 0
    recorded: int = 0
    unreached: int = 0
    skipped_unreached: int = 0

    @classmethod
    def open(cls, path, freeze: str) -> "Journal":
        """
        Load a journal, or start one.

        Refuses a journal written under a different freeze or a different
        journal version. Continuing across a configuration change would splice
        two experiments together and report the join as one.
        """
        path = Path(path)
        journal = cls(path=path, freeze=freeze)
        if not path.exists():
            return journal
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                # A journal truncated mid-write by the kill it exists to
                # survive. The last line is the only one that can be partial,
                # and dropping it costs one call.
                break
            if row.get("version") != JOURNAL_VERSION:
                raise JournalMismatch(
                    f"{path} line {number} was written by {row.get('version')!r}, but this "
                    f"is {JOURNAL_VERSION}. A line whose meaning may have changed is not "
                    "replayed.")
            if row.get("freeze") != freeze:
                raise JournalMismatch(
                    # Both digests in full: truncating them is how two
                    # different configurations print as the same string and a
                    # real mismatch reads as a nonsense message.
                    f"{path} was recorded under frozen configuration "
                    f"{row.get('freeze')!r}, and this run is {freeze!r}. "
                    "Resuming across a refreeze would join two different experiments and "
                    "report the seam as one measurement. Start a new journal.")
            if not reached_provider(row["response"]):
                # Written under an earlier rule, or by an older version. Left
                # in the file and skipped here, so nothing is destroyed and
                # the item is simply asked for the first time.
                journal.skipped_unreached += 1
                continue
            journal.replies[row["key"]] = row["response"]
            journal.spent = Counter(row.get("spent") or {})
            journal.elapsed_seconds = float(row.get("elapsed_seconds") or 0.0)
        return journal

    def get(self, key: str) -> dict | None:
        return self.replies.get(key)

    def record(self, key: str, response: GenerationResponse, *,
               spent: Counter, elapsed_seconds: float) -> None:
        """
        Write one reply down and make it survive a kill.

        The flush and fsync are the point of the whole module: a reply held in
        a buffer when the container restarts is a reply that was paid for and
        lost.
        """
        if not reached_provider(response):
            # Not an observation of the model. Deliberately left absent, so a
            # resumed run asks this item for the first time rather than
            # replaying our own network failure as the model's silence.
            self.unreached += 1
            return
        row = {"version": JOURNAL_VERSION, "freeze": self.freeze, "key": key,
               "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "response": response.as_dict(),
               "spent": dict(spent), "elapsed_seconds": round(elapsed_seconds, 2)}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self.replies[key] = row["response"]
        self.spent = Counter(dict(spent))
        self.elapsed_seconds = elapsed_seconds
        self.recorded += 1

    def summary(self) -> str:
        total = self.replayed + self.recorded
        unreached = ""
        if self.unreached or self.skipped_unreached:
            unreached = (f"; {self.unreached} never reached the provider and were not "
                         f"recorded, {self.skipped_unreached} such rows skipped at load")
        return (f"journal {self.path}: {self.replayed} replayed, {self.recorded} newly "
                f"recorded, {total} total; carried spend {dict(self.spent)}, carried "
                f"elapsed {self.elapsed_seconds / 60.0:.1f} min{unreached}")


class JournalledProvider:
    """
    A provider that writes down what it hears and never asks twice.

    Wraps the METERED provider from outside, so a replayed reply costs nothing
    against the budget -- it makes no outbound attempt, and charging it would
    make the frozen ceiling drift downward with every resume.
    """

    def __init__(self, inner, journal: Journal, *, arm: str, role: str,
                 budget=None, clock=None):
        self._inner = inner
        self._journal = journal
        self._arm = arm
        self._role = role
        self._budget = budget
        self._clock = clock

    def __getattr__(self, name):
        # model, model_version, model_family, is_model, is_oracle, name: the
        # wrapper must be indistinguishable from what it wraps to everything
        # that describes or records a run.
        return getattr(self._inner, name)

    def generate(self, request) -> GenerationResponse:
        key = key_for(arm=self._arm, role=self._role,
                      model=getattr(self._inner, "model", ""), request=request)
        hit = self._journal.get(key)
        if hit is not None:
            self._journal.replayed += 1
            # Replayed verbatim, error and all. A recorded outage is an
            # observation about this run; asking again until it answers is how
            # a failure rate quietly becomes zero.
            return GenerationResponse(**hit)
        response = self._inner.generate(request)
        self._journal.record(key, response, spent=self._spent(), elapsed_seconds=self._elapsed())
        return response

    def _spent(self) -> Counter:
        """What the budget has been charged so far, so a resume starts from it."""
        return Counter(dict(getattr(self._budget, "spent", None) or {}))

    def _elapsed(self) -> float:
        """
        Total elapsed seconds for the SET, carried plus this invocation's own.

        `WallClock` is already constructed with its start pushed back by the
        carried time, so its `elapsed_minutes` is the set's, not this
        process's -- which is exactly the number the frozen ceiling bounds.
        """
        if self._clock is None:
            return self._journal.elapsed_seconds
        return self._clock.elapsed_minutes * 60.0
