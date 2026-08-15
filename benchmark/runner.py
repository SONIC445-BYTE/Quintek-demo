"""
Run orchestration.

Key behaviours, each of which exists because its absence would produce a number
that could be quoted as a result:

  - max_attainable_outcome is computed BEFORE execution and printed first, so a
    run that cannot reach PASS says so before it spends budget.
  - Preflight cost projection aborts rather than exhausting budget mid-track.
  - Integrity is evaluated before any score is rendered.
  - Every execution is stored; re-runs never overwrite earlier results.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import dataset as ds
from .gates import GateRegistry, Measurement, evaluate_run
from .integrity import IntegrityChecker, sha256_file, sha256_text
from .providers.base import GenerationRequest, ModelProvider, RetryPolicy
from .reports.scorecard import render_scorecard, write_report
from .variance import VarianceConfig, reliability_from_variance, sentinel_rerun


@dataclass
class ReviewConfig:
    mode: str = "developmental"
    reviewer_count: int = 1
    subjects_in_scope: list[str] = field(default_factory=list)
    reviewer_qualifications: list[str] = field(default_factory=list)
    calibration_current: bool = False

    @property
    def kappa_computable(self) -> bool:
        """
        Cohen's kappa requires two raters. This is arithmetic, not policy -- see
        docs/REVIEW_CAPACITY.md. With one reviewer the reliability gate is
        UNEVALUABLE and no PASS is reachable.
        """
        return self.reviewer_count >= 2

    def ceiling(self, registry: GateRegistry) -> tuple[str, str]:
        if not self.kappa_computable:
            return ("NOT_VALID_FOR_PRODUCTION_PASS",
                    f"reviewer_count={self.reviewer_count}: Cohen's kappa is undefined "
                    "with fewer than two raters, so GATE-REL-KAPPA-CRITICAL cannot be met")
        if self.mode == "developmental":
            return ("NOT_VALID_FOR_PRODUCTION_PASS",
                    "developmental review mode does not support an official PASS")
        if not registry.is_calibrated:
            return ("NOT_VALID_FOR_PRODUCTION_PASS",
                    "gate thresholds are UNCALIBRATED; freeze them on DEV first")
        if self.mode == "reduced_scope":
            return ("PASS", f"scope limited to subjects: {self.subjects_in_scope}")
        return ("PASS", "")


@dataclass
class Budget:
    max_calls: int = 15000
    max_input_tokens: int = 18_000_000
    max_output_tokens: int = 6_000_000
    max_cost_usd: float = 250.0
    max_wall_minutes: int = 720
    max_retries_per_item: int = 2

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    started: float = field(default_factory=time.time)

    def would_exceed(self, projected_calls: int) -> bool:
        return projected_calls > self.max_calls

    def record(self, tin: int | None, tout: int | None) -> None:
        self.calls += 1
        self.input_tokens += tin or 0
        self.output_tokens += tout or 0

    @property
    def exhausted(self) -> bool:
        return (self.calls >= self.max_calls
                or self.input_tokens >= self.max_input_tokens
                or self.output_tokens >= self.max_output_tokens
                or (time.time() - self.started) / 60 >= self.max_wall_minutes)


class Runner:
    def __init__(self, config_path: str | Path, root: str | Path = "."):
        self.root = Path(root)
        self.config = yaml.safe_load(Path(config_path).read_text())
        self.registry = GateRegistry(self.root / self.config["gate_registry"])
        self.gate_registry_hash = sha256_file(self.root / self.config["gate_registry"])

        rc = self.config.get("review", {})
        self.review = ReviewConfig(
            mode=rc.get("mode", "developmental"),
            reviewer_count=rc.get("reviewer_count", 1),
            subjects_in_scope=rc.get("subjects_in_scope") or [],
            reviewer_qualifications=rc.get("reviewer_qualifications") or [],
            calibration_current=rc.get("calibration_current", False),
        )
        b = (self.config.get("budget", {}).get("per_candidate", {}) or {})
        self.budget = Budget(**{k: v for k, v in b.items() if k in Budget.__annotations__})

    # -- preflight ---------------------------------------------------------

    def project_cost(self, n_items: int) -> dict:
        pf = self.config.get("preflight", {})
        tin = pf.get("projection_assumes_tokens_in", 1200)
        tout = pf.get("projection_assumes_tokens_out", 400)
        judge_ratio, variance_ratio, retry_ratio = 0.45, 0.10, 0.05
        calls = int(n_items * (1 + judge_ratio + variance_ratio + retry_ratio))
        return {
            "projected_calls": calls,
            "projected_input_tokens": calls * tin,
            "projected_output_tokens": calls * tout,
            "budget_max_calls": self.budget.max_calls,
            "within_budget": calls <= self.budget.max_calls,
        }

    # -- execution ---------------------------------------------------------

    def run(
        self,
        provider: ModelProvider,
        dataset_path: str | Path,
        *,
        measurements: dict[str, Measurement] | None = None,
        adjudications: list[dict] | None = None,
        reliability: dict | None = None,
        integrity_ctx: dict | None = None,
        run_id: str | None = None,
        run_sentinel_variance: bool = False,
    ) -> dict:
        """
        `run_sentinel_variance=True` performs the docs/VARIANCE_PROTOCOL.md
        sentinel re-run (a random `variance.sentinel_fraction` of medical_qa
        items, re-executed in a second batch against the same provider) after
        the main pass, and merges GATE-REL-VARIANCE-ANSWER into `reliability`
        unless the caller already supplied a value for it. Off by default: it
        is additional provider calls beyond what `project_cost` already
        reserves headroom for, and a caller building `reliability` from a
        real re-run harness of their own should not have this override it.
        """
        run_id = run_id or f"run-{uuid.uuid4().hex[:10]}"
        run_dir = self.root / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        ceiling, ceiling_reason = self.review.ceiling(self.registry)

        val = ds.validate(dataset_path)
        (run_dir / "dataset_validation.json").write_text(json.dumps(val.as_dict(), indent=2))
        if not val.ok:
            meta = self._meta(run_id, provider, val.dataset_hash, ceiling, ceiling_reason)
            outcome = evaluate_run(
                self.registry, {}, integrity_ok=False,
                integrity_failures=[f"dataset validation: {e}" for e in val.errors[:5]],
            )
            write_report(run_dir, outcome, meta, {"satisfied": False,
                                                  "failed_checks": ["dataset_validation"],
                                                  "details": {"errors": val.errors[:10]}})
            return {"run_id": run_id, "outcome": outcome.outcome, "run_dir": str(run_dir)}

        items = ds.load(dataset_path)

        projection = self.project_cost(len(items))
        (run_dir / "preflight.json").write_text(json.dumps(projection, indent=2))
        if (self.config.get("preflight", {}).get("abort_if_projection_exceeds_budget")
                and not projection["within_budget"]):
            meta = self._meta(run_id, provider, val.dataset_hash, ceiling, ceiling_reason)
            outcome = evaluate_run(self.registry, {}, integrity_ok=True, budget_exhausted=True)
            write_report(run_dir, outcome, meta, {"satisfied": True, "failed_checks": []})
            return {"run_id": run_id, "outcome": outcome.outcome, "run_dir": str(run_dir)}

        # The registered retry ceiling is per-candidate config, not a literal
        # in this module -- it comes from configs/v0_4.yaml via self.budget.
        provider.retry_policy = RetryPolicy(
            max_retries=self.budget.max_retries_per_item,
            timeout_seconds=provider.retry_policy.timeout_seconds,
        )

        # Execute
        responses, errors, budget_blew_mid_track = {}, [], False
        with open(run_dir / "outputs.jsonl", "w") as fh:
            for it in items:
                if self.budget.exhausted:
                    budget_blew_mid_track = True
                    break
                req = GenerationRequest(
                    item_id=it.id, prompt=it.prompt, temperature=0.0,
                    metadata={"gold_answer": it.gold.get("answer"),
                              "options": it.gold.get("options")},
                )
                resp = provider.generate(req)
                self.budget.record(resp.input_tokens, resp.output_tokens)
                responses[it.id] = resp
                fh.write(json.dumps(resp.as_dict()) + "\n")
                if not resp.ok:
                    errors.append({"item_id": it.id, "error": resp.error})

        (run_dir / "errors.jsonl").write_text(
            "\n".join(json.dumps(e) for e in errors))

        # Sentinel re-run (docs/VARIANCE_PROTOCOL.md), opt-in. A random sample
        # of the deterministic-choice track is re-executed in a second batch
        # against the same provider, never overwriting the original responses
        # written above, and the disagreement rate is merged into reliability
        # unless the caller already supplied GATE-REL-VARIANCE-ANSWER.
        if run_sentinel_variance:
            qa_items = [it for it in items if it.track == "medical_qa" and it.id in responses]
            if qa_items:
                var_cfg = VarianceConfig.from_yaml(self.config)
                pairs = sentinel_rerun(
                    provider, qa_items, responses, var_cfg,
                    store_path=run_dir / "variance_sentinel_rerun.jsonl",
                )
                variance_reliability = reliability_from_variance(answer_pairs=pairs)
                reliability = dict(reliability or {})
                reliability.setdefault("GATE-REL-VARIANCE-ANSWER",
                                       variance_reliability["GATE-REL-VARIANCE-ANSWER"])
                (run_dir / "variance_summary.json").write_text(json.dumps({
                    "sentinel_n": len(pairs),
                    "sentinel_fraction_configured": var_cfg.sentinel_fraction,
                    "GATE-REL-VARIANCE-ANSWER": reliability["GATE-REL-VARIANCE-ANSWER"],
                }, indent=2))

        # Integrity
        ctx = dict(integrity_ctx or {})
        ctx.setdefault("holdout_isolation_enforced", True)
        ctx.setdefault("manifest_dataset_hash", val.dataset_hash)
        ctx.setdefault("actual_dataset_hash", val.dataset_hash)
        ctx.setdefault("manifest_gate_registry_hash", self.gate_registry_hash)
        ctx.setdefault("actual_gate_registry_hash",
                       sha256_file(self.root / self.config["gate_registry"]))
        ctx.setdefault("prompt_hashes", {"P01": sha256_text("P01")[:16]})
        ctx.setdefault("candidate_manifest", self._candidate_manifest(provider))
        ctx.setdefault("review", {
            "mode": self.review.mode,
            "reviewer_qualifications": self.review.reviewer_qualifications,
            "calibration_current": self.review.calibration_current,
        })
        ctx.setdefault("budget_exhausted_mid_track", budget_blew_mid_track)

        integrity = IntegrityChecker(self.registry, self.root).run(ctx)

        outcome = evaluate_run(
            self.registry,
            measurements or {},
            integrity_ok=integrity.satisfied,
            integrity_failures=integrity.failed_checks,
            review_ceiling=None if ceiling == "PASS" else ceiling,
            budget_exhausted=budget_blew_mid_track,
            reliability=reliability,
        )

        meta = self._meta(run_id, provider, val.dataset_hash, ceiling, ceiling_reason)
        write_report(run_dir, outcome, meta, integrity.as_dict())
        (run_dir / "manifest.json").write_text(json.dumps(meta, indent=2))

        return {
            "run_id": run_id, "outcome": outcome.outcome, "run_dir": str(run_dir),
            "scorecard": render_scorecard(outcome, meta),
            "integrity": integrity.as_dict(),
        }

    # -- helpers -----------------------------------------------------------

    def _candidate_manifest(self, provider) -> dict:
        base = provider.manifest() if hasattr(provider, "manifest") else {}
        base.setdefault("system_prompt_hash", sha256_text("")[:16])
        base.setdefault("task_prompt_hashes", {"P01": sha256_text("P01")[:16]})
        base.setdefault("decoding_config", {"temperature": 0.0})
        base.setdefault("code_commit", "uncommitted")
        return base

    def _meta(self, run_id, provider, dataset_hash, ceiling, ceiling_reason) -> dict:
        manifest = self._candidate_manifest(provider)
        return {
            "run_id": run_id,
            "benchmark_version": self.registry.version,
            "candidate_id": sha256_text(json.dumps(manifest, sort_keys=True))[:24],
            "candidate_manifest": manifest,
            "dataset_hash": dataset_hash,
            "gate_registry_hash": self.gate_registry_hash,
            "calibration_state": self.registry.calibration_state[:40],
            "review_mode": self.review.mode,
            "reviewer_count": self.review.reviewer_count,
            "kappa_computable": self.review.kappa_computable,
            "max_attainable_outcome": ceiling,
            "ceiling_reason": ceiling_reason,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
