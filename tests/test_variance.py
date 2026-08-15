"""
Re-run variance protocol.

docs/VARIANCE_PROTOCOL.md's actual execution protocol (DEV characterization,
sentinel re-run, disagreement metrics, "store every execution, never
overwrite") had no executing code before this -- only the reliability gate
thresholds existed in the registry. These tests exercise
benchmark/variance.py directly, including feeding its output straight into
benchmark.gates.evaluate_run the same way a real run would.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from benchmark import dataset as ds
from benchmark.gates import GateRegistry, Measurement, evaluate_run
from benchmark.providers.base import GenerationRequest
from benchmark.providers.scripted import ScriptedProvider
from benchmark.variance import (
    VarianceConfig, answer_disagreement_rate, characterize,
    generation_score_mean_absolute_difference, reliability_from_variance,
    select_sentinel_sample, sentinel_rerun, validator_decision_disagreement_rate,
)

DATA = ROOT / "data" / "synthetic_harness_v0_4.jsonl"


@pytest.fixture(scope="module")
def qa_items():
    items = ds.load(DATA)
    return [i for i in items if i.track == "medical_qa"][:60]


def test_config_reads_from_yaml_not_literals():
    cfg = VarianceConfig.from_yaml({"variance": {"dev_characterization_runs": 5,
                                                  "sentinel_fraction": 0.2}})
    assert cfg.dev_characterization_runs == 5
    assert cfg.sentinel_fraction == 0.2


def test_config_defaults_match_registered_yaml_values():
    import yaml
    cfg_raw = yaml.safe_load((ROOT / "configs" / "v0_4.yaml").read_text())
    cfg = VarianceConfig.from_yaml(cfg_raw)
    assert cfg.dev_characterization_runs == cfg_raw["variance"]["dev_characterization_runs"]
    assert cfg.sentinel_fraction == cfg_raw["variance"]["sentinel_fraction"]


def test_characterize_runs_each_item_the_registered_number_of_times(qa_items, tmp_path):
    provider = ScriptedProvider(accuracy=0.5, seed=1)
    cfg = VarianceConfig(dev_characterization_runs=3, sentinel_fraction=0.1)
    store = tmp_path / "characterization.jsonl"
    results = characterize(provider, qa_items, cfg, store_path=store)

    assert set(results) == {it.id for it in qa_items}
    assert all(len(runs) == 3 for runs in results.values())

    # Every execution is on disk -- 3 per item, none overwritten.
    lines = store.read_text().strip().splitlines()
    assert len(lines) == 3 * len(qa_items)
    repeat_indices = sorted({json.loads(l)["repeat_index"] for l in lines})
    assert repeat_indices == [0, 1, 2]


def test_characterize_at_low_accuracy_actually_shows_disagreement(qa_items):
    """A stochastic provider run 3x per item should not always agree with itself."""
    provider = ScriptedProvider(accuracy=0.5, seed=2)
    cfg = VarianceConfig(dev_characterization_runs=3, sentinel_fraction=0.1)
    results = characterize(provider, qa_items, cfg)
    disagreement_seen = any(
        len({r.parsed["answer"] for r in runs}) > 1 for runs in results.values()
    )
    assert disagreement_seen, "accuracy=0.5 across 3 runs/item and 60 items should show variance"


def test_sentinel_sample_size_matches_fraction():
    ids = [f"item-{i}" for i in range(200)]
    cfg = VarianceConfig(sentinel_fraction=0.10, seed=5)
    sample = select_sentinel_sample(ids, cfg)
    assert len(sample) == 20
    assert len(set(sample)) == 20  # no duplicates
    assert set(sample) <= set(ids)


def test_sentinel_rerun_pairs_original_and_rerun_and_stores_both(qa_items, tmp_path):
    provider = ScriptedProvider(accuracy=1.0, seed=9)
    original = {it.id: provider.generate(GenerationRequest(
        item_id=it.id, prompt=it.prompt,
        metadata={"gold_answer": it.gold["answer"], "options": it.gold["options"]}))
        for it in qa_items}

    cfg = VarianceConfig(sentinel_fraction=0.2, seed=9)
    store = tmp_path / "sentinel.jsonl"
    pairs = sentinel_rerun(provider, qa_items, original, cfg, store_path=store)

    assert len(pairs) == round(len(qa_items) * 0.2)
    for iid, (orig, rerun) in pairs.items():
        assert orig is original[iid]
        assert rerun is not orig  # a genuinely new execution, not a cached copy

    lines = store.read_text().strip().splitlines()
    assert len(lines) == len(pairs)
    assert all(json.loads(l)["rerun_of"] in pairs for l in lines)


def test_original_response_object_is_never_mutated(qa_items):
    provider = ScriptedProvider(accuracy=1.0, seed=3)
    original = {it.id: provider.generate(GenerationRequest(
        item_id=it.id, prompt=it.prompt,
        metadata={"gold_answer": it.gold["answer"], "options": it.gold["options"]}))
        for it in qa_items}
    snapshot = {iid: r.as_dict() for iid, r in original.items()}

    cfg = VarianceConfig(sentinel_fraction=1.0, seed=3)
    sentinel_rerun(provider, qa_items, original, cfg)

    assert all(original[iid].as_dict() == snapshot[iid] for iid in original)


# ---------------------------------------------------------------------------
# Disagreement metrics
# ---------------------------------------------------------------------------

def test_answer_disagreement_rate_zero_for_a_deterministic_provider(qa_items):
    provider = ScriptedProvider(accuracy=1.0, seed=4)
    original = {it.id: provider.generate(GenerationRequest(
        item_id=it.id, prompt=it.prompt,
        metadata={"gold_answer": it.gold["answer"], "options": it.gold["options"]}))
        for it in qa_items}
    cfg = VarianceConfig(sentinel_fraction=1.0, seed=4)
    pairs = sentinel_rerun(provider, qa_items, original, cfg)
    rate = answer_disagreement_rate(pairs)
    # accuracy=1.0 means every attempt returns the gold answer deterministically.
    assert rate == 0.0


def test_answer_disagreement_rate_excludes_errored_pairs():
    from benchmark.providers.base import GenerationResponse

    ok = GenerationResponse(item_id="a", raw_output="{}", parsed={"answer": "A"},
                            provider="p", model="m", model_version="v", latency_ms=1.0)
    errored = GenerationResponse(item_id="a", raw_output="", parsed=None,
                                 provider="p", model="m", model_version="v",
                                 latency_ms=1.0, error="boom")
    pairs = {"a": (ok, errored), "b": (ok, ok)}
    # Only "b" is usable; "a" is excluded because the rerun errored.
    assert answer_disagreement_rate(pairs) == 0.0


def test_validator_decision_disagreement_rate():
    from benchmark.providers.base import GenerationResponse

    def resp(**parsed):
        return GenerationResponse(item_id="x", raw_output="{}", parsed=parsed,
                                  provider="p", model="m", model_version="v", latency_ms=1.0)

    approve = dict(medical_correct=True, answer_key_correct=True, single_best=True,
                   critical_error=False)
    reject = dict(medical_correct=False, answer_key_correct=True, single_best=True,
                  critical_error=False)

    pairs = {
        "agree_approve": (resp(**approve), resp(**approve)),
        "agree_reject": (resp(**reject), resp(**reject)),
        "disagree": (resp(**approve), resp(**reject)),
    }
    rate = validator_decision_disagreement_rate(pairs)
    assert rate == pytest.approx(1 / 3)


def test_generation_score_mean_absolute_difference():
    pairs = [(3.0, 3.0), (2.0, 2.5), (4.0, 3.0)]
    mad = generation_score_mean_absolute_difference(pairs)
    assert mad == pytest.approx((0 + 0.5 + 1.0) / 3)


def test_generation_score_mad_empty_is_none():
    assert generation_score_mean_absolute_difference([]) is None


# ---------------------------------------------------------------------------
# Integration: feeds evaluate_run's reliability dict directly
# ---------------------------------------------------------------------------

def test_reliability_from_variance_feeds_gate_evaluation_directly(qa_items):
    registry = GateRegistry(ROOT / "configs" / "gate_registry_v0_4.json")

    provider = ScriptedProvider(accuracy=1.0, seed=11)
    original = {it.id: provider.generate(GenerationRequest(
        item_id=it.id, prompt=it.prompt,
        metadata={"gold_answer": it.gold["answer"], "options": it.gold["options"]}))
        for it in qa_items}
    cfg = VarianceConfig(sentinel_fraction=1.0, seed=11)
    pairs = sentinel_rerun(provider, qa_items, original, cfg)

    reliability = reliability_from_variance(answer_pairs=pairs)
    assert reliability["GATE-REL-VARIANCE-ANSWER"] == 0.0
    assert reliability["GATE-REL-VARIANCE-VALIDATOR"] is None  # not measured this call

    m = {key: Measurement(n=0) for key in registry.tracks}  # everything else UNEVALUABLE
    out = evaluate_run(registry, m, integrity_ok=True, reliability=reliability)
    # UNEVALUABLE from the missing tracks, not from the variance gates we DID
    # measure -- proves the reliability dict was actually consumed, not ignored.
    assert out.reliability_results == reliability


def test_none_reliability_value_is_unmeasured_not_a_free_pass():
    """
    A gate we never measured must not silently read as satisfying its
    threshold -- benchmark.gates.evaluate_run treats a None value as
    unmeasured, same as if the key were absent entirely.
    """
    registry = GateRegistry(ROOT / "configs" / "gate_registry_v0_4.json")
    reliability = reliability_from_variance()  # nothing supplied -> all None
    assert all(v is None for v in reliability.values())


# ---------------------------------------------------------------------------
# Runner integration -- opt-in, off by default
#
# These instantiate Runner against the real repo root (config paths in
# configs/v0_4.yaml are relative to it) and clean up the run directories
# they create rather than leaving them for the next commit to pick up.
# ---------------------------------------------------------------------------

@pytest.fixture
def runner():
    from benchmark.runner import Runner
    return Runner(ROOT / "configs" / "v0_4.yaml", root=ROOT)


def _cleanup(run_dir: Path):
    import shutil
    shutil.rmtree(run_dir, ignore_errors=True)


def test_runner_default_does_not_perform_sentinel_variance(runner):
    provider = ScriptedProvider(accuracy=1.0, seed=42)
    result = runner.run(provider, DATA, run_id="test-variance-off")
    run_dir = Path(result["run_dir"])
    try:
        assert not (run_dir / "variance_summary.json").exists()
    finally:
        _cleanup(run_dir)


def test_runner_opt_in_sentinel_variance_writes_summary_and_feeds_reliability(runner):
    provider = ScriptedProvider(accuracy=1.0, seed=43)
    result = runner.run(provider, DATA, run_id="test-variance-on", run_sentinel_variance=True)
    run_dir = Path(result["run_dir"])
    try:
        summary_path = run_dir / "variance_summary.json"
        assert summary_path.exists()
        summary = json.loads(summary_path.read_text())
        assert summary["sentinel_n"] > 0
        assert summary["GATE-REL-VARIANCE-ANSWER"] is not None

        rerun_path = run_dir / "variance_sentinel_rerun.jsonl"
        assert rerun_path.exists()
        assert len(rerun_path.read_text().strip().splitlines()) == summary["sentinel_n"]

        report = json.loads((run_dir / "report.json").read_text())
        if report.get("reliability") is not None:
            assert "GATE-REL-VARIANCE-ANSWER" in report["reliability"]
    finally:
        _cleanup(run_dir)


def test_caller_supplied_reliability_value_is_not_overridden(runner):
    """
    setdefault semantics: if the caller already computed
    GATE-REL-VARIANCE-ANSWER from their own harness, the opt-in sentinel
    re-run must not clobber it with its own number.
    """
    provider = ScriptedProvider(accuracy=1.0, seed=44)
    result = runner.run(provider, DATA, run_id="test-variance-preset",
                        reliability={"GATE-REL-VARIANCE-ANSWER": 0.999},
                        run_sentinel_variance=True)
    run_dir = Path(result["run_dir"])
    try:
        summary = json.loads((run_dir / "variance_summary.json").read_text())
        assert summary["GATE-REL-VARIANCE-ANSWER"] == 0.999
    finally:
        _cleanup(run_dir)
