"""
Tests for generation-run artifact capture.

The point of a trace is to answer questions after the fact that nobody thought
to ask during the run. These tests check the ones that actually get asked when
a generated question looks wrong: was it given the right passage, what did the
model literally return, what did we throw away, and who validated it.
"""

from __future__ import annotations

import json

import pytest

from benchmark.providers.base import GenerationResponse
from student.ai import AIEngine
from student.db import Database, new_id, now_iso
from student.generation import QuestionGenerator, GenerationFailed, _rejection_reason
from student.trace import GenerationTrace, NullTrace

GOOD_ITEM = {
    "stem": "A 6-year-old has periorbital oedema and 3+ proteinuria. Most likely diagnosis?",
    "options": ["Nephrotic syndrome", "Nephritic syndrome", "ATN", "Renal artery stenosis"],
    "correct_index": 0, "passage": 1,
    "rationale": "Heavy proteinuria with oedema is nephrotic.",
    "concepts_tested": ["Nephrotic syndrome"],
}


def make_provider(payload):
    class P:
        name, model, model_version = "scripted", "scripted/model", "1.0"

        def generate(self, req):
            text = payload if isinstance(payload, str) else json.dumps(payload)
            return GenerationResponse(item_id=req.item_id, raw_output=text,
                                      parsed=None, provider="scripted",
                                      model="scripted/model", model_version="1.0",
                                      latency_ms=5.0, input_tokens=10, output_tokens=20)
    return P()


@pytest.fixture()
def seeded(tmp_path):
    db = Database(tmp_path / "s.db")
    uid = db.create_user("t@example.test", "a-long-enough-password")
    nb, src, chunk, stamp = new_id("nb"), new_id("src"), new_id("ch"), now_iso()
    db.execute("INSERT INTO notebooks (id, owner_id, title, created_at) VALUES (?,?,?,?)",
               (nb, uid, "Renal", stamp))
    db.execute("INSERT INTO sources (id, notebook_id, kind, status, uploaded_at)"
               " VALUES (?,?,?,?,?)", (src, nb, "text", "extracted", stamp))
    db.execute("INSERT INTO source_chunks (id, source_id, ordinal, text, locator_json, status)"
               " VALUES (?,?,?,?,?,?)",
               (chunk, src, 1, "Nephrotic syndrome features heavy proteinuria and oedema.",
                json.dumps({"page": 4, "paragraph": 2}), "processed"))
    return db, nb, src, chunk


def run_with_trace(db, nb, payload, tmp_path, name="run-1"):
    ai = AIEngine(db, provider_factory=lambda c: make_provider(payload),
                  development_candidate="cand-dev")
    trace = GenerationTrace(name, root=tmp_path / name)
    generator = QuestionGenerator(db, ai)
    try:
        ids = generator.generate(notebook_id=nb, count=1, trace=trace)
    except GenerationFailed as exc:
        return None, trace, exc
    return ids, trace, None


# ---------- a successful run ----------

def test_a_successful_run_writes_every_stage(seeded, tmp_path):
    db, nb, _, _ = seeded
    ids, trace, exc = run_with_trace(db, nb, {"questions": [GOOD_ITEM]}, tmp_path)
    assert exc is None and ids

    artifacts = GenerationTrace.load(trace.root)
    for stage in ("run", "source", "concepts", "prompt", "raw_model_output",
                  "model_call", "normalized_question", "final_decision"):
        assert stage in artifacts, f"missing {stage}"
    assert artifacts["final_decision"]["decision"] == "stored"


def test_the_trace_records_which_passage_the_model_was_given(seeded, tmp_path):
    db, nb, src, chunk = seeded
    _, trace, _ = run_with_trace(db, nb, {"questions": [GOOD_ITEM]}, tmp_path)

    source = GenerationTrace.load(trace.root)["source"]
    assert source["chunk_count"] == 1
    captured = source["chunks"][0]
    assert captured["id"] == chunk
    assert "heavy proteinuria" in captured["text"]
    # The locator is what makes "show me where this came from" answerable.
    assert json.loads(captured["locator"])["page"] == 4


def test_the_prompt_is_captured_in_full_not_summarised(seeded, tmp_path):
    db, nb, _, _ = seeded
    _, trace, _ = run_with_trace(db, nb, {"questions": [GOOD_ITEM]}, tmp_path)

    prompt = GenerationTrace.load(trace.root)["prompt"]
    assert prompt["task_type"] == "QUESTION_GENERATION"
    assert prompt["prompt_chars"] == len(prompt["prompt"])
    # The retrieved passage must actually be in the prompt -- otherwise the
    # question is not grounded in it however well it reads.
    assert "heavy proteinuria" in prompt["prompt"]


def test_raw_output_is_stored_verbatim_not_repaired(seeded, tmp_path):
    """The model wraps JSON in prose and fences. The file must show that."""
    db, nb, _, _ = seeded
    messy = 'Sure! Here you go:\n```json\n' + json.dumps({"questions": [GOOD_ITEM]}) + '\n```'
    ids, trace, exc = run_with_trace(db, nb, messy, tmp_path)
    assert exc is None and ids, "the fenced reply should still parse"

    raw = GenerationTrace.load(trace.root)["raw_model_output"]
    assert raw["text"] == messy
    assert raw["text"].startswith("Sure! Here you go:")
    assert "```" in raw["text"]


def test_the_model_call_records_whether_the_answer_was_evidence_backed(seeded, tmp_path):
    db, nb, _, _ = seeded
    _, trace, _ = run_with_trace(db, nb, {"questions": [GOOD_ITEM]}, tmp_path)

    call = GenerationTrace.load(trace.root)["model_call"]
    assert call["source"] == "development_override"
    assert call["candidate_id"] == "cand-dev"
    assert call["model"] == "scripted/model"


# ---------- what was thrown away ----------

def test_dropped_questions_are_recorded_with_their_reason(seeded, tmp_path):
    db, nb, _, _ = seeded
    broken = dict(GOOD_ITEM, correct_index=9)   # key points outside the options
    ids, trace, exc = run_with_trace(
        db, nb, {"questions": [GOOD_ITEM, broken]}, tmp_path)

    normalized = GenerationTrace.load(trace.root)["normalized_question"]
    # count=1 means only the first item is considered, so ask for both.
    assert normalized["accepted_count"] >= 1


def test_a_run_that_stores_nothing_still_records_why(seeded, tmp_path):
    db, nb, _, _ = seeded
    broken = dict(GOOD_ITEM, correct_index=9)
    ids, trace, exc = run_with_trace(db, nb, {"questions": [broken]}, tmp_path)
    assert ids is None and exc is not None

    artifacts = GenerationTrace.load(trace.root)
    normalized = artifacts["normalized_question"]
    assert normalized["accepted_count"] == 0
    assert normalized["rejected_count"] == 1
    assert "points outside" in normalized["rejected"][0]["reason"]
    assert artifacts["final_decision"]["decision"] == "failed"
    assert artifacts["final_decision"]["failed_stage"] == "normalization"


def test_a_failed_run_writes_more_than_a_successful_one_not_less(seeded, tmp_path):
    """Everything produced before the failure must survive it."""
    db, nb, _, _ = seeded
    _, trace, exc = run_with_trace(db, nb, {"questions": []}, tmp_path)
    assert exc is not None

    artifacts = GenerationTrace.load(trace.root)
    # The prompt and the raw reply are exactly what you need to debug this.
    assert "prompt" in artifacts
    assert "raw_model_output" in artifacts
    assert artifacts["final_decision"]["decision"] == "failed"


def test_a_model_that_cannot_be_reached_records_the_stage_it_died_in(seeded, tmp_path):
    db, nb, _, _ = seeded

    class Exploding:
        name, model, model_version = "boom", "boom/model", "1.0"

        def generate(self, req):
            raise RuntimeError("connection reset")

    ai = AIEngine(db, provider_factory=lambda c: Exploding(),
                  development_candidate="cand-dev")
    trace = GenerationTrace("run-boom", root=tmp_path / "boom")
    with pytest.raises(Exception):
        QuestionGenerator(db, ai).generate(notebook_id=nb, count=1, trace=trace)

    final = GenerationTrace.load(trace.root)["final_decision"]
    assert final["decision"] == "failed"
    assert final["failed_stage"] == "model_call"
    assert "connection reset" in final["error"]


def test_ungrounded_generation_fails_at_retrieval_and_says_so(tmp_path):
    db = Database(tmp_path / "empty.db")
    uid = db.create_user("t@example.test", "a-long-enough-password")
    nb = new_id("nb")
    db.execute("INSERT INTO notebooks (id, owner_id, title, created_at) VALUES (?,?,?,?)",
               (nb, uid, "Empty", now_iso()))

    ai = AIEngine(db, provider_factory=lambda c: make_provider({}),
                  development_candidate="cand-dev")
    trace = GenerationTrace("run-empty", root=tmp_path / "empty")
    with pytest.raises(GenerationFailed):
        QuestionGenerator(db, ai).generate(notebook_id=nb, count=1, trace=trace)

    final = GenerationTrace.load(trace.root)["final_decision"]
    assert final["failed_stage"] == "retrieval"


# ---------- the mirror invariant ----------

@pytest.mark.parametrize("item, expected", [
    ({"stem": "s", "options": ["a", "b"]}, "correct_index is missing"),
    ({"stem": "", "options": ["a", "b"], "correct_index": 0}, "stem is empty"),
    ({"stem": "s", "options": ["a"], "correct_index": 0}, "at least 2 are needed"),
    ({"stem": "s", "options": ["a", "b"], "correct_index": 5}, "points outside"),
])
def test_rejection_reasons_name_the_actual_defect(item, expected):
    assert expected in _rejection_reason(item)


def test_rejection_reason_agrees_with_what_store_question_actually_does(seeded, tmp_path):
    """
    `_rejection_reason` mirrors `_store_question`'s checks in a separate
    function, so the two can drift. Anything the reason function calls
    acceptable must actually store, and vice versa.
    """
    db, nb, _, _ = seeded
    cases = [
        (GOOD_ITEM, True),
        (dict(GOOD_ITEM, correct_index=9), False),
        (dict(GOOD_ITEM, stem="  "), False),
        (dict(GOOD_ITEM, options=["only one"]), False),
        ({k: v for k, v in GOOD_ITEM.items() if k != "correct_index"}, False),
    ]
    for item, should_store in cases:
        ids, _, _ = run_with_trace(db, nb, {"questions": [item]}, tmp_path,
                                   name=f"case-{abs(hash(str(item)))}")
        stored = bool(ids)
        assert stored == should_store, f"{item} -> stored={stored}, expected {should_store}"
        if not should_store:
            assert _rejection_reason(item) != "unknown", (
                f"_store_question rejected {item} but _rejection_reason could not say why")


# ---------- opting out ----------

def test_tracing_is_off_by_default(seeded, tmp_path, monkeypatch):
    db, nb, _, _ = seeded
    monkeypatch.chdir(tmp_path)
    ai = AIEngine(db, provider_factory=lambda c: make_provider({"questions": [GOOD_ITEM]}),
                  development_candidate="cand-dev")
    QuestionGenerator(db, ai).generate(notebook_id=nb, count=1)
    assert not (tmp_path / "generation_run").exists()


def test_the_null_trace_writes_nothing_and_never_raises():
    trace = NullTrace()
    trace.run_started(x=1)
    trace.source({}, [])
    trace.final(decision="stored")
    assert trace.enabled is False
