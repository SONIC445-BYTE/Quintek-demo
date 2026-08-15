"""
Orchestration layer: the one place a production call site should ever touch.

    task -> Router.select() -> provider.generate() -> provenance record
                                       |
                                  on failure -> exclude, re-route, retry
                                       |
                                  every attempt logged, nothing silent

This is the piece the actual Quintek product backend (a separate
repository -- this repo is the benchmark harness, see README.md's scope
note) imports instead of calling a provider directly. "No hard-coded model
scattered through the app" only holds if every call site goes through one
orchestrator; this module is that seam.

Every execution -- successful, failed, or one that never found an eligible
candidate -- is appended to `ExecutionLog`, never overwritten, same pattern
as `analytics.RoutingLog` (which this module also writes to, so "why did
Quintek use this model" and "what happened when it did" are both
reconstructable from disk).
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import analytics as an
from .providers.base import GenerationRequest, GenerationResponse, ModelProvider
from .registry import ModelCandidate, Registry
from .router import Router, RoutingPolicy


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class ExecutionRecord:
    execution_id: str
    task_type: str
    candidate_id: str | None
    provider: str | None
    model: str | None
    model_version: str | None
    prompt_version: str
    timestamp: str
    latency_ms: float | None
    input_tokens: int | None
    output_tokens: int | None
    status: str                       # ok | error | no_eligible_candidate
    error: str | None
    routing_policy: str
    fallback: bool = False
    fallback_reason: str | None = None
    attempt_number: int = 1

    def as_dict(self) -> dict:
        return dict(
            execution_id=self.execution_id, task_type=self.task_type,
            candidate_id=self.candidate_id, provider=self.provider, model=self.model,
            model_version=self.model_version, prompt_version=self.prompt_version,
            timestamp=self.timestamp, latency_ms=self.latency_ms,
            input_tokens=self.input_tokens, output_tokens=self.output_tokens,
            status=self.status, error=self.error, routing_policy=self.routing_policy,
            fallback=self.fallback, fallback_reason=self.fallback_reason,
            attempt_number=self.attempt_number,
        )


class ExecutionLog:
    """Append-only JSONL. Never rewrites a prior record -- see module
    docstring and analytics.py's module docstring rule 2."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def record(self, rec: ExecutionRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a") as fh:
            fh.write(json.dumps(rec.as_dict()) + "\n")

    def all(self) -> list[ExecutionRecord]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text().splitlines():
            if line.strip():
                out.append(ExecutionRecord(**json.loads(line)))
        return out

    def for_candidate(self, candidate_id: str) -> list[ExecutionRecord]:
        return [r for r in self.all() if r.candidate_id == candidate_id]


class CallLimiter:
    """
    Rate/concurrency/budget guard for PRODUCTION orchestration calls.
    Deliberately separate from `runner.Budget`, which scopes one benchmark
    RUN -- this scopes live traffic, which is a different lifetime and a
    different cost profile (docs/BUDGET_PROTOCOL.md's "smoke/standard/full"
    modes don't apply to production serving).

    Thread-safe: `acquire`/`release` bound concurrency via a semaphore;
    `check_and_record` is called while holding a slot, under a lock, so
    concurrent callers can't race past the call/token ceiling.
    """

    def __init__(self, max_concurrency: int = 4, max_calls: int | None = None,
                 max_tokens: int | None = None):
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self._semaphore = threading.Semaphore(max_concurrency)
        self.max_calls = max_calls
        self.max_tokens = max_tokens
        self._calls = 0
        self._tokens = 0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        self._semaphore.acquire()

    def release(self) -> None:
        self._semaphore.release()

    def check_and_record(self, tokens_used: int = 0) -> None:
        with self._lock:
            if self.max_calls is not None and self._calls >= self.max_calls:
                raise RuntimeError(
                    f"orchestration call budget exhausted: max_calls={self.max_calls}")
            if self.max_tokens is not None and self._tokens + tokens_used > self.max_tokens:
                raise RuntimeError(
                    f"orchestration token budget exhausted: max_tokens={self.max_tokens}")
            self._calls += 1
            self._tokens += tokens_used

    @property
    def calls_made(self) -> int:
        return self._calls


class Orchestrator:
    """
    `provider_factory(candidate) -> ModelProvider` is supplied by the
    caller rather than hard-coded here, so this module has no opinion about
    NVIDIA specifically -- swapping providers means changing the factory,
    not this class. See providers/nvidia.py for the concrete NVIDIA
    implementation the factory would typically wrap.
    """

    def __init__(
        self, registry: Registry, archive: an.RunArchive,
        provider_factory: Callable[[ModelCandidate], ModelProvider],
        execution_log: ExecutionLog, routing_log: an.RoutingLog,
        call_limiter: CallLimiter | None = None,
    ):
        self.router = Router(registry, archive)
        self.registry = registry
        self.provider_factory = provider_factory
        self.execution_log = execution_log
        self.routing_log = routing_log
        self.call_limiter = call_limiter or CallLimiter()

    def generate(
        self, task, prompt: str, *,
        policy: RoutingPolicy = RoutingPolicy.QUALITY_FIRST,
        system: str = "", max_tokens: int = 1024, temperature: float = 0.0,
        prompt_version: str = "", max_fallbacks: int = 2,
        **router_kwargs,
    ) -> tuple[GenerationResponse | None, ExecutionRecord]:
        tried: set[str] = set()
        is_fallback = False
        fallback_reason: str | None = None
        record = None

        for _ in range(max_fallbacks + 1):
            result = self.router.select(task, policy=policy, exclude=tried, **router_kwargs)
            execution_id = f"exec-{uuid.uuid4().hex[:12]}"

            self.routing_log.record(an.RoutingDecision(
                execution_id=execution_id, task=getattr(task, "value", str(task)),
                selected_candidate=result.selected_candidate or "",
                eligible_candidates=result.eligible_candidates,
                routing_policy=policy.value,
                benchmark_evidence={"scores": result.scores, "reason": result.reason},
                timestamp=_now(), fallback=is_fallback, fallback_reason=fallback_reason,
            ))

            if result.selected_candidate is None:
                record = ExecutionRecord(
                    execution_id=execution_id, task_type=getattr(task, "value", str(task)),
                    candidate_id=None, provider=None, model=None, model_version=None,
                    prompt_version=prompt_version, timestamp=_now(), latency_ms=None,
                    input_tokens=None, output_tokens=None, status="no_eligible_candidate",
                    error=result.reason, routing_policy=policy.value,
                    fallback=is_fallback, fallback_reason=fallback_reason,
                )
                self.execution_log.record(record)
                return None, record

            candidate = self.registry.get(result.selected_candidate)
            provider = self.provider_factory(candidate)

            self.call_limiter.acquire()
            try:
                self.call_limiter.check_and_record()
                response = provider.generate(GenerationRequest(
                    item_id=execution_id, prompt=prompt, system=system,
                    max_tokens=max_tokens, temperature=temperature,
                ))
            except RuntimeError as exc:  # budget exhausted -- do not retry past it
                record = ExecutionRecord(
                    execution_id=execution_id, task_type=getattr(task, "value", str(task)),
                    candidate_id=candidate.candidate_id, provider=candidate.provider,
                    model=candidate.model_id, model_version=candidate.model_version,
                    prompt_version=prompt_version, timestamp=_now(), latency_ms=None,
                    input_tokens=None, output_tokens=None, status="error", error=str(exc),
                    routing_policy=policy.value, fallback=is_fallback,
                    fallback_reason=fallback_reason,
                )
                self.execution_log.record(record)
                return None, record
            finally:
                self.call_limiter.release()

            record = ExecutionRecord(
                execution_id=execution_id, task_type=getattr(task, "value", str(task)),
                candidate_id=candidate.candidate_id, provider=candidate.provider,
                model=candidate.model_id, model_version=candidate.model_version,
                prompt_version=prompt_version, timestamp=_now(), latency_ms=response.latency_ms,
                input_tokens=response.input_tokens, output_tokens=response.output_tokens,
                status="ok" if response.ok else "error", error=response.error,
                routing_policy=policy.value, fallback=is_fallback,
                fallback_reason=fallback_reason, attempt_number=len(tried) + 1,
            )
            self.execution_log.record(record)

            if response.ok:
                return response, record

            # Never silently switch models -- record exactly why, then retry
            # through the router with this candidate excluded.
            tried.add(candidate.candidate_id)
            is_fallback = True
            fallback_reason = f"{candidate.candidate_id} failed: {response.error}"

        return None, record
