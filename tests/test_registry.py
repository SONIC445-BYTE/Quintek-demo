"""
Model Registry: identity, persistence, and the lifecycle state machine.

Section 10 of the NVIDIA architecture spec requires that only ELIGIBLE or
PRODUCTION candidates can ever be selected by the router, and that a
candidate can't skip straight to PRODUCTION. These tests exercise the actual
state machine, not just its happy path.
"""

from __future__ import annotations

import pytest

from benchmark.registry import Registry, Status, ModelCandidate


@pytest.fixture
def registry(tmp_path):
    return Registry(tmp_path / "registry.json")


def test_register_creates_a_registered_candidate(registry):
    c = registry.register("nvidia", "meta/llama-3.1-70b-instruct", "1.0")
    assert c.status == Status.REGISTERED
    assert c.candidate_id.startswith("cand-")


def test_registering_identical_config_twice_returns_same_candidate(registry):
    """Same provider+model+version+prompt+retrieval+config = same candidate,
    per docs/CANDIDATE_DEFINITION.md."""
    a = registry.register("nvidia", "meta/llama-3.1-70b-instruct", "1.0",
                          prompt_version="p1")
    b = registry.register("nvidia", "meta/llama-3.1-70b-instruct", "1.0",
                          prompt_version="p1")
    assert a.candidate_id == b.candidate_id
    assert len(registry.all()) == 1


def test_different_prompt_version_is_a_different_candidate(registry):
    a = registry.register("nvidia", "meta/llama-3.1-70b-instruct", "1.0",
                          prompt_version="p1")
    b = registry.register("nvidia", "meta/llama-3.1-70b-instruct", "1.0",
                          prompt_version="p2")
    assert a.candidate_id != b.candidate_id


def test_full_lifecycle_happy_path(registry):
    c = registry.register("nvidia", "m", "1.0")
    for status in (Status.BENCHMARK_REQUIRED, Status.EVALUATING, Status.ELIGIBLE,
                   Status.PRODUCTION):
        c = registry.transition(c.candidate_id, status)
    assert c.status == Status.PRODUCTION


def test_cannot_skip_straight_to_production(registry):
    c = registry.register("nvidia", "m", "1.0")
    with pytest.raises(ValueError):
        registry.transition(c.candidate_id, Status.PRODUCTION)


def test_evaluating_can_fail(registry):
    c = registry.register("nvidia", "m", "1.0")
    registry.transition(c.candidate_id, Status.BENCHMARK_REQUIRED)
    registry.transition(c.candidate_id, Status.EVALUATING)
    c = registry.transition(c.candidate_id, Status.FAILED)
    assert c.status == Status.FAILED


def test_failed_is_terminal(registry):
    c = registry.register("nvidia", "m", "1.0")
    registry.transition(c.candidate_id, Status.BENCHMARK_REQUIRED)
    registry.transition(c.candidate_id, Status.EVALUATING)
    registry.transition(c.candidate_id, Status.FAILED)
    with pytest.raises(ValueError):
        registry.transition(c.candidate_id, Status.EVALUATING)


def test_eligible_candidates_excludes_registered_and_failed(registry):
    a = registry.register("nvidia", "model-a", "1.0")
    for s in (Status.BENCHMARK_REQUIRED, Status.EVALUATING, Status.ELIGIBLE):
        registry.transition(a.candidate_id, s)

    b = registry.register("nvidia", "model-b", "1.0")  # stays REGISTERED

    c = registry.register("nvidia", "model-c", "1.0")
    for s in (Status.BENCHMARK_REQUIRED, Status.EVALUATING, Status.FAILED):
        registry.transition(c.candidate_id, s)

    eligible_ids = {m.candidate_id for m in registry.eligible_candidates()}
    assert eligible_ids == {a.candidate_id}
    assert b.candidate_id not in eligible_ids
    assert c.candidate_id not in eligible_ids


def test_deprecated_is_terminal(registry):
    c = registry.register("nvidia", "m", "1.0")
    for s in (Status.BENCHMARK_REQUIRED, Status.EVALUATING, Status.ELIGIBLE,
              Status.DEPRECATED):
        registry.transition(c.candidate_id, s)
    with pytest.raises(ValueError):
        registry.transition(c.candidate_id, Status.EVALUATING)


def test_unknown_status_rejected(registry):
    c = registry.register("nvidia", "m", "1.0")
    with pytest.raises(ValueError):
        registry.transition(c.candidate_id, "SUPER_ELIGIBLE")


def test_transition_unknown_candidate_raises(registry):
    with pytest.raises(KeyError):
        registry.transition("cand-does-not-exist", Status.BENCHMARK_REQUIRED)


def test_with_capability_filters_and_stays_within_eligible(registry):
    a = registry.register("nvidia", "model-a", "1.0", capabilities=["medical_qa"])
    for s in (Status.BENCHMARK_REQUIRED, Status.EVALUATING, Status.ELIGIBLE):
        registry.transition(a.candidate_id, s)
    b = registry.register("nvidia", "model-b", "1.0", capabilities=["medical_qa"])
    # b stays REGISTERED -- has the capability but is not eligible yet.

    result = registry.with_capability("medical_qa")
    assert [c.candidate_id for c in result] == [a.candidate_id]


def test_registry_persists_across_instances(tmp_path):
    path = tmp_path / "registry.json"
    r1 = Registry(path)
    c = r1.register("nvidia", "m", "1.0")
    r1.transition(c.candidate_id, Status.BENCHMARK_REQUIRED)

    r2 = Registry(path)  # fresh instance, same file
    reloaded = r2.get(c.candidate_id)
    assert reloaded is not None
    assert reloaded.status == Status.BENCHMARK_REQUIRED


def test_registry_file_is_valid_json_after_every_write(tmp_path):
    import json
    path = tmp_path / "registry.json"
    r = Registry(path)
    r.register("nvidia", "m1", "1.0")
    r.register("nvidia", "m2", "1.0")
    # Must not be left as a .tmp file or a partial write.
    assert path.exists()
    data = json.loads(path.read_text())
    assert len(data) == 2
