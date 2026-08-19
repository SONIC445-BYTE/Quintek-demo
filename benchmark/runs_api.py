"""
The run-centric half of the HTTP contract: `/api/runs`, `/api/datasets`,
`/api/gates`, `/api/preflight`.

`analytics_api.py` serves a *candidate*-centric view (leaderboards, per-track
summaries, routing). This module serves the *run*-centric view the engineering
console consumes: a run, its unmodified report.json, its integrity block, and
the pre-run checks (dataset validation, gate registry, cost projection).

Two rules shape everything here:

1. **`GET /api/runs/:run_id` returns the report exactly as it was written.**
   It is read off disk and passed through without reserialization through any
   dataclass. A console that renders a stored verdict must see the stored
   bytes, not a lossy round-trip of them -- otherwise the artifact under review
   is not the artifact that was produced.

2. **Nothing here invents a run.** `POST /api/runs` executes a benchmark only
   when an execution backend has actually been supplied (`run_launcher`).
   Without one it returns 501 and says so, because a benchmark run needs a
   configured provider, credentials and a corpus, and an API that answers
   "queued" while none of those exist would be reporting work it cannot do.
"""

from __future__ import annotations

import json
from pathlib import Path

from .gates import GateRegistry


class RunsAPI:
    """
    Route handlers returning `(status, body)`, or None when the path is not
    one of this module's routes so the caller can fall through to its own.

    `body` is normally a dict serialized as JSON; a `str` body is sent
    verbatim (used only by `report.md`, which is markdown, not JSON).
    """

    def __init__(self, runs_root: str | Path, *,
                 gate_registry_path: str | Path | None = None,
                 config_path: str | Path | None = None,
                 root: str | Path | None = None,
                 run_launcher=None):
        self.runs_root = Path(runs_root)
        self.gate_registry_path = Path(gate_registry_path) if gate_registry_path else None
        self.config_path = Path(config_path) if config_path else None
        self.root = Path(root) if root else Path.cwd()
        self.run_launcher = run_launcher
        # Dataset validations performed through POST /api/datasets/validate,
        # keyed by the hash the validator computed, so GET /api/datasets/:hash
        # can return a real prior result instead of guessing one.
        self._dataset_validations: dict[str, dict] = {}

    # ---------- run discovery ----------

    def _run_dirs(self):
        if not self.runs_root.exists():
            return
        for d in sorted(self.runs_root.iterdir()):
            if d.is_dir() and (d / "report.json").exists():
                yield d

    def _find_run_dir(self, run_id: str) -> Path | None:
        """
        Locate by the run_id recorded inside report.json, falling back to the
        directory name. The recorded id is authoritative: a directory can be
        renamed, moved or restored from backup, and the report is what the
        console was asked about.
        """
        direct = self.runs_root / run_id
        if (direct / "report.json").exists():
            return direct
        for d in self._run_dirs():
            try:
                if json.loads((d / "report.json").read_text()).get("run_id") == run_id:
                    return d
            except (json.JSONDecodeError, OSError):
                continue
        return None

    def _raw_report(self, run_dir: Path) -> dict:
        return json.loads((run_dir / "report.json").read_text())

    # ---------- GET ----------

    def handle_get(self, path: str, params: dict[str, list[str]]):
        def one(key, default=None):
            return params.get(key, [default])[0]

        if path == "/api/runs":
            return self._list_runs(one("limit"), one("offset"))

        if path == "/api/gates":
            return self._gates()

        if path == "/api/preflight":
            return self._preflight(one("dataset"))

        if path.startswith("/api/datasets/"):
            return self._dataset(path[len("/api/datasets/"):].strip("/"))

        if path.startswith("/api/runs/"):
            rest = [p for p in path[len("/api/runs/"):].split("/") if p]
            if not rest:
                return 404, {"error": f"no such endpoint: {path}"}
            run_dir = self._find_run_dir(rest[0])
            if run_dir is None:
                return 404, {"error": f"no such run: '{rest[0]}'"}
            if len(rest) == 1:
                return 200, self._raw_report(run_dir)
            if rest[1] == "integrity":
                report = self._raw_report(run_dir)
                return 200, {
                    "run_id": report.get("run_id"),
                    "outcome": report.get("outcome"),
                    "integrity": report.get("integrity"),
                }
            if rest[1] in ("report.md", "report_md"):
                md = run_dir / "report.md"
                if not md.exists():
                    return 404, {"error": f"run '{rest[0]}' has no report.md"}
                return 200, md.read_text()
            return 404, {"error": f"no such sub-resource: {rest[1]}"}

        return None

    def _list_runs(self, limit, offset):
        """
        Newest first. Each entry is the run's own report.json summary fields --
        never a recomputed score, and never a score at all for a suppressed run.
        """
        rows = []
        for d in self._run_dirs():
            try:
                r = self._raw_report(d)
            except (json.JSONDecodeError, OSError) as exc:
                # A corrupt run is reported as corrupt rather than skipped: a
                # console showing 11 of 12 runs with no explanation is worse
                # than one that names the unreadable one.
                rows.append({"run_id": d.name, "error": f"unreadable report.json: {exc}"})
                continue
            rows.append({
                "run_id": r.get("run_id") or d.name,
                "benchmark_version": r.get("benchmark_version"),
                "candidate_id": r.get("candidate_id"),
                "candidate_manifest": r.get("candidate_manifest"),
                "outcome": r.get("outcome"),
                "rankable": r.get("rankable"),
                "timestamp": r.get("timestamp"),
                "max_attainable_outcome": r.get("max_attainable_outcome"),
                "ceiling_reason": r.get("ceiling_reason"),
                "scores_withheld": r.get("scores") is None,
                "dataset_hash": r.get("dataset_hash"),
            })
        rows.sort(key=lambda x: x.get("timestamp") or "", reverse=True)
        total = len(rows)
        try:
            off = max(0, int(offset)) if offset is not None else 0
            lim = max(1, int(limit)) if limit is not None else total or 1
        except ValueError:
            return 400, {"error": "'limit' and 'offset' must be integers"}
        return 200, {"total": total, "offset": off, "limit": lim, "runs": rows[off:off + lim]}

    def _gates(self):
        if self.gate_registry_path is None or not self.gate_registry_path.exists():
            return 501, {"error": "no gate registry configured for this API instance"}
        reg = GateRegistry(self.gate_registry_path)
        from .integrity import sha256_file

        # The registry stores calibration as one sentence beginning with the
        # state token. The console shows the state as a badge and the rest as
        # explanatory text, so they are split here rather than in the UI.
        state_text = reg.calibration_state
        state = state_text.split()[0].rstrip("—-–,:").strip() if state_text else "UNKNOWN"
        note = state_text[len(state):].lstrip(" —-–,:").strip() if state_text else ""

        return 200, {
            "version": reg.version,
            "gate_registry_hash": sha256_file(self.gate_registry_path)[:10],
            "calibration_state": state,
            "calibration_note": note,
            "gates": [
                {
                    "gate_id": s["gate_id"],
                    "metric": s["metric"],
                    "direction": s["direction"],
                    "threshold": s["threshold"],
                    "required_n": s["min_n"],
                    "n_unit": s.get("n_unit", ""),
                    "mandatory": bool(s.get("mandatory", True)),
                    "overrides_all_other_gates": bool(s.get("overrides_all_other_gates", False)),
                    "max_tolerated_upper_bound": s.get("max_tolerated_upper_bound"),
                }
                for s in reg.tracks.values()
            ],
            "reliability_gates": [
                {
                    "gate_id": gid,
                    "metric": s["metric"],
                    "direction": s["direction"],
                    "threshold": s["threshold"],
                    "remediate_below": s.get("remediation_band_low"),
                }
                for gid, s in reg.reliability.items()
            ],
            "tolerance_relative": reg.tolerance,
            "outcome_states": list(reg.outcome_states),
        }

    def _dataset(self, dataset_hash: str):
        hit = self._dataset_validations.get(dataset_hash)
        if hit is None:
            return 404, {
                "error": f"no validation on record for dataset hash '{dataset_hash}'",
                "hint": "POST /api/datasets/validate first; results are recorded by hash",
            }
        return 200, hit

    def _preflight(self, dataset: str | None):
        if not dataset:
            return 400, {"error": "missing required query param 'dataset'"}
        if self.config_path is None or not self.config_path.exists():
            return 501, {"error": "no run config available; cost projection needs configs/v0_4.yaml"}
        path = Path(dataset)
        if not path.exists():
            return 404, {"error": f"no such dataset file: '{dataset}'"}
        from . import dataset as ds
        from .runner import Runner

        runner = Runner(self.config_path, root=self.root)
        n = len(ds.load(path))
        projection = runner.project_cost(n)
        ceiling, reason = runner.review.ceiling(runner.registry)
        return 200, {
            "dataset": str(path),
            "n_items": n,
            **projection,
            "max_attainable_outcome": ceiling,
            "ceiling_reason": reason,
        }

    # ---------- POST ----------

    def handle_post(self, path: str, body: dict):
        if path == "/api/datasets/validate":
            return self._validate_dataset(body)
        if path == "/api/runs":
            return self._create_run(body)
        return None

    def _validate_dataset(self, body: dict):
        """
        Re-validates server-side. A client-side "looks fine" is not a
        validation, and 422 (not 400) marks a well-formed request carrying a
        dataset that cannot be scored.
        """
        dataset = (body or {}).get("dataset")
        if not dataset:
            return 400, {"error": "body must name a 'dataset' path"}
        path = Path(dataset)
        if not path.exists():
            return 404, {"error": f"no such dataset file: '{dataset}'"}
        if path.suffix != ".jsonl":
            return 415, {"error": f"expected a .jsonl dataset, got '{path.suffix or 'no extension'}'"}

        from . import dataset as ds

        rep = ds.validate(path)
        payload = {
            "ok": rep.ok,
            "dataset": str(path),
            "dataset_hash": rep.dataset_hash,
            "n_items": rep.n_items,
            "by_track": rep.by_track,
            "by_split": rep.by_split,
            "errors": list(rep.errors),
            "warnings": list(getattr(rep, "warnings", []) or []),
        }
        self._dataset_validations[rep.dataset_hash] = payload
        return (200 if rep.ok else 422), payload

    def _create_run(self, body: dict):
        if self.run_launcher is None:
            return 501, {
                "error": "run execution is not configured on this API instance",
                "detail": "Starting a benchmark run requires a configured provider "
                          "adapter, provider credentials and a scoreable corpus. This "
                          "endpoint reports 501 rather than returning 'queued' for work "
                          "it cannot perform. Supply RunsAPI(run_launcher=...) to enable it.",
            }
        try:
            handle = self.run_launcher(body or {})
        except Exception as exc:
            return 400, {"error": f"{type(exc).__name__}: {exc}"}
        return 202, {"run_id": handle.get("run_id"), "status": handle.get("status", "queued")}
