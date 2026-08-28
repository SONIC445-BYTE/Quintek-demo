"""
Model Registry.

A candidate is a complete configuration, not a model name -- see
docs/CANDIDATE_DEFINITION.md. This module is the persistent store for
candidates and enforces their lifecycle so that "the router picked an
eligible model" is a claim backed by a state machine, not a comment.

Lifecycle (section 10 of the architecture spec this implements):

    REGISTERED -> BENCHMARK_REQUIRED -> EVALUATING -> ELIGIBLE -> PRODUCTION
                                                    \\-> FAILED
    any non-terminal state -> DEPRECATED

DEPRECATED is reachable from anywhere because a provider can withdraw a
model at any point, including while it is sitting here REGISTERED and
unevaluated. That is an exit, not a promotion: the route INTO ELIGIBLE is
unchanged and still runs only through EVALUATING.

Only ELIGIBLE or PRODUCTION candidates may ever be returned by the router
(benchmark/router.py) -- this module is where that rule is structurally
enforced: `Registry.eligible_candidates()` filters on status, and
`Registry.transition()` rejects any transition that isn't in
VALID_TRANSITIONS, so "mark it eligible without running the benchmark"
isn't a typo away.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path


class Status:
    REGISTERED = "REGISTERED"
    BENCHMARK_REQUIRED = "BENCHMARK_REQUIRED"
    EVALUATING = "EVALUATING"
    ELIGIBLE = "ELIGIBLE"
    PRODUCTION = "PRODUCTION"
    DEPRECATED = "DEPRECATED"
    FAILED = "FAILED"


ALL_STATUSES = {
    Status.REGISTERED, Status.BENCHMARK_REQUIRED, Status.EVALUATING,
    Status.ELIGIBLE, Status.PRODUCTION, Status.DEPRECATED, Status.FAILED,
}

# Only these transitions are legal. A candidate cannot skip straight from
# REGISTERED to PRODUCTION, and nothing can leave FAILED except re-
# registration as a new candidate (a failed candidate is not "retried" in
# place -- see docs/CANDIDATE_DEFINITION.md: a materially different config
# is a different candidate).
#
# DEPRECATED is reachable from every non-terminal state, and only DEPRECATED
# is. A provider can withdraw a model at any point in its life -- NVIDIA
# retired two on 2026-08-26 while they sat here REGISTERED -- and a registry
# with nowhere to put that fact either keeps offering a model that no longer
# exists or has the row deleted, which loses the history. Neither is
# acceptable, and neither was avoidable before this line existed.
#
# This widens only the exit. The guarded transition is the one INTO
# ELIGIBLE, which still comes solely from EVALUATING: nothing here makes it
# one typo easier to mark a candidate eligible without a benchmark run.
VALID_TRANSITIONS: dict[str, set[str]] = {
    Status.REGISTERED: {Status.BENCHMARK_REQUIRED, Status.DEPRECATED},
    Status.BENCHMARK_REQUIRED: {Status.EVALUATING, Status.DEPRECATED},
    Status.EVALUATING: {Status.ELIGIBLE, Status.FAILED, Status.DEPRECATED},
    Status.ELIGIBLE: {Status.PRODUCTION, Status.DEPRECATED, Status.EVALUATING},
    Status.PRODUCTION: {Status.DEPRECATED, Status.EVALUATING},
    Status.DEPRECATED: set(),
    Status.FAILED: set(),
}


@dataclass
class ModelCandidate:
    candidate_id: str
    provider: str
    model_id: str
    model_version: str
    status: str = Status.REGISTERED
    capabilities: list[str] = field(default_factory=list)
    context_window: int | None = None
    configuration: dict = field(default_factory=dict)
    prompt_version: str = ""
    retrieval_version: str = ""
    created_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    updated_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ModelCandidate":
        return cls(**d)


class Registry:
    """
    JSON-file-backed store. Every mutating call rewrites the file
    atomically (write to a temp file, then rename) so a crash mid-write
    can't leave a half-written registry -- the registry is small,
    infrequently written, and this is cheaper than a database for what it
    is.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._candidates: dict[str, ModelCandidate] = {}
        if self.path.exists():
            self._load()

    def _load(self) -> None:
        data = json.loads(self.path.read_text())
        self._candidates = {
            cid: ModelCandidate.from_dict(d) for cid, d in data.items()
        }

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {cid: c.as_dict() for cid, c in self._candidates.items()}, indent=2,
        ))
        tmp.replace(self.path)

    def register(
        self, provider: str, model_id: str, model_version: str, *,
        capabilities: list[str] | None = None, context_window: int | None = None,
        configuration: dict | None = None, prompt_version: str = "",
        retrieval_version: str = "", candidate_id: str | None = None,
    ) -> ModelCandidate:
        """
        candidate_id defaults to a hash of the identity fields from
        docs/CANDIDATE_DEFINITION.md, so registering the same configuration
        twice yields the same candidate rather than a duplicate.
        """
        cid = candidate_id or self._derive_candidate_id(
            provider, model_id, model_version, prompt_version, retrieval_version,
            configuration or {},
        )
        if cid in self._candidates:
            return self._candidates[cid]
        c = ModelCandidate(
            candidate_id=cid, provider=provider, model_id=model_id,
            model_version=model_version, capabilities=capabilities or [],
            context_window=context_window, configuration=configuration or {},
            prompt_version=prompt_version, retrieval_version=retrieval_version,
        )
        self._candidates[cid] = c
        self._save()
        return c

    @staticmethod
    def _derive_candidate_id(provider, model_id, model_version, prompt_version,
                              retrieval_version, configuration) -> str:
        import hashlib
        key = json.dumps({
            "provider": provider, "model_id": model_id, "model_version": model_version,
            "prompt_version": prompt_version, "retrieval_version": retrieval_version,
            "configuration": configuration,
        }, sort_keys=True)
        return "cand-" + hashlib.sha256(key.encode()).hexdigest()[:16]

    def get(self, candidate_id: str) -> ModelCandidate | None:
        return self._candidates.get(candidate_id)

    def all(self) -> list[ModelCandidate]:
        return list(self._candidates.values())

    def by_status(self, status: str) -> list[ModelCandidate]:
        return [c for c in self._candidates.values() if c.status == status]

    def eligible_candidates(self) -> list[ModelCandidate]:
        """The only two statuses the router may ever select from."""
        return [c for c in self._candidates.values()
               if c.status in (Status.ELIGIBLE, Status.PRODUCTION)]

    def with_capability(self, capability: str) -> list[ModelCandidate]:
        return [c for c in self.eligible_candidates() if capability in c.capabilities]

    def transition(self, candidate_id: str, new_status: str) -> ModelCandidate:
        if new_status not in ALL_STATUSES:
            raise ValueError(f"unknown status '{new_status}'")
        c = self._candidates.get(candidate_id)
        if c is None:
            raise KeyError(f"no candidate '{candidate_id}'")
        allowed = VALID_TRANSITIONS.get(c.status, set())
        if new_status not in allowed:
            raise ValueError(
                f"illegal transition {c.status} -> {new_status} for '{candidate_id}'; "
                f"allowed from {c.status}: {sorted(allowed) or '(terminal state)'}"
            )
        c.status = new_status
        c.updated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._save()
        return c
