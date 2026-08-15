"""
Task-type taxonomy.

The Model Router (benchmark/router.py) receives one of these task types and
never a free-text string, so a typo can't silently create a task the
registry has no evidence for. Each task type maps to the benchmark gate(s)
that measure a candidate's fitness for it -- this is the join key between
"what does Quintek need to do" and "what did the benchmark measure."

Deliberately NOT product-layer entities: this module has no opinion about
notebooks, questions, or sources. It is the contract a product backend
(outside this repository -- see README.md's scope note) calls into.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TaskType(str, Enum):
    SOURCE_PROCESSING = "SOURCE_PROCESSING"
    CONCEPT_EXTRACTION = "CONCEPT_EXTRACTION"
    CONCEPT_RESOLUTION = "CONCEPT_RESOLUTION"
    RELATIONSHIP_EXTRACTION = "RELATIONSHIP_EXTRACTION"
    QUESTION_GENERATION = "QUESTION_GENERATION"
    QUESTION_VALIDATION = "QUESTION_VALIDATION"
    EXPLANATION = "EXPLANATION"
    KNOWLEDGE_GAP_EXTRACTION = "KNOWLEDGE_GAP_EXTRACTION"
    REVISION_SELECTION = "REVISION_SELECTION"


@dataclass(frozen=True)
class TaskSpec:
    task: TaskType
    gate_ids: tuple[str, ...]          # which registry gates evidence this task
    required_capabilities: tuple[str, ...] = ()


# The join between "what Quintek needs to do" and "what the benchmark
# measured" -- gate_ids reference configs/gate_registry_v0_4.json's own IDs,
# never restated thresholds, consistent with the rest of this harness.
TASK_SPECS: dict[TaskType, TaskSpec] = {
    TaskType.SOURCE_PROCESSING: TaskSpec(
        TaskType.SOURCE_PROCESSING, gate_ids=("GATE-B-F1",),
        required_capabilities=("long_context",),
    ),
    TaskType.CONCEPT_EXTRACTION: TaskSpec(
        TaskType.CONCEPT_EXTRACTION, gate_ids=("GATE-B-F1",),
        required_capabilities=("concept_extraction",),
    ),
    TaskType.CONCEPT_RESOLUTION: TaskSpec(
        TaskType.CONCEPT_RESOLUTION, gate_ids=("GATE-C-F1", "GATE-C-MERGE"),
        required_capabilities=("concept_extraction", "reasoning"),
    ),
    TaskType.RELATIONSHIP_EXTRACTION: TaskSpec(
        TaskType.RELATIONSHIP_EXTRACTION, gate_ids=("GATE-D-F1",),
        required_capabilities=("relationship_extraction",),
    ),
    TaskType.QUESTION_GENERATION: TaskSpec(
        TaskType.QUESTION_GENERATION, gate_ids=("GATE-E-RUBRIC",),
        required_capabilities=("question_generation",),
    ),
    TaskType.QUESTION_VALIDATION: TaskSpec(
        TaskType.QUESTION_VALIDATION, gate_ids=("GATE-F-FALSEAPPROVE",),
        required_capabilities=("question_validation", "reasoning"),
    ),
    TaskType.EXPLANATION: TaskSpec(
        TaskType.EXPLANATION, gate_ids=("GATE-A-ACC",),
        required_capabilities=("medical_qa",),
    ),
    TaskType.KNOWLEDGE_GAP_EXTRACTION: TaskSpec(
        TaskType.KNOWLEDGE_GAP_EXTRACTION, gate_ids=("GATE-G-LINK", "GATE-H-FAMILY"),
        required_capabilities=("knowledge_gap_detection",),
    ),
    TaskType.REVISION_SELECTION: TaskSpec(
        TaskType.REVISION_SELECTION, gate_ids=("GATE-I-RETENTION",),
        required_capabilities=("reasoning",),
    ),
}


def gate_ids_for(task: TaskType) -> tuple[str, ...]:
    return TASK_SPECS[task].gate_ids


def required_capabilities_for(task: TaskType) -> tuple[str, ...]:
    return TASK_SPECS[task].required_capabilities
