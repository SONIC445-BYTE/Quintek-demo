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
Five distinct things can stop a layer, and collapsing them loses the
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

MODES = (MODE_CONFIGURATION, MODE_PRECONDITION, MODE_TRANSPORT,
         MODE_RATE_LIMITED, MODE_UNPARSEABLE, MODE_UNUSABLE)

#: Modes where a model was actually asked something. The complement is the set
#: that must NOT be read as evidence about a candidate's reliability.
MODEL_WAS_CALLED = (MODE_TRANSPORT, MODE_RATE_LIMITED, MODE_UNPARSEABLE,
                    MODE_UNUSABLE)

#: Modes that say the RUN was paced wrong rather than that anything is broken.
#: Kept separate so a rerun report can say "this many items were lost to our
#: own rate" without that being read as the host being unreliable.
SELF_INFLICTED = (MODE_RATE_LIMITED,)


class LayerUnavailable(RuntimeError):
    """
    A configured layer did not produce a finding.

    Subclasses set `layer`. `mode` is required and keyword-only: a raise site
    that does not say which of the five happened is the thing this module
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
    for rec in records:
        layer, mode = rec.get("layer", "unknown"), rec.get("mode", "unknown")
        by_layer[layer] = by_layer.get(layer, 0) + 1
        by_mode[mode] = by_mode.get(mode, 0) + 1
        key = f"{layer}/{mode}"
        by_layer_mode[key] = by_layer_mode.get(key, 0) + 1
        if rec.get("model_was_called"):
            called += 1
    return {
        "total": len(records),
        "by_layer": dict(sorted(by_layer.items())),
        "by_mode": dict(sorted(by_mode.items())),
        "by_layer_and_mode": dict(sorted(by_layer_mode.items())),
        "model_was_called": called,
        "model_was_not_called": len(records) - called,
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
