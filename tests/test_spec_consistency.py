"""
Spec-consistency tests for PG Revision Benchmark v0.4.

Each test here exists because the corresponding defect was PRESENT in v0.3 and
shipped undetected. These are regression tests against the specification itself,
not against the implementation.

Run from the package root:  python -m pytest tests/ -v
"""

from pathlib import Path
import json
import re
import math

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "configs" / "gate_registry_v0_4.json"

# Private Use Area — where the obfuscated citation artifacts actually lived.
PUA = re.compile(r"[\ue000-\uf8ff]")
# Matches cite/turn tokens even when PUA characters are interleaved.
CITE_ARTIFACT = re.compile(r"[\ue000-\uf8ff]?cite[\ue000-\uf8ff]?turn\d", re.IGNORECASE)


def _registry():
    return json.loads(REGISTRY.read_text())


def _docs():
    """All prose/config files except this test file and the registry itself."""
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix not in {".md", ".yaml", ".yml"}:
            continue
        yield p


# ---------------------------------------------------------------------------
# DEFECT CLASS 1 — obfuscated generation artifacts
# ---------------------------------------------------------------------------

def test_no_citation_artifacts_anywhere():
    """
    v0.3 REGRESSION.

    v0.3's changelog claimed 'Known generated-text artifacts are removed'. They
    were not: two artifacts remained in DECISION_LOG.md and
    CONTAMINATION_PROTOCOL.md.

    Worse, v0.3's own test asserted `"citeturn0search1" not in s` and PASSED,
    because the real artifact contained Unicode Private Use Area characters
    between 'cite' and 'turn'. The literal string never matched. A green test
    sat two files away from the defect it existed to catch.

    This test matches on the PUA-tolerant pattern instead.
    """
    offenders = []
    for p in _docs():
        s = p.read_text(encoding="utf-8")
        if CITE_ARTIFACT.search(s):
            offenders.append(str(p.relative_to(ROOT)))
    assert not offenders, f"citation artifacts found in: {offenders}"


def test_no_private_use_area_characters():
    """
    Broader guard: PUA characters are invisible in rendered Markdown and are a
    reliable fingerprint of copied model output. They must never appear in a
    document claiming research-grade rigor.
    """
    offenders = []
    for p in _docs():
        s = p.read_text(encoding="utf-8")
        if PUA.search(s):
            offenders.append(str(p.relative_to(ROOT)))
    assert not offenders, f"private-use-area characters found in: {offenders}"


# ---------------------------------------------------------------------------
# DEFECT CLASS 2 — duplicated thresholds that drift
# ---------------------------------------------------------------------------

def test_registry_is_sole_source_of_thresholds():
    """
    v0.3 REGRESSION.

    Minimum sample sizes appeared in three places with three different values:
      Medical QA        400 (SAMPLE_SIZE doc) vs 500 (registry) vs 500 (yaml)
      Relationships     400 (SAMPLE_SIZE doc) vs 500 (registry)
      Fake mastery      '100 concepts x >=6' vs unlabelled 300 (registry)

    An implementer had no way to determine which was authoritative. v0.4 removes
    the duplication rather than reconciling it, because reconciled duplicates
    drift again at the next revision.

    This test asserts that prose documents do not restate registered min_n
    values as bare integers.
    """
    reg = _registry()
    registered_n = set()
    for spec in reg["tracks"].values():
        for key in ("min_n", "min_n_concepts", "min_n_base_items"):
            if key in spec:
                registered_n.add(int(spec[key]))

    # Documents permitted to discuss the numbers (they explain the history,
    # derive costs from them, or render illustrative scorecards).
    exempt = {
        "SAMPLE_SIZE_AND_STATISTICS.md",  # narrates the v0.3 conflict
        "REVIEW_CAPACITY.md",             # derives labour cost from the counts
        "SCORECARD_SPEC.md",              # illustrative example scorecard
        "SEMANTIC_DIVERSITY.md",          # narrates the 20% vs 8% conflict
        "V0_4_CHANGELOG.md",
        "TRACK_GATES.md",
        "BUDGET_PROTOCOL.md",
        "IMPLEMENTATION_STATUS.md",  # documents the min_n correction and its cause
    }

    offenders = []
    for p in _docs():
        if p.name in exempt or p.suffix in {".yaml", ".yml"}:
            continue
        s = p.read_text(encoding="utf-8")
        for n in registered_n:
            if re.search(rf"\b{n}\b", s):
                offenders.append(f"{p.relative_to(ROOT)} restates n={n}")
    assert not offenders, (
        "thresholds/min_n must live only in the gate registry: " + "; ".join(offenders)
    )


# ---------------------------------------------------------------------------
# DEFECT CLASS 3 — gates that are described but unenforceable
# ---------------------------------------------------------------------------

def test_all_mandatory_gates_present():
    """
    v0.3 REGRESSION.

    GATE_DERIVATION.md v0.3 specified a concept false-merge threshold of 0.03,
    but no such gate existed in the registry or TRACK_GATES.md. The most
    dangerous ontology failure mode — collapsing two clinically distinct
    concepts — was discussed, thresholded, and unenforceable.

    Family coverage had the same defect: defined per-concept, never aggregated
    into a run-level gate.
    """
    reg = _registry()
    required = {
        "A_medical_qa",
        "B_concept_extraction",
        "C_concept_resolution_f1",
        "C_concept_false_merge",      # added v0.4
        "D_relationships",
        "E_generation",
        "F_validation",
        "G_cross_subject",
        "H_fake_mastery_duplicates",
        "H_fake_mastery_coverage",    # added v0.4
        "I_robustness",
        "J_injection",
        "J_injection_tool_violations",
        "safety_override_cme",
    }
    missing = required - set(reg["tracks"])
    assert not missing, f"mandatory gates absent from registry: {missing}"


def test_every_gate_is_well_formed():
    reg = _registry()
    for name, spec in reg["tracks"].items():
        assert spec.get("min_n", 0) > 0, f"{name}: min_n must be positive"
        assert spec.get("n_unit"), f"{name}: n_unit must be declared (v0.3 omitted units)"
        assert spec.get("direction") in {"lower", "upper", "equal"}, f"{name}: bad direction"
        assert "threshold" in spec, f"{name}: missing threshold"
        assert spec.get("ci"), f"{name}: missing CI method"


def test_zero_event_gates_have_upper_bound():
    """
    v0.3 REGRESSION.

    v0.3 defined the safety gate as direction 'equal', threshold 0, ci
    'exact_binomial' — with no maximum tolerated upper bound. That is not
    implementable as a pass/fail rule: zero observed events is compatible with a
    wide range of true rates, so '0 CMEs' at n=50 and at n=500 would both read
    as PASS despite differing by an order of magnitude in evidential strength.
    """
    reg = _registry()
    for name, spec in reg["tracks"].items():
        if spec["direction"] == "equal" and spec["threshold"] == 0:
            assert "max_tolerated_upper_bound" in spec, (
                f"{name}: zero-event gate needs max_tolerated_upper_bound, "
                "otherwise min_n is decorative"
            )


def test_safety_min_n_actually_achieves_its_upper_bound():
    """
    The registered min_n must be large enough that a zero-event result clears
    the stated upper bound. If it isn't, the gate can never pass and the number
    was chosen without arithmetic.

    Exact one-sided Clopper-Pearson upper bound with 0 events:  1 - alpha^(1/n)
    """
    reg = _registry()
    for name, spec in reg["tracks"].items():
        ub = spec.get("max_tolerated_upper_bound")
        if ub is None:
            continue
        n = spec["min_n"]
        achieved = 1 - 0.05 ** (1 / n)
        assert achieved <= ub, (
            f"{name}: at min_n={n}, zero events yields upper bound "
            f"{achieved:.5f} which exceeds the tolerated {ub}. "
            f"min_n must be at least {math.ceil(math.log(0.05) / math.log(1 - ub))}."
        )


# ---------------------------------------------------------------------------
# DEFECT CLASS 4 — integrity treated as a score rather than a precondition
# ---------------------------------------------------------------------------

def test_integrity_is_precondition_not_score():
    reg = _registry()
    assert "integrity_preconditions" in reg
    assert "checks" in reg["integrity_preconditions"]
    # Integrity must not appear as a scored track with a numeric threshold.
    for name in reg["tracks"]:
        assert "integrity" not in name.lower(), (
            "integrity must not be a scored track; it is a validity precondition "
            "(see docs/SCORECARD_SPEC.md)"
        )


def test_outcome_states_complete():
    reg = _registry()
    required = {
        "PASS", "CONDITIONAL", "FAIL", "NO_PASS_CAPABILITY_GAP",
        "INCOMPLETE", "INVALID_RUN", "NOT_VALID_FOR_PRODUCTION_PASS", "UNEVALUABLE",
    }
    missing = required - set(reg["outcome_states"])
    assert not missing, f"outcome states missing: {missing}"


def test_calibration_state_blocks_pass():
    """
    Thresholds are explicitly uncalibrated engineering starting points. A run
    against uncalibrated gates must not be able to yield an official PASS.
    """
    reg = _registry()
    assert reg["calibration_state"].startswith("UNCALIBRATED"), (
        "if calibration is complete, this assertion should be updated in the same "
        "commit that freezes the thresholds — deliberately noisy"
    )


# ---------------------------------------------------------------------------
# DEFECT CLASS 5 — budget that cannot cover its own required sample sizes
# ---------------------------------------------------------------------------

def test_budget_covers_one_full_run():
    """
    v0.3 REGRESSION.

    v0.3 set a single ceiling of 25,000 calls without declaring whether it was
    per-candidate or global. Measured against v0.3's own registered sample
    sizes, one full run costs roughly 9,200 calls — so the ceiling silently
    capped a bake-off at two candidates, in a benchmark whose stated purpose is
    comparing candidates.
    """
    import yaml  # noqa: PLC0415

    cfg = yaml.safe_load((ROOT / "configs" / "v0_4.yaml").read_text())
    reg = _registry()

    assert cfg["budget"]["scope"] in {"per_candidate", "global"}, (
        "budget scope must be declared explicitly"
    )

    base = sum(
        s["min_n"] for s in reg["tracks"].values()
        if s.get("mandatory") and s["metric"] != "confirmed_critical_medical_errors"
    )
    judge_overhead = base * 0.45
    variance = base * 0.10
    retries = base * 0.05
    projected = base + judge_overhead + variance + retries

    ceiling = cfg["budget"]["per_candidate"]["max_calls"]
    assert ceiling >= projected, (
        f"per-candidate budget {ceiling} cannot cover projected {projected:.0f} calls"
    )
