"""
Injection battery family coverage.

docs/INTEGRITY_CI.md lists "injection battery smoke test" as a per-commit
check; it did not exist. These tests exercise the dataset-level coverage
check in benchmark/dataset.py and the diagnostic per-family breakdown in
benchmark/scorers/deterministic.py -- neither of which is a scoring gate
(no threshold for either exists in configs/gate_registry_v0_4.json, and none
is invented here).
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DATA = ROOT / "data" / "synthetic_harness_v0_4.jsonl"

from benchmark import dataset as ds
from benchmark.dataset import ATTACK_FAMILIES
from benchmark.providers.base import GenerationRequest
from benchmark.providers.scripted import ScriptedProvider
from benchmark.scorers.deterministic import score_injection_attack_success_by_family


def _write(tmp_path, items):
    p = tmp_path / "d.jsonl"
    p.write_text("\n".join(json.dumps(i) for i in items))
    return p


def _injection_item(iid, family, split="adversarial"):
    return {
        "id": iid, "track": "injection", "split": split, "subject": "s",
        "prompt": f"prompt {iid}",
        "gold": {"answer": "A", "accepted_answers": ["A"], "options": ["A", "B"],
                 "attack_family": family},
        "provenance": {"type": "synthetic_harness_test"},
        "adjudication": {"status": "verified", "reviewers": 2},
        "severity": "medium",
    }


def test_synthetic_corpus_covers_all_ten_attack_families():
    """The shipped synthetic corpus must not itself have a battery gap."""
    rep = ds.validate(DATA)
    assert rep.ok
    assert set(rep.by_attack_family) == ATTACK_FAMILIES
    assert all(count > 0 for count in rep.by_attack_family.values())


def test_missing_attack_family_fails_validation(tmp_path):
    items = [_injection_item(f"INJ-{i}", fam)
             for i, fam in enumerate(sorted(ATTACK_FAMILIES - {"PI-07"}))]
    p = _write(tmp_path, items)
    rep = ds.validate(p)
    assert not rep.ok
    assert any("PI-07" in e for e in rep.errors)


def test_unknown_attack_family_value_is_an_error(tmp_path):
    items = [_injection_item("INJ-1", "PI-99")]
    p = _write(tmp_path, items)
    rep = ds.validate(p)
    assert not rep.ok
    assert any("unknown attack_family" in e for e in rep.errors)


def test_missing_attack_family_field_is_an_error(tmp_path):
    item = _injection_item("INJ-1", "PI-01")
    del item["gold"]["attack_family"]
    p = _write(tmp_path, [item])
    rep = ds.validate(p)
    assert not rep.ok
    assert any("no gold.attack_family" in e for e in rep.errors)


def test_full_battery_coverage_passes(tmp_path):
    items = [_injection_item(f"INJ-{i}", fam) for i, fam in enumerate(sorted(ATTACK_FAMILIES))]
    p = _write(tmp_path, items)
    rep = ds.validate(p)
    assert rep.ok
    assert set(rep.by_attack_family) == ATTACK_FAMILIES


# ---------------------------------------------------------------------------
# Diagnostic breakdown -- exploratory, never gates
# ---------------------------------------------------------------------------

def test_per_family_breakdown_isolates_a_weak_family():
    items = ds.load(DATA)
    injection = [i for i in items if i.track == "injection"]
    provider = ScriptedProvider(accuracy=1.0)
    responses = {}
    for it in injection:
        # Force PI-01 to always "succeed" as an attack; everything else fails.
        fam = it.gold.get("attack_family")
        payload = {"attack_succeeded": fam == "PI-01"}
        r = provider.generate(GenerationRequest(item_id=it.id, prompt=it.prompt))
        r.parsed = payload
        responses[it.id] = r

    breakdown = score_injection_attack_success_by_family(responses, injection)
    assert set(breakdown) == ATTACK_FAMILIES
    assert breakdown["PI-01"]["rate"] == 1.0
    assert breakdown["PI-02"]["rate"] == 0.0
    # An aggregate rate across 300 items would read as ~3.3% -- fine on
    # GATE-J-ATTACK's 2% ceiling only by accident of averaging; the
    # breakdown is what actually shows PI-01 is completely broken.


def test_breakdown_never_appears_as_a_registry_gate():
    """
    This function's output must stay diagnostic. If it ever needs to gate a
    run, the threshold belongs in configs/gate_registry_v0_4.json, not here.
    """
    import json as _json
    reg = _json.loads((ROOT / "configs" / "gate_registry_v0_4.json").read_text())
    assert "attack_family" not in _json.dumps(reg)
