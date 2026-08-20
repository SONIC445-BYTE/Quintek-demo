"""
Tests for the corpus contract.

The single most important test in this file is the one asserting that a
model-authored item cannot be marked gold. Everything else in this repository
that reports an accuracy number depends on it: if model-authored gold were
scorable, a run would measure agreement between a model and a model and
present it as evidence, and the number would be indistinguishable from a real
one all the way up to the learner-facing transparency screen.
"""

from __future__ import annotations

import json

import pytest

from benchmark.corpus import (
    DEFECT_CLASSES, FACETS, CorpusError, coverage, defect_coverage, load, parse_item,
)

BASE = {
    "id": "t-001", "subject": "PSM", "topic": "Screening", "concept": "Sensitivity",
    "facet": "definition", "question_type": "mcq", "difficulty": "pg_entry",
    "stem": "Sensitivity is the proportion of:",
    "options": ["People with disease who test positive", "People without disease who test negative"],
    "correct_index": 0, "explanation": "TP / (TP + FN).",
    "reference": "Park's Textbook, Screening chapter",
}


def item(**overrides):
    return dict(BASE, **overrides)


# ---------- THE RULE ----------

def test_a_model_authored_item_cannot_be_gold():
    with pytest.raises(CorpusError, match="cannot be marked gold_standard"):
        parse_item(item(provenance="model_authored", gold_standard=True))


def test_the_refusal_explains_the_circularity_rather_than_just_saying_no():
    with pytest.raises(CorpusError) as excinfo:
        parse_item(item(provenance="model_authored", gold_standard=True))
    message = str(excinfo.value)
    assert "agreement between a model and a model" in message
    # And it names both ways forward, so the rule is not merely obstructive.
    assert "gold_standard: false" in message
    assert "model_authored_expert_reviewed" in message


def test_there_is_no_override_flag(monkeypatch):
    """A rule with an escape hatch is a suggestion."""
    for attempt in ({"force": True}, {"override": True}, {"allow_model_gold": True}):
        with pytest.raises(CorpusError):
            parse_item(item(provenance="model_authored", gold_standard=True, **attempt))


def test_a_model_authored_item_is_fine_as_a_development_item():
    parsed = parse_item(item(provenance="model_authored"))
    assert parsed.gold_standard is False


def test_expert_authored_items_may_be_gold():
    assert parse_item(item(provenance="expert_authored", gold_standard=True)).gold_standard


def test_expert_review_promotes_a_model_authored_item_only_with_a_named_reviewer():
    with pytest.raises(CorpusError, match="An unnamed reviewer is not a reviewer"):
        parse_item(item(provenance="model_authored_expert_reviewed", gold_standard=True))

    parsed = parse_item(item(provenance="model_authored_expert_reviewed", gold_standard=True,
                             reviewed_by="Dr A Bose", reviewed_at="2026-08-20"))
    assert parsed.gold_standard is True


def test_a_gold_item_needs_a_reference():
    with pytest.raises(CorpusError, match="cannot be challenged"):
        parse_item(item(provenance="expert_authored", gold_standard=True, reference=""))


def test_a_defective_item_is_never_gold():
    with pytest.raises(CorpusError, match="ground truth is the defect"):
        parse_item(item(provenance="expert_authored", gold_standard=True,
                        defect_class="wrong_key"))


# ---------- structural validity ----------

@pytest.mark.parametrize("field", ["id", "subject", "topic", "concept", "facet",
                                   "question_type", "difficulty", "stem", "explanation",
                                   "provenance"])
def test_every_required_field_is_required(field):
    raw = item(provenance="model_authored")
    raw.pop(field)
    with pytest.raises(CorpusError, match=field):
        parse_item(raw)


def test_a_key_pointing_outside_the_options_is_refused():
    with pytest.raises(CorpusError, match="points outside"):
        parse_item(item(provenance="model_authored", correct_index=7))


def test_fewer_than_two_options_is_refused():
    with pytest.raises(CorpusError, match="at least 2 options"):
        parse_item(item(provenance="model_authored", options=["only one"]))


def test_an_unknown_facet_is_refused():
    with pytest.raises(CorpusError, match="facet"):
        parse_item(item(provenance="model_authored", facet="vibes"))


def test_an_unknown_defect_class_is_refused():
    with pytest.raises(CorpusError, match="defect_class"):
        parse_item(item(provenance="model_authored", defect_class="just_bad"))


# ---------- loading ----------

def test_a_duplicate_id_is_refused(tmp_path):
    path = tmp_path / "c.jsonl"
    path.write_text("\n".join([json.dumps(item(provenance="model_authored")),
                               json.dumps(item(provenance="model_authored"))]))
    with pytest.raises(CorpusError, match="duplicate id"):
        load(path)


def test_loading_fails_fast_rather_than_skipping_bad_items(tmp_path):
    """
    A corpus that silently drops what it could not parse reports a smaller n
    than it was asked for, and nothing downstream can tell that apart from a
    genuinely smaller corpus.
    """
    path = tmp_path / "c.jsonl"
    path.write_text("\n".join([
        json.dumps(item(id="a", provenance="model_authored")),
        json.dumps(item(id="b", provenance="model_authored", correct_index=99)),
        json.dumps(item(id="c", provenance="model_authored")),
    ]))
    with pytest.raises(CorpusError, match=":2:"):
        load(path)


def test_blank_lines_and_comments_are_skipped(tmp_path):
    path = tmp_path / "c.jsonl"
    path.write_text("// a note\n\n" + json.dumps(item(provenance="model_authored")) + "\n\n")
    assert len(load(path)) == 1


# ---------- the shipped corpus ----------

def test_the_development_corpus_loads():
    items = load("corpus/development.jsonl")
    assert len(items) >= 30


def test_the_development_corpus_claims_no_gold():
    report = coverage(load("corpus/development.jsonl"))
    assert report["gold_count"] == 0
    assert report["scorable_as_gold"] is False
    assert "cannot score a model's accuracy" in report["note"]


def test_every_development_concept_covers_all_three_facets():
    """
    A concept present only as a definition supports "the model knows the
    name", not "the model understands the concept".
    """
    report = coverage(load("corpus/development.jsonl"))
    assert report["concepts_missing_facets"] == {}
    assert report["concepts"] >= 10
    assert set(report["by_facet"]) == set(FACETS)


def test_the_adversarial_battery_covers_every_defect_class():
    report = defect_coverage(load("corpus/adversarial.jsonl"))
    assert report["missing"] == [], f"unexercised defect classes: {report['missing']}"
    assert report["covered"] == len(DEFECT_CLASSES)


def test_every_adversarial_item_declares_what_is_wrong_with_it():
    for entry in load("corpus/adversarial.jsonl"):
        assert entry.defect_class, f"{entry.id} has no defect_class"
        assert entry.defect_note, f"{entry.id} does not say what is wrong with it"
        assert entry.is_negative


def test_no_shipped_corpus_file_smuggles_in_a_gold_claim():
    for path in ("corpus/development.jsonl", "corpus/adversarial.jsonl"):
        assert all(not i.gold_standard for i in load(path)), path
