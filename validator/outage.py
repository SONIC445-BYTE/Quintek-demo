"""
What a validator outage was, recorded so the next one is diagnosable.

WHY THIS MODULE EXISTS
----------------------
Phase 0 (D018) terminated INCOMPLETE because arms 1 and 3 could not decide 12
and 14 items. The run artifact recorded `outages: 14` -- a bare integer. The
per-item reasons went to `render()`'s stdout, which printed the first five and
was never persisted, so afterwards it was impossible to say which layer had
failed or why.

D017 was solvable for exactly the opposite reason: a journal had kept every raw
reply, and the cause -- the provider returning the HTTP envelope as
`raw_output` -- was readable straight off it. The difference between a
diagnosable failure and an undiagnosable one was whether the evidence had been
written down.

THE MODES ARE NOT ALL THE MODEL'S FAULT
---------------------------------------
Seven distinct things can stop a layer, and collapsing them loses the
distinction that matters most:

    configuration  a layer was enabled with no provider. Nobody called anything.
    precondition   the ITEM cannot be checked by this layer -- no passage, a
                   malformed key, no declared concept. NO MODEL CALL IS MADE.
                   This is a corpus or Layer-A finding wearing an outage's
                   clothes, and counting it against a model is a mistake.
    transport      the call failed: network, HTTP status, timeout, auth.
    rate_limited   the host refused because we asked too fast. NOT a transport
                   failure: the host is working, our pacing is wrong. Counting
                   it as transport would make a self-inflicted problem look
                   like an unreliable provider, and the two have opposite
                   fixes.
    unparseable    a reply arrived and contained no JSON object.
    unusable       a reply arrived and parsed, but its content was not an
                   answer to the question asked.
    wall_clock     the RUN's own elapsed-time ceiling stopped it before this
                   item was asked. NO MODEL CALL IS MADE.
    budget         the RUN's own call ceiling stopped it before this item was
                   asked. NO MODEL CALL IS MADE.

THE LAST TWO ARE THE SAME MISTAKE AS THE FIRST TWO
---------------------------------------------------
`transport` is the default bucket -- a layer assigns it whenever a call failed
and the failure was not recognised as something else. So every unrecognised
failure is reported as the backend being unwell.

That is how the first Groq run came to record 24 `grounding/transport`
outages. None of them were Groq. The wall-clock ceiling had stopped the run,
`WallClockExceeded` reached the layer as an ordinary failed response, and
items that were never asked were filed as a quarter of the corpus being
dropped by the host. Read cold, that artifact accuses a provider that did
nothing wrong -- the same conflation as counting a `precondition` against a
model, arriving by a different route.

A ceiling stop is the RUN's decision. It belongs with `rate_limited` in
`SELF_INFLICTED`, not with `transport`.

"the model returned replies that did not parse" was the D018 summary. With a
bare count there was no way to check it, and no way to notice if some of those
items had never reached a model at all.
"""

from __future__ import annotations

# A reply is stored whole. It is the evidence, and the last time this project
# truncated model output to be tidy -- max_tokens 1024 -- it cost 52 items.
# The cap here is a guard against a pathological reply, not a summary, and when
# it bites it says so rather than silently shortening.
MAX_STORED_REPLY = 64 * 1024

MODE_CONFIGURATION = "configuration"
MODE_PRECONDITION = "precondition"
MODE_TRANSPORT = "transport"
MODE_RATE_LIMITED = "rate_limited"
MODE_UNPARSEABLE = "unparseable"
MODE_UNUSABLE = "unusable_reply"
MODE_WALL_CLOCK = "wall_clock"
MODE_BUDGET = "budget_exhausted"

MODES = (MODE_CONFIGURATION, MODE_PRECONDITION, MODE_TRANSPORT,
         MODE_RATE_LIMITED, MODE_UNPARSEABLE, MODE_UNUSABLE,
         MODE_WALL_CLOCK, MODE_BUDGET)

#: The run stopped itself. Neither of these is a fact about the host, and an
#: item recorded under one was never asked -- it is missing, not failed.
RUN_CEILING = (MODE_WALL_CLOCK, MODE_BUDGET)

#: Modes where a model was actually asked something. The complement is the set
#: that must NOT be read as evidence about a candidate's reliability.
MODEL_WAS_CALLED = (MODE_TRANSPORT, MODE_RATE_LIMITED, MODE_UNPARSEABLE,
                    MODE_UNUSABLE)

#: Modes that say the RUN was configured or paced wrong rather than that
#: anything is broken. Kept separate so a rerun report can say "this many items
#: were lost to our own rate and our own ceilings" without that being read as
#: the host being unreliable.
SELF_INFLICTED = (MODE_RATE_LIMITED,) + RUN_CEILING


class LayerUnavailable(RuntimeError):
    """
    A configured layer did not produce a finding.

    Subclasses set `layer`. `mode` is required and keyword-only: a raise site
    that does not say which of the modes happened is the thing this module
    exists to prevent, so it fails at the call rather than recording "unknown".
    """

    layer = "unknown"

    def __init__(self, message: str, *, mode: str, item_id: str = "",
                 purpose: str = "", raw_reply: str | None = None,
                 attempts: int | None = None, provider_error: str | None = None):
        super().__init__(message)
        if mode not in MODES:
            raise ValueError(
                f"unknown outage mode {mode!r}; expected one of {', '.join(MODES)}")
        self.mode = mode
        self.item_id = item_id
        self.purpose = purpose
        self.attempts = attempts
        self.provider_error = provider_error
        self.raw_reply, self.reply_truncated = _clip(raw_reply)

    @property
    def model_was_called(self) -> bool:
        return self.mode in MODEL_WAS_CALLED

    def as_dict(self) -> dict:
        return {
            "id": self.item_id,
            "layer": self.layer,
            "mode": self.mode,
            "model_was_called": self.model_was_called,
            "purpose": self.purpose,
            "attempts": self.attempts,
            "provider_error": self.provider_error,
            "raw_reply": self.raw_reply,
            "raw_reply_truncated": self.reply_truncated,
            "raw_reply_chars": len(self.raw_reply) if self.raw_reply is not None else None,
            "error": str(self),
        }


def _clip(raw: str | None) -> tuple[str | None, bool]:
    if raw is None:
        return None, False
    if len(raw) <= MAX_STORED_REPLY:
        return raw, False
    return raw[:MAX_STORED_REPLY], True


def summarise(records: list[dict]) -> dict:
    """
    The split D018 could not report: by layer, by mode, and how many of the
    lost items ever reached a model.

    Kept here rather than in `analysis` because it describes items that have NO
    verdict, and everything in `analysis` is computed from verdicts.
    """
    by_layer: dict[str, int] = {}
    by_mode: dict[str, int] = {}
    by_layer_mode: dict[str, int] = {}
    called = 0
    self_inflicted = 0
    ceiling = 0
    for rec in records:
        layer, mode = rec.get("layer", "unknown"), rec.get("mode", "unknown")
        by_layer[layer] = by_layer.get(layer, 0) + 1
        by_mode[mode] = by_mode.get(mode, 0) + 1
        key = f"{layer}/{mode}"
        by_layer_mode[key] = by_layer_mode.get(key, 0) + 1
        if rec.get("model_was_called"):
            called += 1
        if mode in SELF_INFLICTED:
            self_inflicted += 1
        if mode in RUN_CEILING:
            ceiling += 1
    return {
        "total": len(records),
        "by_layer": dict(sorted(by_layer.items())),
        "by_mode": dict(sorted(by_mode.items())),
        "by_layer_and_mode": dict(sorted(by_layer_mode.items())),
        "model_was_called": called,
        "model_was_not_called": len(records) - called,
        # The split a reader needs FIRST and had to derive by hand: of the
        # items this run lost, how many were the run's own doing. The Groq run
        # lost 33 and 33 were ours -- 9 to our pace, 24 to our ceiling -- and
        # the artifact as it stood read as 24 failures by the host.
        "self_inflicted": self_inflicted,
        "stopped_by_run_ceiling": ceiling,
        "attributable_to_host": len(records) - self_inflicted,
    }


def was_rate_limited(response) -> bool:
    """
    Did this response fail because the host refused our pace?

    Read off the error string rather than the exception type, because by the
    time a layer sees it the provider's retry loop has already flattened the
    exception into `response.error`. `RateLimited` is the type raised inside
    `_call`; this is how it survives the flattening.
    """
    error = getattr(response, "error", None) or ""
    return "RateLimited" in error or "429" in error


def classify_failure(response) -> str:
    """
    Which mode a failed `GenerationResponse` belongs to.

    ONE function rather than a ternary repeated in every layer. The three
    layers previously each wrote

        mode=(MODE_RATE_LIMITED if was_rate_limited(r) else MODE_TRANSPORT)

    which meant every newly-recognised failure had to be added in three places
    and, until it was, fell into `transport` -- the default bucket that reads
    as "the backend is unwell". `WallClockExceeded` was never added to any of
    them, so a run the CEILING stopped was reported as the host dropping a
    quarter of the corpus. Centralised here so the next kind of failure is
    classified everywhere at once, or nowhere.

    Read off the error string for the same reason `was_rate_limited` does: by
    the time a layer sees it, `BaseProvider.generate` has flattened the
    exception into `f"{type(exc).__name__}: {exc}"`, so the type name is the
    prefix and the type itself is gone.

    The ceilings are tested FIRST. They are decisions the run made about
    itself, and a run that stopped on its own terms did not observe anything
    about the host -- so nothing that follows may claim it did.
    """
    error = getattr(response, "error", None) or ""
    if "WallClockExceeded" in error:
        return MODE_WALL_CLOCK
    if "BudgetExhausted" in error:
        return MODE_BUDGET
    if was_rate_limited(response):
        return MODE_RATE_LIMITED
    return MODE_TRANSPORT
