"""
Seeds configs/model_registry.json from what the providers are actually
serving.

    python3 tools_seed_model_registry.py --dry-run
    python3 tools_seed_model_registry.py --provider nvidia --limit 5

WHY THIS NO LONGER CARRIES A LIST OF MODELS
-------------------------------------------
It used to. Five NVIDIA ids, typed out, each with a comment explaining why it
was a sensible candidate. On 2026-08-28 four of the five were dead:

    meta/llama-3.3-70b-instruct              410 end of life 2026-08-26
    nvidia/llama-3.1-nemotron-70b-instruct   404 not found for this account
    google/gemma-3-12b-it                    404 not found for this account
    writer/palmyra-med-70b-32k               404 not found for this account
    openai/gpt-oss-120b                      timed out at 60s

Nothing said so, because a list in Python source cannot notice anything. The
candidate registry it seeds is what `benchmark/router.py` selects production
traffic from, so a stale list here is a stale answer at the far end of the
product.

The models now come from `benchmark/discovery.py`, which observed them, with a
timestamp, through a real call. Registering a model the registry knows is
retired is refused outright rather than warned about.

WHAT IS STILL TRUE
------------------
Every candidate is REGISTERED and nothing more. This script does NOT and MUST
NOT transition anything to ELIGIBLE -- that requires a real benchmark run
against real gold data. Registering a candidate says "this is a configuration
Quintek could evaluate"; it says nothing about whether it would pass. Until a
run promotes one, `Router.select()` correctly returns "no eligible candidate"
for every task, which is the intended behaviour of an honest registry with no
evidence yet, not a bug to work around.

`context_window` is left unset wherever the provider does not expose one. This
script does not fabricate a number it cannot verify, and `capabilities` below
is a REGISTRATION-TIME DECLARATION of what a candidate would be evaluated
for -- not a claim that it can. What a model has been shown to do lives in the
discovery registry as an OBSERVED capability claim, and the two are
deliberately different words.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from benchmark.discovery import (DEFAULT_REGISTRY_PATH, ROLE_REQUIREMENTS,
                                 DynamicModelRegistry)
from benchmark.registry import Registry

ROOT = Path(__file__).resolve().parent
REGISTRY_PATH = ROOT / "configs" / "model_registry.json"

# Capabilities correspond to TaskSpec.required_capabilities in benchmark/tasks.py.
GENERAL_CAPABILITIES = [
    "long_context", "concept_extraction", "reasoning", "relationship_extraction",
    "question_generation", "question_validation", "medical_qa", "knowledge_gap_detection",
]


def deprecate_retired(registry: Registry, models: DynamicModelRegistry) -> list[str]:
    """
    Retire the candidates whose model the provider has withdrawn.

    DEPRECATED, never deleted. "What was registered as a candidate in August,
    and why is it not any more" has to stay answerable, and a benchmark result
    naming a candidate the registry has forgotten is unauditable.
    """
    dead = {r.provider + ":" + r.model_id for r in models.retired()}
    moved = []
    for candidate in registry.all():
        key = f"{candidate.provider}:{candidate.model_id}"
        if key in dead and candidate.status != "DEPRECATED":
            registry.transition(candidate.candidate_id, "DEPRECATED")
            moved.append(f"{candidate.candidate_id}  {key}  -> DEPRECATED")
    return moved


def discover(provider: str, limit: int, role: str) -> tuple[list[dict], list[str]]:
    """
    Candidates from the discovery registry, and what was refused.

    Only models the registry has actually reached are offered, and a family
    cap keeps the pool from filling with checkpoints of one lineage -- a
    registry of five variants of one model measures that model five times.
    """
    path = Path(DEFAULT_REGISTRY_PATH)
    if not path.exists():
        raise SystemExit(
            f"no model registry at {path}. Run `python3 tools_discovery.py catalogue "
            "--providers nvidia` and then `probe` first: seeding candidates from "
            "nothing is how a list of dead model ids got into this file in the "
            "first place.")
    registry = DynamicModelRegistry(path)
    refused = [f"{r.key} is RETIRED ({r.retirement_reason[:60]})"
               for r in registry.retired() if not provider or r.provider == provider]

    # OBSERVED, not declared. A candidate registered on a catalogue's word --
    # or worse, on nothing, which is what a bare NVIDIA listing gives -- puts
    # an embedding model and an image model into the pool the production
    # router selects from. Requiring the probe is what keeps "it answered a
    # one-word prompt" from becoming "it can generate exam questions".
    requirements = dict(ROLE_REQUIREMENTS[role])
    requirements["require_observed"] = True
    requirements["providers"] = (provider,) if provider else None

    chosen, families = [], {}
    for record in registry.shortlist(limit=200, **requirements):
        family = record.family or record.model_id.split("/")[0]
        if families.get(family, 0) >= 2:
            continue
        families[family] = families.get(family, 0) + 1
        chosen.append(dict(provider=record.provider, model_id=record.model_id,
                           model_version=record.model_version or "1.0",
                           capabilities=list(GENERAL_CAPABILITIES),
                           context_window=record.context_window))
        if len(chosen) >= limit:
            break
    return chosen, refused


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", default="nvidia")
    parser.add_argument("--limit", type=int, default=5,
                        help="how many candidates to register; the guidance is "
                             "3-5 serious general-purpose models, not everything "
                             "a provider hosts")
    parser.add_argument("--role", default="validation",
                        choices=sorted(ROLE_REQUIREMENTS),
                        help="the capability floor a candidate must have been "
                             "OBSERVED to clear before it is registered")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--deprecate-retired", action="store_true",
                        help="move candidates whose model has been withdrawn to "
                             "DEPRECATED; they are never deleted")
    args = parser.parse_args()

    if args.deprecate_retired:
        path = Path(DEFAULT_REGISTRY_PATH)
        if not path.exists():
            raise SystemExit(f"no discovery registry at {path}")
        moved = deprecate_retired(Registry(REGISTRY_PATH), DynamicModelRegistry(path))
        for line in moved:
            print(line)
        print(f"{len(moved)} candidate(s) deprecated")
        if not args.dry_run:
            return

    candidates, refused = discover(args.provider, args.limit, args.role)
    for line in refused:
        print(f"refused: {line}")
    if not candidates:
        raise SystemExit(
            f"no {args.provider} model has been OBSERVED to meet the {args.role!r} "
            f"capability floor "
            f"({', '.join(ROLE_REQUIREMENTS[args.role]['required_capabilities'])}), "
            "so there is nothing honest to register. Run "
            "`python3 tools_discovery.py capability-probe --providers "
            f"{args.provider} --role {args.role}` first, then re-run this. "
            "Registering on a catalogue's word is how an embedding model ends up "
            "in the pool the production router selects from.")
    if args.dry_run:
        for spec in candidates:
            print(f"would register  {spec['provider']}/{spec['model_id']}")
        return

    registry = Registry(REGISTRY_PATH)
    for spec in candidates:
        candidate = registry.register(**spec)
        print(f"{candidate.candidate_id}  {candidate.provider}/{candidate.model_id}  "
              f"status={candidate.status}")
    print(f"\n{len(registry.all())} candidate(s) in {REGISTRY_PATH}")
    print("All REGISTERED only -- none are ELIGIBLE until a real benchmark run says so.")


if __name__ == "__main__":
    main()
