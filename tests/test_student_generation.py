"""
Phases 5 and 6: grounded question generation, and independent validation.
"""

from __future__ import annotations

import json

import pytest

from student.ai import AIEngine
from student.concepts import ConceptStore
from student.db import Database, now_iso
from student.generation import AIConceptExtractor, GenerationFailed, QuestionGenerator
from student.validation import QuestionValidator, ValidationSkipped


class _Scripted:
    """Returns a queued reply per call, and records the prompts it saw."""
    name, model, model_version = "scripted", "test-model", "1.0"

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts: list[str] = []

    def generate(self, request):
        from benchmark.providers.base import GenerationResponse
        from student.ai import extract_json
        self.prompts.append(request.prompt)
        reply = self.replies.pop(0) if self.replies else "{}"
        return GenerationResponse(
            item_id=request.item_id, raw_output=reply, parsed=extract_json(reply),
            provider=self.name, model=self.model, model_version=self.model_version,
            latency_ms=5.0, input_tokens=1, output_tokens=1, error=None, attempts=1)


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "q.db")


@pytest.fixture
def seeded(db):
    """A notebook with one ingested source chunk and two concepts."""
    uid = db.create_user("l@example.com", "correct-horse")
    db.execute("INSERT INTO notebooks (id,owner_id,title,subject,created_at)"
               " VALUES ('nb',?,'Renal','Medicine',?)", (uid, now_iso()))
    db.execute("INSERT INTO sources (id,notebook_id,kind,status,uploaded_at)"
               " VALUES ('src','nb','text','extracted',?)", (now_iso(),))
    db.execute("INSERT INTO source_chunks (id,source_id,ordinal,text,locator_json,status)"
               " VALUES ('chk','src',1,?,'{\"page\": 3}','processed')",
               ("In pre-renal AKI the fractional excretion of sodium is below 1%, "
                "whereas intrinsic AKI shows values above 2%.",))
    store = ConceptStore(db)
    a = store.resolve_or_create("Pre-renal AKI", subject="Medicine")
    b = store.resolve_or_create("Fractional excretion of sodium", subject="Medicine")
    for cid in (a, b):
        store.link_to_source("src", cid, "chk")
        store.link_to_notebook("nb", cid)
    return uid, a, b


def _engine(db, provider, candidate="cand-dev"):
    return AIEngine(db, provider_factory=lambda c: provider, development_candidate=candidate)


# ---------------------------------------------------------------------------
# Grounding
# ---------------------------------------------------------------------------

def test_generation_without_source_passages_is_refused(db):
    """Ungrounded generation is the invention the grounding rule exists to
    prevent, so it is refused rather than attempted."""
    uid = db.create_user("l@example.com", "correct-horse")
    db.execute("INSERT INTO notebooks (id,owner_id,title,created_at) VALUES ('nb',?,'N',?)",
               (uid, now_iso()))
    gen = QuestionGenerator(db, _engine(db, _Scripted([])))
    with pytest.raises(GenerationFailed, match="no source passages"):
        gen.generate(notebook_id="nb", count=3)


def test_the_prompt_carries_passages_targets_related_and_the_grounding_rule(db, seeded):
    _, a, b = seeded
    provider = _Scripted(['{"questions": []}'])
    gen = QuestionGenerator(db, _engine(db, provider))
    ConceptStore(db).relate(a, b, "measured_by")

    with pytest.raises(GenerationFailed):
        gen.generate(notebook_id="nb", count=1, concept_ids=[a])

    prompt = provider.prompts[0]
    assert "fractional excretion of sodium is below 1%" in prompt  # the passage
    assert "Pre-renal AKI" in prompt                                # target concept
    assert "RELATED CONCEPTS" in prompt                             # graph context
    assert "must be answerable from the SOURCE PASSAGES alone" in prompt
    assert '"page": 3' in prompt                                    # locator travels too


def test_demonstrations_are_marked_style_only_and_the_rule_is_repeated(db, seeded):
    """A model lifting a clinical value out of a demo is a fabricated fact
    wearing the costume of a grounded one, so the rule is stated twice."""
    uid, a, _ = seeded
    db.execute("INSERT INTO question_demos (id,owner_id,title,question,stem_structure,"
               "distractor_strategy,created_at) VALUES ('d1',?,'Vignette','A 62-year-old "
               "with ferritin 9 ng/mL...','clinical vignette','near-miss values',?)",
               (uid, now_iso()))
    provider = _Scripted(['{"questions": []}'])
    gen = QuestionGenerator(db, _engine(db, provider))
    with pytest.raises(GenerationFailed):
        gen.generate(notebook_id="nb", count=1, concept_ids=[a], demo_ids=["d1"])

    prompt = provider.prompts[0]
    assert prompt.count("STYLE ONLY") >= 2
    assert "Never reuse a clinical fact" in prompt
    assert "clinical vignette" in prompt          # the shape is passed through
    assert "A 62-year-old with ferritin" in prompt


# ---------------------------------------------------------------------------
# Storage and provenance
# ---------------------------------------------------------------------------

def test_a_generated_question_keeps_full_provenance(db, seeded):
    _, a, _ = seeded
    reply = json.dumps({"questions": [{
        "stem": "A patient has FeNa of 0.4%. What does this indicate?",
        "options": ["Pre-renal AKI", "Intrinsic AKI", "Post-renal AKI", "Normal"],
        "correct_index": 0, "rationale": "FeNa below 1% indicates pre-renal.",
        "concepts_tested": ["Pre-renal AKI"], "passage": 1}]})
    gen = QuestionGenerator(db, _engine(db, _Scripted([reply])))
    ids = gen.generate(notebook_id="nb", count=1, concept_ids=[a], demo_ids=["d1"])

    q = db.query_one("SELECT * FROM questions WHERE id = ?", (ids[0],))
    assert q["source_id"] == "src" and q["chunk_id"] == "chk"
    assert q["generated_by_candidate_id"] == "cand-dev"
    assert q["prompt_version"] == "gen-v1"
    assert json.loads(q["demo_ids_json"]) == ["d1"]
    assert q["validation_status"] == "pending", "nothing is approved on the way in"

    linked = db.query("SELECT concept_id FROM question_concepts WHERE question_id = ?", (ids[0],))
    assert [r["concept_id"] for r in linked] == [a]


def test_a_malformed_question_is_dropped_not_stored_broken(db, seeded):
    """A key pointing outside its own options can never be answered."""
    _, a, _ = seeded
    reply = json.dumps({"questions": [
        {"stem": "Bad", "options": ["only one"], "correct_index": 0},
        {"stem": "Also bad", "options": ["a", "b"], "correct_index": 7},
        {"stem": "Good one?", "options": ["a", "b"], "correct_index": 1, "passage": 1},
    ]})
    gen = QuestionGenerator(db, _engine(db, _Scripted([reply])))
    ids = gen.generate(notebook_id="nb", count=3, concept_ids=[a])
    assert len(ids) == 1
    assert db.query_one("SELECT stem FROM questions WHERE id=?", (ids[0],))["stem"] == "Good one?"


def test_all_malformed_means_failure_not_silent_success(db, seeded):
    _, a, _ = seeded
    reply = json.dumps({"questions": [{"stem": "x", "options": ["a"], "correct_index": 0}]})
    gen = QuestionGenerator(db, _engine(db, _Scripted([reply])))
    with pytest.raises(GenerationFailed, match="well-formed"):
        gen.generate(notebook_id="nb", count=1, concept_ids=[a])


# ---------------------------------------------------------------------------
# Concept extraction feeding the graph
# ---------------------------------------------------------------------------

def test_extraction_creates_concepts_relationships_and_links(db, seeded):
    reply = json.dumps({
        "concepts": [{"name": "Hepcidin", "description": "Iron regulator"},
                     {"name": "Iron absorption", "description": "Duodenal uptake"}],
        "relationships": [{"from": "Hepcidin", "to": "Iron absorption",
                           "type": "mechanism_of", "confidence": 0.9}]})
    extractor = AIConceptExtractor(db, _engine(db, _Scripted([reply])))
    extractor.extract_for_chunk(source_id="src", chunk_id="chk", text="...", locator={})

    store = ConceptStore(db)
    hep = store.find("Hepcidin")
    assert hep and store.find("Iron absorption")
    assert [n["relation_type"] for n in store.neighbours(hep)] == ["mechanism_of"]
    assert db.query_one("SELECT COUNT(*) c FROM source_concepts WHERE concept_id=?",
                        (hep,))["c"] == 1
    assert db.query_one("SELECT COUNT(*) c FROM notebook_concepts WHERE concept_id=?",
                        (hep,))["c"] == 1


def test_an_unknown_relation_type_is_skipped_not_stored(db, seeded):
    reply = json.dumps({"concepts": [{"name": "A"}, {"name": "B"}],
                        "relationships": [{"from": "A", "to": "B", "type": "vibes_with"}]})
    extractor = AIConceptExtractor(db, _engine(db, _Scripted([reply])))
    extractor.extract_for_chunk(source_id="src", chunk_id="chk", text="...", locator={})
    store = ConceptStore(db)
    assert store.neighbours(store.find("A")) == []


# ---------------------------------------------------------------------------
# Validation independence
# ---------------------------------------------------------------------------

def _store_question(db, generated_by="cand-gen"):
    db.execute("INSERT INTO questions (id,primary_notebook_id,stem,options_json,correct_index,"
               "chunk_id,generated_by_candidate_id,generated_at) VALUES"
               " ('q','nb','Stem?','[\"a\",\"b\"]',1,'chk',?,?)", (generated_by, now_iso()))


def test_validation_refuses_when_only_the_generator_is_available(db, seeded):
    """An approval from the model that wrote the question means nothing, and a
    meaningless approval is worse than none because it looks like a real one."""
    _store_question(db, generated_by="cand-dev")
    validator = QuestionValidator(db, _engine(db, _Scripted([]), candidate="cand-dev"))
    with pytest.raises(ValidationSkipped, match="means nothing"):
        validator.validate("q")
    assert db.query_one("SELECT validation_status FROM questions WHERE id='q'"
                        )["validation_status"] == "pending"


def test_the_validator_never_sees_the_generators_rationale(db, seeded):
    db.execute("INSERT INTO questions (id,primary_notebook_id,stem,options_json,correct_index,"
               "rationale,chunk_id,generated_by_candidate_id,generated_at) VALUES"
               " ('q','nb','Stem?','[\"a\",\"b\"]',1,'BECAUSE-I-REASONED-THIS-WAY','chk',"
               "'cand-gen',?)", (now_iso(),))
    provider = _Scripted([json.dumps({"checks": {k: True for k, _ in
                                                 __import__("student.validation",
                                                            fromlist=["CHECKS"]).CHECKS},
                                      "issues": [], "verdict": "approved"})])
    QuestionValidator(db, _engine(db, provider, candidate="cand-val")).validate("q")
    assert "BECAUSE-I-REASONED-THIS-WAY" not in provider.prompts[0]
    assert "In pre-renal AKI" in provider.prompts[0], "the source passage must be supplied"


def test_a_failed_check_flags_the_question_and_keeps_the_reasons(db, seeded):
    _store_question(db)
    from student.validation import CHECKS
    checks = {k: True for k, _ in CHECKS}
    checks["key_is_right"] = False
    provider = _Scripted([json.dumps({"checks": checks,
                                      "issues": ["option B is not clearly best"],
                                      "verdict": "approved"})])
    result = QuestionValidator(db, _engine(db, provider, candidate="cand-val")).validate("q")

    # The verdict comes from the checks, not from the model's own summary line.
    assert result["status"] == "flagged"
    assert result["failed_checks"] == ["key_is_right"]

    row = db.query_one("SELECT validation_status, validation_json FROM questions WHERE id='q'")
    assert row["validation_status"] == "flagged"
    assert "not clearly best" in row["validation_json"]


def test_a_flagged_question_is_kept_not_deleted(db, seeded):
    """Deleting it would destroy the evidence that the generator makes this
    kind of error -- which is what the failure-analysis screens exist to show."""
    _store_question(db)
    from student.validation import CHECKS
    checks = {k: False for k, _ in CHECKS}
    provider = _Scripted([json.dumps({"checks": checks, "issues": ["everything"]})])
    QuestionValidator(db, _engine(db, provider, candidate="cand-val")).validate("q")
    assert db.query_one("SELECT COUNT(*) c FROM questions WHERE id='q'")["c"] == 1


def test_an_unreachable_validator_leaves_the_question_pending(db, seeded):
    """Never approved by default -- that is how unchecked questions reach a
    learner."""
    _store_question(db)

    class Failing(_Scripted):
        def generate(self, request):
            from benchmark.providers.base import GenerationResponse
            return GenerationResponse(
                item_id=request.item_id, raw_output="", parsed=None, provider="s",
                model="m", model_version="1", latency_ms=1.0, input_tokens=None,
                output_tokens=None, error="unreachable", attempts=3)

    from student.ai import AICallFailed
    validator = QuestionValidator(db, _engine(db, Failing([]), candidate="cand-val"))
    with pytest.raises(AICallFailed):
        validator.validate("q")
    assert db.query_one("SELECT validation_status FROM questions WHERE id='q'"
                        )["validation_status"] == "pending"
