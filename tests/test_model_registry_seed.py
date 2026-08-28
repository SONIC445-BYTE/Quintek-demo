"""
The committed, persistent configs/model_registry.json seed.

This is real data (produced by tools_seed_model_registry.py against a live
GET /v1/models call), not test fixture data -- these tests guard the
specific honesty property that matters here: every seeded candidate is
REGISTERED and nothing has been fabricated into ELIGIBLE/PRODUCTION without
a real benchmark run to justify it.
"""

from __future__ import annotations

from pathlib import Path

from benchmark.registry import Registry, Status

ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = ROOT / "configs" / "model_registry.json"


def test_seed_file_exists_and_is_not_gitignored():
    assert REGISTRY_PATH.exists()
    gitignore = (ROOT / ".gitignore").read_text().splitlines()
    # The bare "registry.json" ignore rule (for ad hoc local registries)
    # must not shadow this committed, differently-named file.
    assert "model_registry.json" not in gitignore


def test_seed_loads_via_registry():
    registry = Registry(REGISTRY_PATH)
    candidates = registry.all()
    assert len(candidates) == 5


def test_no_seeded_candidate_is_fabricated_eligible():
    """
    The single most important property of this file: registering a
    candidate is not the same as it having passed anything. Nothing here
    may be ELIGIBLE or PRODUCTION without a real benchmark run, because none
    has happened yet -- see tools_seed_model_registry.py's module docstring
    and README.md's "the corpus does not exist."

    DEPRECATED is allowed alongside REGISTERED, and is not a loosening: it is
    the terminal, unselectable state a candidate moves to when the provider
    withdraws its model, which NVIDIA did to meta/llama-3.3-70b-instruct on
    2026-08-26. This assertion used to demand exactly REGISTERED, which was
    tighter than its own stated purpose and left a withdrawn model no state to
    move to. What is checked is the property that matters -- and
    `eligible_candidates()` below is the guarantee, not the status spelling.
    """
    registry = Registry(REGISTRY_PATH)
    allowed = {Status.REGISTERED, Status.DEPRECATED}
    for c in registry.all():
        assert c.status in allowed, (
            f"{c.candidate_id} ({c.model_id}) is {c.status} -- no candidate may be "
            "marked eligible without a real benchmark run"
        )
    assert registry.eligible_candidates() == []


def test_every_candidate_has_a_real_nvidia_model_id():
    registry = Registry(REGISTRY_PATH)
    seen_model_ids = set()
    for c in registry.all():
        assert c.provider == "nvidia"
        assert "/" in c.model_id, f"'{c.model_id}' doesn't look like a NIM model id"
        assert c.model_id not in seen_model_ids, f"duplicate model_id {c.model_id}"
        seen_model_ids.add(c.model_id)


def test_medical_specialized_candidate_has_a_narrower_capability_set():
    """writer/palmyra-med-70b was deliberately registered without
    long_context/concept_extraction/relationship_extraction -- no evidence
    supports those for a medical-domain-tuned model, so the seed doesn't
    claim them."""
    registry = Registry(REGISTRY_PATH)
    med_candidates = [c for c in registry.all() if c.model_id == "writer/palmyra-med-70b"]
    assert len(med_candidates) == 1
    med = med_candidates[0]
    assert "medical_qa" in med.capabilities
    assert "long_context" not in med.capabilities
    assert "concept_extraction" not in med.capabilities


def test_general_purpose_candidates_share_the_full_capability_set():
    registry = Registry(REGISTRY_PATH)
    general = [c for c in registry.all() if c.model_id != "writer/palmyra-med-70b"]
    assert len(general) == 4
    for c in general:
        assert set(c.capabilities) == {
            "long_context", "concept_extraction", "reasoning", "relationship_extraction",
            "question_generation", "question_validation", "medical_qa", "knowledge_gap_detection",
        }


def test_no_safety_moderation_or_embedding_model_was_registered():
    """Per tools_seed_model_registry.py's module docstring: embedding,
    vision, and safety-moderation endpoints are not general-purpose
    candidates and must not appear here."""
    registry = Registry(REGISTRY_PATH)
    model_ids = {c.model_id for c in registry.all()}
    forbidden_substrings = ("embed", "guard", "vision", "-vl-", "nemoguard")
    for model_id in model_ids:
        for bad in forbidden_substrings:
            assert bad not in model_id.lower(), f"{model_id} looks like a specialized model, not general-purpose"


def test_router_correctly_finds_no_eligible_candidate_against_the_real_seed(tmp_path):
    """
    End-to-end honesty check: point a real Router at the real seeded
    registry (with an empty run archive, since no benchmark has run) and
    confirm it says "no eligible candidate" for every task -- not a
    fabricated selection.
    """
    from benchmark import analytics as an
    from benchmark.router import Router
    from benchmark.tasks import TaskType

    registry = Registry(REGISTRY_PATH)
    archive = an.RunArchive(tmp_path / "empty_runs")
    router = Router(registry, archive)

    for task in TaskType:
        result = router.select(task)
        assert result.selected_candidate is None, (
            f"{task} selected {result.selected_candidate} with zero benchmark evidence"
        )


def test_cli_serve_analytics_defaults_to_the_seeded_registry():
    """
    Parses real argv through benchmark.cli's actual parser (without starting
    a server -- serve() is monkeypatched) to confirm --registry defaults to
    the committed seed file rather than None.
    """
    from unittest.mock import patch
    from benchmark import cli as cli_module

    captured = {}

    def fake_serve(runs_root, *, host, port, routing_log_path, registry_path, **kwargs):
        captured["registry_path"] = registry_path
        captured.update(kwargs)

    with patch("benchmark.analytics_api.serve", fake_serve):
        cli_module.main(["serve-analytics"])

    assert captured["registry_path"] == str(REGISTRY_PATH)
