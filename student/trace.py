"""
Every intermediate artifact of one generation run, written to disk.

"Question generated successfully" is not evidence. It cannot tell you whether
the model was given the right passage, whether it echoed the passage back as a
stem, whether the validator saw the generator's reasoning, or why a question
was dropped. All of that existed in memory for a few milliseconds and was
discarded.

A run directory holds the whole chain:

    generation_run/<run_id>/
      run.json                  what was asked for, and by what
      source.json               the source and the exact chunks retrieved
      concepts.json             concepts in play, and how each was resolved
      prompt.json               the full prompt, system text, and parameters
      raw_model_output.json     what the model actually returned, untouched
      normalized_question.json  what we made of it, and what we dropped
      validation.json           each check, the validator's identity, verdict
      final_decision.json       stored or not, and why

Three rules, each learned from a specific way this goes wrong:

  * **Raw output is never repaired before it is written.** The bytes on disk
    are the bytes the model returned. Normalization is a separate file, so
    "the model produced valid JSON" and "we coerced it into valid JSON" stay
    distinguishable.

  * **A failed run writes more, not less.** The interesting runs are the ones
    that failed. Every artifact produced before the failure is flushed, and
    the failure is recorded with the stage it happened in.

  * **The validator's identity is recorded next to its verdict.** Judge
    independence is a claim about who did the checking. A verdict without a
    checker identity cannot support that claim.

Artifacts are written incrementally, as each stage completes, so a run killed
half way leaves the stages that did complete.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

TRACE_ROOT_ENV = "QUINTEK_TRACE_ROOT"
DEFAULT_TRACE_ROOT = "generation_run"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def trace_root() -> Path:
    return Path(os.environ.get(TRACE_ROOT_ENV) or DEFAULT_TRACE_ROOT)


@dataclass
class GenerationTrace:
    """
    One run's artifact directory.

    Disabled by default (`enabled=False` writes nothing) so the ordinary
    request path costs nothing, and so tests that do not care about tracing
    are not obliged to clean up after it.
    """

    run_id: str
    root: Path | None = None
    enabled: bool = True
    stages: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.root = Path(self.root) if self.root else (trace_root() / self.run_id)
        if self.enabled:
            self.root.mkdir(parents=True, exist_ok=True)

    # ---------- writing ----------

    def write(self, name: str, payload) -> None:
        if not self.enabled:
            return
        path = self.root / (name if name.endswith(".json") else f"{name}.json")
        path.write_text(json.dumps(payload, indent=2, default=str, ensure_ascii=False),
                        encoding="utf-8")
        stage = path.stem
        if stage not in self.stages:
            self.stages.append(stage)

    def write_raw(self, name: str, text: str) -> None:
        """
        Model output exactly as returned, before any parsing.

        Written as a JSON envelope carrying the text verbatim rather than as a
        bare .txt, so the file is machine-readable, but `text` is never
        touched -- not stripped, not fence-removed, not re-encoded.
        """
        self.write(name, {"captured_at": _now(), "text": text,
                          "length_chars": len(text)})

    # ---------- the stages ----------

    def run_started(self, **facts) -> None:
        self.write("run", {"run_id": self.run_id, "started_at": _now(), **facts})

    def source(self, source_row: dict, chunks: list[dict]) -> None:
        self.write("source", {
            "source": source_row,
            "chunk_count": len(chunks),
            # The retrieved passages, in the order the prompt will see them,
            # with locators -- so "was it even given the right passage" is
            # answerable without re-running anything.
            "chunks": [{"id": c.get("id"), "source_id": c.get("source_id"),
                        "locator": c.get("locator_json") or c.get("locator"),
                        "text": c.get("text")} for c in chunks],
        })

    def concepts(self, concepts: list[dict], *, resolution: list[dict] | None = None) -> None:
        self.write("concepts", {"concepts": concepts, "resolution": resolution or []})

    def prompt(self, *, prompt: str, system: str = "", task_type: str = "",
               prompt_version: str = "", temperature: float | None = None,
               max_tokens: int | None = None, demos: list | None = None) -> None:
        self.write("prompt", {
            "task_type": task_type, "prompt_version": prompt_version,
            "system": system, "prompt": prompt,
            "temperature": temperature, "max_tokens": max_tokens,
            "demonstration_ids": demos or [],
            "prompt_chars": len(prompt),
        })

    def raw_output(self, result) -> None:
        """`result` is an AIResult, or anything carrying .text and provenance."""
        self.write_raw("raw_model_output", getattr(result, "text", "") or "")
        self.write("model_call", {
            "candidate_id": getattr(result, "candidate_id", None),
            "model": getattr(result, "model", None),
            "provider": getattr(result, "provider", None),
            # promoted / routed / development_override -- the difference
            # between an evaluated answer and an unevaluated one.
            "source": getattr(result, "source", None),
            "latency_ms": getattr(result, "latency_ms", None),
            "attempts": getattr(result, "attempts", None),
            "execution_id": getattr(result, "execution_id", None),
            "parsed_by_provider": getattr(result, "parsed", None) is not None,
        })

    def normalized(self, *, accepted: list[dict], rejected: list[dict]) -> None:
        """
        What survived normalization and what did not.

        Rejections carry their reason. A run that generated five questions and
        stored one is a different event from a run that generated one, and the
        distinction is invisible unless the four dropped ones are recorded.
        """
        self.write("normalized_question", {
            "accepted_count": len(accepted), "rejected_count": len(rejected),
            "accepted": accepted, "rejected": rejected,
        })

    def validation(self, records: list[dict]) -> None:
        self.write("validation", {"validated_count": len(records), "results": records})

    def final(self, *, decision: str, reason: str = "", **facts) -> None:
        self.write("final_decision", {
            "decision": decision, "reason": reason, "finished_at": _now(),
            "stages_completed": list(self.stages), **facts})

    def failed(self, stage: str, exc: BaseException) -> None:
        """A failed run is the interesting one; record where it stopped."""
        self.write("final_decision", {
            "decision": "failed", "failed_stage": stage,
            "error": f"{type(exc).__name__}: {exc}", "finished_at": _now(),
            "stages_completed": list(self.stages)})

    # ---------- reading back ----------

    @classmethod
    def load(cls, run_dir: str | Path) -> dict:
        """Every artifact in one run directory, keyed by stem."""
        run_dir = Path(run_dir)
        out = {}
        for path in sorted(run_dir.glob("*.json")):
            try:
                out[path.stem] = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                out[path.stem] = {"unreadable": str(exc)}
        return out

    @classmethod
    def runs(cls, root: str | Path | None = None) -> list[str]:
        base = Path(root) if root else trace_root()
        if not base.exists():
            return []
        return sorted(d.name for d in base.iterdir() if d.is_dir())


class NullTrace(GenerationTrace):
    """Writes nothing. The default, so tracing is opt-in per run."""

    def __init__(self):
        super().__init__(run_id="null", root=Path("."), enabled=False)

    def __post_init__(self):
        self.enabled = False
        self.stages = []
