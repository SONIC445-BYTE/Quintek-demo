"""
Seeds configs/model_registry.json with an initial NVIDIA NIM candidate pool.

Run once (or re-run after editing CANDIDATES below -- registering an
identical configuration twice is a no-op, see registry.py's
_derive_candidate_id):

    python3 tools_seed_model_registry.py

Every candidate here is REGISTERED and nothing more. This script does NOT
and MUST NOT transition anything to ELIGIBLE -- that requires a real
benchmark run against real gold data, which docs/README.md is explicit does
not exist yet ("The corpus does not exist"). Registering a candidate says
"this is a configuration Quintek could evaluate"; it says nothing about
whether it would pass. Until a real run promotes a candidate,
Router.select() will correctly return "no eligible candidate" for every
task -- that is the intended behavior of an honest registry with no
benchmark evidence yet, not a bug to work around.

Model IDs and owning organizations below were read directly from a live
`GET /v1/models` call against https://integrate.api.nvidia.com on
2026-08-15 -- not guessed. `context_window` is left unset because that
endpoint doesn't expose it and this script won't fabricate a number it
can't verify.

Selection follows the "3-5 serious candidate models, general-purpose only"
guidance: NVIDIA hosts 100+ endpoints, most of them embedding
(nvidia/nv-embed-v1), vision (meta/llama-3.2-11b-vision-instruct), code
(mistralai/codestral-22b-instruct-v0.1), translation, or safety-moderation
models (meta/llama-guard-4-12b, nvidia/llama-3.1-nemoguard-8b-content-safety)
-- none of those are general-purpose chat candidates for Quintek's tasks and
none are registered here. If a safety-moderation capability is wanted later,
it should be modeled as its own capability/task type, not smuggled in as an
ordinary candidate.
"""

from __future__ import annotations

from pathlib import Path

from benchmark.registry import Registry

ROOT = Path(__file__).resolve().parent
REGISTRY_PATH = ROOT / "configs" / "model_registry.json"

# Capabilities correspond to TaskSpec.required_capabilities in benchmark/tasks.py.
GENERAL_CAPABILITIES = [
    "long_context", "concept_extraction", "reasoning", "relationship_extraction",
    "question_generation", "question_validation", "medical_qa", "knowledge_gap_detection",
]

CANDIDATES = [
    dict(
        provider="nvidia", model_id="meta/llama-3.3-70b-instruct", model_version="1.0",
        capabilities=GENERAL_CAPABILITIES,
        # Live-verified in this session against meta/llama-3.1-70b-instruct, same
        # family/API shape -- see IMPLEMENTATION_STATUS.md "Fifth pass" for the
        # actual request/response.
    ),
    dict(
        provider="nvidia", model_id="openai/gpt-oss-120b", model_version="1.0",
        capabilities=GENERAL_CAPABILITIES,
        # Different family from every Llama-lineage candidate here -- the
        # natural Tier-2 judge partner when a Llama-family candidate is being
        # scored (see docs/JUDGE_INDEPENDENCE.md and integrity.py:_judge_family).
    ),
    dict(
        provider="nvidia", model_id="google/gemma-3-12b-it", model_version="1.0",
        capabilities=GENERAL_CAPABILITIES,
        # Smaller/cheaper than the 70B-class candidates -- gives
        # COST_OPTIMIZED/LATENCY_OPTIMIZED routing policies something real to
        # differentiate on once cost/latency hints exist.
    ),
    dict(
        provider="nvidia", model_id="nvidia/llama-3.1-nemotron-70b-instruct", model_version="1.0",
        capabilities=GENERAL_CAPABILITIES,
        # NVIDIA's own RLHF-tuned lineage -- distinct training recipe from the
        # base Llama and OpenAI/Gemma candidates even though NIM hosts it.
    ),
    dict(
        provider="nvidia", model_id="writer/palmyra-med-70b", model_version="1.0",
        # Deliberately NOT the general capability set: this is a medical-domain
        # -tuned model with no confirmed general long-context/extraction
        # behavior. Registered narrower on purpose rather than assumed general
        # -purpose -- a real benchmark run decides the rest, not this script.
        capabilities=["medical_qa", "reasoning", "question_validation", "knowledge_gap_detection"],
    ),
]


def main() -> None:
    registry = Registry(REGISTRY_PATH)
    for spec in CANDIDATES:
        c = registry.register(**spec)
        print(f"{c.candidate_id}  {c.provider}/{c.model_id}  status={c.status}")
    print(f"\n{len(registry.all())} candidate(s) in {REGISTRY_PATH}")
    print("All REGISTERED only -- none are ELIGIBLE until a real benchmark run says so.")


if __name__ == "__main__":
    main()
