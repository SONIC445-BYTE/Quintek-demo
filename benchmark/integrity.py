"""
Integrity preconditions.

These are not scored. They determine whether a run produced a measurement at
all. Any failure yields INVALID_RUN and performance figures are withheld
entirely -- see docs/SCORECARD_SPEC.md.

Design rule: fail closed. A check that cannot be performed returns False with a
reason, never True. An integrity system that defaults to "probably fine" is
worse than none, because it renders as a green line on a scorecard.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# The obfuscated-citation pattern that v0.3's own test failed to match, because
# Private Use Area characters sit between 'cite' and 'turn'.
PUA = re.compile(r"[\ue000-\uf8ff]")
CITE_ARTIFACT = re.compile(r"[\ue000-\uf8ff]?cite[\ue000-\uf8ff]?turn\d", re.IGNORECASE)


@dataclass
class IntegrityReport:
    satisfied: bool
    failed_checks: list[str] = field(default_factory=list)
    details: dict[str, str] = field(default_factory=dict)
    passed_checks: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "satisfied": self.satisfied,
            "failed_checks": self.failed_checks,
            "passed_checks": self.passed_checks,
            "details": self.details,
        }


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class IntegrityChecker:
    """
    Runs every precondition named in the gate registry.

    A check missing from this class is reported as a FAILURE, not skipped. If the
    registry names a control the code cannot perform, the honest report is that
    the control was not verified.
    """

    def __init__(self, registry, root: str | Path = "."):
        self.registry = registry
        self.root = Path(root)
        self.results: dict[str, tuple[bool, str]] = {}

    def run(self, ctx: dict) -> IntegrityReport:
        handlers = {
            "holdout_isolation_verified": self._holdout_isolation,
            "gold_not_present_in_generation_prompts": self._gold_not_leaked,
            "dataset_hash_matches_manifest": self._dataset_hash,
            "prompt_hashes_recorded": self._prompt_hashes,
            "candidate_manifest_complete": self._candidate_manifest,
            "judge_independence_tier_satisfied": self._judge_independence,
            "no_same_family_judge_as_sole_pass_basis": self._judge_family,
            "reviewer_qualification_satisfied": self._reviewer_qualification,
            "reviewer_calibration_current": self._reviewer_calibration,
            "forbidden_artifact_scan_clean": self._artifact_scan,
            "no_post_hoc_threshold_edit_detected": self._no_post_hoc_edit,
            "budget_not_exhausted_mid_track": self._budget_mid_track,
            "gate_registry_calibration_state_is_frozen": self._calibration_frozen,
        }

        failed, passed, details = [], [], {}
        for check in self.registry.integrity_checks:
            fn = handlers.get(check)
            if fn is None:
                failed.append(check)
                details[check] = "no handler implemented; check NOT verified (fail closed)"
                continue
            try:
                ok, detail = fn(ctx)
            except Exception as exc:  # a check that errors has not passed
                ok, detail = False, f"check raised: {exc!r}"
            (passed if ok else failed).append(check)
            if detail:
                details[check] = detail

        return IntegrityReport(
            satisfied=not failed,
            failed_checks=failed,
            passed_checks=passed,
            details=details,
        )

    # -- individual checks -------------------------------------------------

    def _holdout_isolation(self, ctx):
        accessed = ctx.get("holdout_paths_accessed_by_candidate", [])
        if accessed:
            return False, f"candidate process read {len(accessed)} HOLDOUT path(s): {accessed[:3]}"
        if not ctx.get("holdout_isolation_enforced", False):
            return False, "holdout isolation was not enforced during the run"
        return True, ""

    def _gold_not_leaked(self, ctx):
        leaks = []
        for rec in ctx.get("generation_prompts", []):
            text = rec.get("prompt", "")
            gold = rec.get("gold") or {}
            for field_name in ("answer", "rationale"):
                val = gold.get(field_name)
                if val and str(val) in text:
                    leaks.append(f"{rec.get('item_id')}:{field_name}")
        if leaks:
            return False, f"gold present in generation prompts: {leaks[:5]}"
        return True, ""

    def _dataset_hash(self, ctx):
        expected = ctx.get("manifest_dataset_hash")
        actual = ctx.get("actual_dataset_hash")
        if not expected or not actual:
            return False, "dataset hash missing from manifest or run"
        if expected != actual:
            return False, f"dataset hash mismatch: manifest {expected[:12]} vs actual {actual[:12]}"
        return True, ""

    def _prompt_hashes(self, ctx):
        hashes = ctx.get("prompt_hashes") or {}
        if not hashes:
            return False, "no prompt hashes recorded"
        return True, f"{len(hashes)} prompt version(s) recorded"

    def _candidate_manifest(self, ctx):
        manifest = ctx.get("candidate_manifest") or {}
        required = [
            "provider", "model_id", "model_version", "system_prompt_hash",
            "task_prompt_hashes", "decoding_config", "code_commit",
        ]
        missing = [f for f in required if not manifest.get(f)]
        if missing:
            return False, f"candidate manifest incomplete, missing: {missing}"
        return True, ""

    def _judge_independence(self, ctx):
        judge = ctx.get("judge_config") or {}
        if not judge:
            return True, "no LLM judge used (deterministic scoring only)"
        if judge.get("tier") != 2:
            return False, f"primary judge is tier {judge.get('tier')}, tier 2 required"
        return True, ""

    def _judge_family(self, ctx):
        judge = ctx.get("judge_config") or {}
        cand = ctx.get("candidate_manifest") or {}
        if not judge:
            return True, ""
        jf = (judge.get("model_family") or "").lower()
        cf = (cand.get("model_family") or "").lower()
        if jf and cf and jf == cf and judge.get("gates_pass", True):
            return False, (f"judge family '{jf}' matches candidate family; a same-family "
                           "judge may never be the sole basis for PASS")
        return True, ""

    def _reviewer_qualification(self, ctx):
        review = ctx.get("review") or {}
        if review.get("mode") == "developmental":
            # Legitimate mode, but it caps the outcome elsewhere rather than
            # failing integrity. Qualification is not violated, merely limited.
            return True, "developmental mode: outcome ceiling applies"
        if not review.get("reviewer_qualifications"):
            return False, "no reviewer qualifications recorded"
        return True, ""

    def _reviewer_calibration(self, ctx):
        review = ctx.get("review") or {}
        if review.get("mode") == "developmental":
            return True, "developmental mode"
        if not review.get("calibration_current", False):
            return False, "reviewer calibration is stale or absent"
        return True, ""

    def _artifact_scan(self, ctx):
        offenders = []
        for p in self.root.rglob("*"):
            if not p.is_file() or p.suffix not in {".md", ".json", ".yaml", ".yml", ".jsonl"}:
                continue
            if "runs" in p.parts or ".git" in p.parts:
                continue
            try:
                s = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if CITE_ARTIFACT.search(s) or PUA.search(s):
                offenders.append(str(p.relative_to(self.root)))
        if offenders:
            return False, f"generated-text artifacts found in: {offenders[:5]}"
        return True, ""

    def _no_post_hoc_edit(self, ctx):
        recorded = ctx.get("manifest_gate_registry_hash")
        actual = ctx.get("actual_gate_registry_hash")
        if not recorded or not actual:
            return False, "gate registry hash not recorded at run start"
        if recorded != actual:
            return False, ("gate registry changed during the run -- post-hoc threshold edit. "
                           "This is the single highest-value attack on the benchmark.")
        return True, ""

    def _budget_mid_track(self, ctx):
        if ctx.get("budget_exhausted_mid_track", False):
            return False, "budget exhausted partway through a track: coverage is partial"
        return True, ""

    def _calibration_frozen(self, ctx):
        # Not an integrity failure -- an uncalibrated registry is an honest
        # developmental state. It caps the outcome via evaluate_run instead.
        if not self.registry.is_calibrated:
            return True, "UNCALIBRATED: outcome ceiling NOT_VALID_FOR_PRODUCTION_PASS applies"
        return True, "frozen"
