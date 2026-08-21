"""
Tests for the layered validator.

Most of these exist because of a specific way a validator can look good without
being good: a layer that turns its own outage into a PASS, a judge that grades
its own homework, a holdout scored until it agrees, a deterministic check whose
"no false flags" claim was untrue on formula options. Each of those is a test
here rather than a paragraph in a document.
"""

from __future__ import annotations

import json

import pytest

from benchmark.corpus import QUESTION_TYPES
from validator import (analysis, conformance, grounding, holdout, judge,
                       metrics, mutate, pipeline, review, runs, scripted,
                       structural)
from validator.devset import (ADJUDICATED, AGREED, CLEAN, DEFECTIVE, DISPUTED,
                              DevsetError, EDGE, assert_disjoint, load)

DEV = "corpus/validator_dev"
HOLDOUT = "corpus/validator_holdout"


@pytest.fixture(scope="module")
def dev():
    return load(DEV)


@pytest.fixture(scope="module")
def hold():
    return load(HOLDOUT)


def item(**overrides):
    base = {
        "id": "t-1", "subject": "Physiology", "topic": "T", "concept": "C",
        "facet": "definition", "question_type": "mcq", "difficulty": "pg_entry",
        "stem": "Which one is right?", "options": ["Alpha", "Beta", "Gamma", "Delta"],
        "correct_index": 0, "explanation": "Because the passage says Alpha.",
        "reference": "The supplied passage.", "provenance": "model_authored",
        "source_passage": "The correct answer here is Alpha, for reasons given at length.",
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------- the corpus

def test_development_set_is_one_hundred_cases_split_forty_forty_twenty(dev):
    summary = dev.summary()
    assert summary["total"] == 100
    assert summary["clean"] == 40
    assert summary["defective"] == 40
    assert summary["edge"] == 20


def test_every_defect_class_is_exercised_in_both_sets(dev, hold):
    assert dev.summary()["defect_classes_missing"] == []
    assert hold.summary()["defect_classes_missing"] == []


def test_both_arms_of_the_holdout_clear_the_minimum_for_a_rate_to_mean_anything(hold):
    assert len(hold.by_label(CLEAN)) >= metrics.MIN_ITEMS_PER_ARM
    assert len(hold.by_label(DEFECTIVE)) >= metrics.MIN_ITEMS_PER_ARM


def test_every_defective_item_names_the_clean_item_it_came_from(dev, hold):
    for devset in (dev, hold):
        clean_ids = {c.id for c in devset.by_label(CLEAN)}
        for case in devset.by_label(DEFECTIVE):
            assert case.derived_from in clean_ids


def test_edge_cases_are_in_neither_arm(dev):
    assert all(not c.in_arm for c in dev.by_label(EDGE))
    assert len(dev.arms) == 80


def test_development_and_holdout_share_no_item_stem_or_passage(dev, hold):
    assert_disjoint(dev, hold)


def test_a_defective_item_without_a_note_is_refused(tmp_path):
    path = tmp_path / "defects.jsonl"
    row = item(defect_class="wrong_key", derived_from="t-0")
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(DevsetError, match="defect_note"):
        from validator.devset import load_file
        load_file(path, DEFECTIVE)


def test_a_clean_item_carrying_a_defect_class_is_refused(tmp_path):
    path = tmp_path / "clean.jsonl"
    path.write_text(json.dumps(item(defect_class="wrong_key")) + "\n", encoding="utf-8")
    with pytest.raises(DevsetError, match="cannot be both"):
        from validator.devset import load_file
        load_file(path, CLEAN)


def test_an_edge_case_must_say_why_it_is_one(tmp_path):
    path = tmp_path / "edge.jsonl"
    path.write_text(json.dumps(item()) + "\n", encoding="utf-8")
    with pytest.raises(DevsetError, match="why it is an edge case"):
        from validator.devset import load_file
        load_file(path, EDGE)


# ------------------------------------------------------- controlled defects

def test_a_mutation_that_changes_more_than_it_declared_is_refused():
    source = item()
    bad = mutate.Mutation(target_id="t-1-def", source_id="t-1", defect_class="wrong_key",
                          operation=mutate.SET_STEM, note="claims to move the key")
    bad = mutate.Mutation(target_id="t-1-def", source_id="t-1", defect_class="wrong_key",
                          operation=mutate.SHIFT_KEY, note="n", payload={"by": 1})
    out = mutate.apply(source, bad)
    assert out["correct_index"] == 1
    assert out["stem"] == source["stem"]
    assert out["derived_from"] == "t-1"


def test_only_the_ungrounded_operation_may_touch_the_source_passage():
    for name, allowed in mutate.TOUCHES.items():
        if name == mutate.SET_PASSAGE:
            assert mutate.PASSAGE_FIELD in allowed
        else:
            assert mutate.PASSAGE_FIELD not in allowed


def test_shifting_the_key_by_a_full_turn_is_refused():
    with pytest.raises(mutate.MutationError, match="leaves it where it was"):
        mutate.apply(item(), mutate.Mutation(
            target_id="t-1-def", source_id="t-1", defect_class="wrong_key",
            operation=mutate.SHIFT_KEY, note="n", payload={"by": 4}))


def test_two_defects_may_not_come_from_one_clean_item():
    source = item()
    muts = [mutate.Mutation(f"t-1-def{i}", "t-1", "wrong_key", mutate.SHIFT_KEY, "n",
                            {"by": i}) for i in (1, 2)]
    with pytest.raises(mutate.MutationError, match="already mutated"):
        mutate.build([source], muts)


def test_a_mutation_needs_a_note_saying_what_is_now_wrong():
    with pytest.raises(mutate.MutationError, match="needs a note"):
        mutate.Mutation("t", "s", "wrong_key", mutate.SHIFT_KEY, "  ")


def test_the_shipped_defect_files_reproduce_from_their_mutation_specs():
    for root in (DEV, HOLDOUT):
        clean = [json.loads(l) for l in open(f"{root}/clean.jsonl") if l.strip()]
        built = mutate.build(clean, mutate.load_mutations(f"{root}/mutations.jsonl"))
        shipped = [json.loads(l) for l in open(f"{root}/defects.jsonl") if l.strip()]
        assert len(built) == len(shipped)
        for made, on_disk in zip(built, shipped):
            assert made["id"] == on_disk["id"]
            assert made["stem"] == on_disk["stem"]
            assert made["options"] == on_disk["options"]
            assert made["correct_index"] == on_disk["correct_index"]


# ------------------------------------------------------------ layer A

def test_layer_a_flags_nothing_on_any_clean_item_in_either_set(dev, hold):
    for devset in (dev, hold):
        for case in devset.by_label(CLEAN):
            result = structural.check(case.item.as_dict(), question_types=QUESTION_TYPES)
            assert result.ok, (case.id, result.failed_checks)


def test_two_different_formulae_are_not_the_same_option():
    result = structural.check(
        item(options=["Na - (Cl + HCO3)", "(Na + Cl) - HCO3", "Na - Cl", "Na + K"]),
        require_verifiable_reference=False)
    assert structural.DUPLICATE_OPTIONS not in result.failed_checks


def test_a_real_duplicate_is_still_caught():
    result = structural.check(item(options=["Alpha", "alpha!", "Beta", "Gamma"]))
    assert structural.DUPLICATE_OPTIONS in result.failed_checks


def test_a_key_pointing_outside_the_options_is_fatal():
    result = structural.check(item(correct_index=9))
    assert structural.KEY_OUT_OF_RANGE in result.failed_checks
    assert not result.ok


def test_a_precise_locator_in_a_citation_this_system_cannot_check_is_flagged():
    result = structural.check(item(reference="Harrison's, 21st edition, page 3122, Table 402-4."))
    assert structural.UNVERIFIABLE_REFERENCE in result.failed_checks


def test_a_named_reviewer_vouching_for_the_locator_exempts_it():
    result = structural.check(item(reference="Harrison's, 21st edition, page 3122.",
                                   reviewed_by="Dr A"))
    assert structural.UNVERIFIABLE_REFERENCE not in result.failed_checks


def test_a_book_level_citation_is_not_flagged():
    result = structural.check(item(reference="Gray's Anatomy for Students."))
    assert structural.UNVERIFIABLE_REFERENCE not in result.failed_checks


# ------------------------------------------------------------ layer B

def _reply(**kw):
    base = {"passage_addresses_question": True, "supported": ["A"],
            "evidence": {"A": "The correct answer here is Alpha"}, "reasoning": "r"}
    base.update(kw)
    return base


def _explained(**kw):
    base = {"contradicted": [], "absent": [], "gives_a_reason": True}
    base.update(kw)
    return base


def test_grounding_passes_an_item_whose_key_the_passage_supports():
    provider = scripted.ReplayProvider({"t-1:key": _reply(),
                                        "t-1:explanation": _explained()})
    assert grounding.check(item(), provider).verdict == metrics.PASSED


def test_grounding_flags_a_key_the_passage_does_not_support():
    provider = scripted.ReplayProvider({"t-1:key": _reply(supported=["B"],
                                                          evidence={"B": "The correct answer here is Alpha"}),
                                        "t-1:explanation": _explained()})
    result = grounding.check(item(), provider)
    assert result.verdict == metrics.FLAGGED
    assert grounding.KEY_NOT_SUPPORTED in result.checks


def test_grounding_flags_an_item_the_passage_cannot_answer():
    provider = scripted.ReplayProvider({"t-1:key": _reply(passage_addresses_question=False,
                                                          supported=[], evidence={}),
                                        "t-1:explanation": _explained()})
    assert grounding.NOT_ANSWERABLE_FROM_PASSAGE in grounding.check(item(), provider).checks


def test_grounding_flags_an_item_with_more_than_one_supported_option():
    provider = scripted.ReplayProvider(
        {"t-1:key": _reply(supported=["A", "B"],
                           evidence={"A": "The correct answer here is Alpha",
                                     "B": "The correct answer here is Alpha"}),
         "t-1:explanation": _explained()})
    assert grounding.MULTIPLE_OPTIONS_SUPPORTED in grounding.check(item(), provider).checks


def test_evidence_that_is_not_in_the_passage_abstains_rather_than_deciding():
    provider = scripted.ReplayProvider(
        {"t-1:key": _reply(evidence={"A": "a sentence the passage never contained"})})
    result = grounding.check(item(), provider)
    assert result.verdict == metrics.ABSTAINED
    assert grounding.EVIDENCE_NOT_IN_PASSAGE in result.checks


def test_a_backend_failure_is_an_outage_and_never_a_pass():
    provider = scripted.ReplayProvider({}, errors={"t-1:key"})
    with pytest.raises(grounding.GroundingUnavailable):
        grounding.check(item(), provider)


def test_an_unparseable_reply_is_an_outage_and_never_a_pass():
    provider = scripted.ReplayProvider({}, garbage={"t-1:key"})
    with pytest.raises(grounding.GroundingUnavailable):
        grounding.check(item(), provider)


def test_the_grounding_prompt_never_shows_the_key_or_the_explanation():
    provider = scripted.ReplayProvider({"t-1:key": _reply(), "t-1:explanation": _explained()})
    grounding.check(item(), provider)
    key_prompt = provider.prompts[0]
    assert "correct_index" not in key_prompt
    assert "Because the passage says Alpha" not in key_prompt


def test_an_explanation_that_only_asserts_is_flagged():
    provider = scripted.ReplayProvider({"t-1:key": _reply(),
                                        "t-1:explanation": _explained(gives_a_reason=False)})
    result = grounding.check(item(), provider)
    assert grounding.EXPLANATION_ASSERTS_WITHOUT_REASON in result.checks


def test_a_contradiction_without_a_verifiable_quotation_is_not_counted():
    provider = scripted.ReplayProvider(
        {"t-1:key": _reply(),
         "t-1:explanation": _explained(contradicted=[{"claim": "x",
                                                      "passage_says": "not in the passage"}])})
    result = grounding.check(item(), provider)
    assert grounding.EXPLANATION_CONTRADICTS_PASSAGE not in result.checks
    assert result.verdict == metrics.PASSED


# ------------------------------------------------------------ layer C

def _judged(**kw):
    base = {"answer": "A", "confidence": 0.9, "also_defensible": [], "answerable": True}
    base.update(kw)
    return base


def test_a_model_may_not_judge_an_item_it_wrote():
    provider = scripted.ReplayProvider({"t-1:judge": _judged()}, model="writer-1")
    with pytest.raises(judge.JudgeNotIndependent):
        judge.check(item(generated_by_model="writer-1"), provider)


def test_a_sibling_from_the_same_family_is_not_a_second_opinion():
    provider = scripted.ReplayProvider({"t-1:judge": _judged()}, model="sib-b",
                                       model_family="acme")
    with pytest.raises(judge.JudgeNotIndependent):
        judge.check(item(generated_by_model="sib-a", generated_by_family="acme"), provider)


def test_the_judge_flags_a_disagreement_with_the_key():
    provider = scripted.ReplayProvider({"t-1:judge": _judged(answer="C")})
    result = judge.check(item(), provider)
    assert judge.DISAGREES_WITH_KEY in result.checks
    assert result.verdict == metrics.FLAGGED


def test_an_unsure_judge_abstains_rather_than_flagging():
    provider = scripted.ReplayProvider({"t-1:judge": _judged(answer="C", confidence=0.3)})
    result = judge.check(item(), provider)
    assert result.verdict == metrics.ABSTAINED
    assert judge.LOW_CONFIDENCE in result.checks


def test_a_judge_answering_outside_the_options_is_an_outage():
    provider = scripted.ReplayProvider({"t-1:judge": _judged(answer="Z")})
    with pytest.raises(judge.JudgeUnavailable):
        judge.check(item(), provider)


def test_the_judge_prompt_never_shows_the_key():
    provider = scripted.ReplayProvider({"t-1:judge": _judged()})
    judge.check(item(), provider)
    assert "correct_index" not in provider.prompts[0]


# ------------------------------------------------------------ layer D

def _conformed(**kw):
    base = {"concept_tested": "C", "matches_requested_concept": True,
            "cognitive_level": "application", "answerable_from_wording_alone": False,
            "wording_cue": ""}
    base.update(kw)
    return base


def test_conformance_flags_a_correct_question_about_the_wrong_concept():
    provider = scripted.ReplayProvider(
        {"t-1:conformance": _conformed(matches_requested_concept=False,
                                       concept_tested="something else")})
    assert conformance.OFF_CONCEPT in conformance.check(item(), provider).checks


def test_conformance_flags_a_recall_question_declared_above_recall():
    provider = scripted.ReplayProvider({"t-1:conformance": _conformed(cognitive_level="recall")})
    assert conformance.BELOW_DECLARED_DIFFICULTY in conformance.check(item(), provider).checks


def test_recall_is_acceptable_where_recall_was_asked_for():
    provider = scripted.ReplayProvider({"t-1:conformance": _conformed(cognitive_level="recall")})
    result = conformance.check(item(difficulty="foundation"), provider)
    assert conformance.BELOW_DECLARED_DIFFICULTY not in result.checks


def test_a_giveaway_is_flagged_only_when_the_cue_is_in_the_question():
    good = scripted.ReplayProvider(
        {"t-1:conformance": _conformed(answerable_from_wording_alone=True,
                                       wording_cue="Which one is right?")})
    assert conformance.ANSWERABLE_FROM_WORDING in conformance.check(item(), good).checks

    bad = scripted.ReplayProvider(
        {"t-1:conformance": _conformed(answerable_from_wording_alone=True,
                                       wording_cue="wording that is not in the question")})
    result = conformance.check(item(), bad)
    assert result.verdict == metrics.ABSTAINED
    assert conformance.CUE_NOT_IN_QUESTION in result.checks


# ------------------------------------------------------------ the pipeline

def _providers(**over):
    replies = {"t-1:key": _reply(), "t-1:explanation": _explained(),
               "t-1:judge": _judged(), "t-1:conformance": _conformed()}
    replies.update(over)
    return scripted.ReplayProvider(replies)


def test_a_fatal_structural_finding_stops_the_pipeline_before_any_model_call():
    provider = _providers()
    verdict = pipeline.run(item(correct_index=9), grounding_provider=provider,
                           judge_provider=provider, conformance_provider=provider)
    assert verdict.verdict == metrics.FLAGGED
    assert verdict.layers_run == (pipeline.LAYER_STRUCTURAL,)
    assert verdict.calls == 0
    assert provider.seen == []


def test_a_configured_layer_with_no_provider_is_an_outage_not_a_skipped_step():
    with pytest.raises(grounding.GroundingUnavailable):
        pipeline.run(item(), judge_provider=_providers())


def test_a_clean_item_passes_every_layer():
    provider = _providers()
    verdict = pipeline.run(item(), grounding_provider=provider, judge_provider=provider,
                           conformance_provider=provider)
    assert verdict.verdict == metrics.PASSED
    assert len(verdict.layers_run) == 4


def test_one_flag_anywhere_flags_the_item_and_names_its_layer():
    provider = _providers(**{"t-1:judge": _judged(answer="B")})
    verdict = pipeline.run(item(), grounding_provider=provider, judge_provider=provider,
                           conformance_provider=provider)
    assert verdict.verdict == metrics.FLAGGED
    assert (pipeline.LAYER_JUDGE, judge.DISAGREES_WITH_KEY) in verdict.flags


def test_an_abstention_with_no_flag_abstains():
    provider = _providers(**{"t-1:judge": _judged(confidence=0.2)})
    verdict = pipeline.run(item(), grounding_provider=provider, judge_provider=provider,
                           conformance_provider=provider)
    assert verdict.verdict == metrics.ABSTAINED


def test_the_configuration_label_says_which_layers_ran():
    assert pipeline.Config().label().endswith("[ABCD]")
    assert pipeline.Config(judge=False, conformance=False).label().endswith("[AB]")


# ------------------------------------------------------------ the ceiling

def _ceiling(cases):
    ground, judge_provider, conform = scripted.oracle(cases)
    return [pipeline.run(c.item.as_dict(), grounding_provider=ground,
                         judge_provider=judge_provider, conformance_provider=conform)
            for c in cases]


def test_a_flawless_run_of_this_design_catches_every_planted_defect(dev):
    verdicts = _ceiling(dev.cases)
    by_id = {v.item_id: v for v in verdicts}
    missed = [c.id for c in dev.by_label(DEFECTIVE)
              if by_id[c.id].verdict != metrics.FLAGGED]
    assert missed == []


def test_a_flawless_run_of_this_design_flags_no_clean_item(dev):
    verdicts = _ceiling(dev.cases)
    by_id = {v.item_id: v for v in verdicts}
    flagged = [c.id for c in dev.by_label(CLEAN)
               if by_id[c.id].verdict == metrics.FLAGGED]
    assert flagged == []


def test_the_ceiling_is_reported_as_invalid_for_gating(dev):
    ground, _, _ = scripted.oracle(dev.cases)
    assert ground.is_oracle is True


def test_every_layer_still_earns_its_place(dev):
    """If a layer can be turned off without losing sensitivity, it is dead weight."""
    ground, judge_provider, conform = scripted.oracle(dev.cases)
    full = None
    for name in ("structural", "grounding", "judge", "conformance"):
        config = pipeline.Config(**{name: False})
        verdicts = [pipeline.run(c.item.as_dict(), grounding_provider=ground,
                                 judge_provider=judge_provider,
                                 conformance_provider=conform, config=config)
                    for c in dev.cases]
        by_id = {v.item_id: v for v in verdicts}
        caught = sum(1 for c in dev.by_label(DEFECTIVE)
                     if by_id[c.id].verdict == metrics.FLAGGED)
        assert caught < 40, f"turning off {name} changed nothing"
        full = full or caught


# ------------------------------------------------------------ the analysis

def test_matched_pairs_separates_discrimination_from_flagging_everything(dev):
    verdicts = _ceiling(dev.cases)
    pairs = analysis.matched_pairs(dev.cases, verdicts)
    assert pairs["pairs"] == 40
    assert pairs["by_outcome"] == {"discriminated": 40}

    flag_all = [pipeline.Verdict(c.id, metrics.FLAGGED) for c in dev.cases]
    pairs = analysis.matched_pairs(dev.cases, flag_all)
    assert pairs["by_outcome"] == {"both_flagged": 40}
    assert pairs["discrimination_rate"] == 0.0


def test_false_positives_are_grouped_by_the_check_that_caused_them(dev):
    verdicts = []
    for case in dev.cases:
        flags = ((pipeline.LAYER_JUDGE, judge.DISAGREES_WITH_KEY),) if case.label == CLEAN else ()
        verdicts.append(pipeline.Verdict(
            case.id, metrics.FLAGGED if flags else metrics.PASSED, flags=flags))
    report = analysis.false_positives(dev.cases, verdicts)
    assert report["count"] == 40
    assert report["worst_check"] == f"{pipeline.LAYER_JUDGE}/{judge.DISAGREES_WITH_KEY}"
    assert report["concentrated"] is True


def test_edge_cases_are_reported_separately_from_both_arms(dev):
    verdicts = _ceiling(dev.cases)
    edge = analysis.edge_behaviour(dev.cases, verdicts)
    assert edge["total"] == 20
    matrix = metrics.confusion(
        [metrics.CLEAN if c.label == CLEAN else metrics.DEFECTIVE for c in dev.arms],
        [v.verdict for v in verdicts if v.item_id in {c.id for c in dev.arms}])
    assert matrix.total == 80


# ------------------------------------------------------------ the gate

def test_a_validator_that_flags_everything_fails_despite_perfect_sensitivity():
    matrix = metrics.confusion([metrics.DEFECTIVE] * 40 + [metrics.CLEAN] * 40,
                               [metrics.FLAGGED] * 80)
    assert matrix.sensitivity == 1.0
    assert metrics.gate(matrix).outcome == metrics.FAIL


def test_too_few_items_in_an_arm_is_insufficient_evidence_not_a_pass():
    matrix = metrics.confusion([metrics.DEFECTIVE] * 10 + [metrics.CLEAN] * 10,
                               [metrics.FLAGGED] * 10 + [metrics.PASSED] * 10)
    assert metrics.gate(matrix).outcome == metrics.INSUFFICIENT


def test_the_gate_judges_the_lower_confidence_bound_not_the_point_estimate():
    labels = [metrics.DEFECTIVE] * 40 + [metrics.CLEAN] * 40
    verdicts = [metrics.FLAGGED] * 34 + [metrics.PASSED] * 6 + [metrics.PASSED] * 40
    matrix = metrics.confusion(labels, verdicts)
    assert matrix.sensitivity == 0.85
    assert matrix.sensitivity_ci[0] < metrics.MIN_SENSITIVITY
    assert metrics.gate(matrix).outcome == metrics.FAIL


# ------------------------------------------------------------ the holdout

def _stub_run(cases):
    return [pipeline.Verdict(c.id, metrics.FLAGGED if c.label == DEFECTIVE else metrics.PASSED)
            for c in cases]


def test_a_holdout_run_must_say_what_changed(tmp_path):
    with pytest.raises(holdout.HoldoutRefused, match="needs a note"):
        holdout.score(_stub_run, config_label="x", note="  ",
                      ledger_path=tmp_path / "ledger.jsonl")


def test_the_same_validator_may_not_be_scored_twice(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    holdout.score(_stub_run, config_label="v1", note="first run", ledger_path=ledger)
    with pytest.raises(holdout.HoldoutRefused, match="already scored"):
        holdout.score(_stub_run, config_label="v1", note="again", ledger_path=ledger)


def test_the_budget_is_a_refusal_not_a_warning(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    for n in range(2):
        holdout.score(_stub_run, config_label=f"v{n}", note=f"run {n}",
                      ledger_path=ledger, max_uses=2)
    with pytest.raises(holdout.HoldoutRefused, match="budget"):
        holdout.score(_stub_run, config_label="v9", note="one more",
                      ledger_path=ledger, max_uses=2)


def test_a_holdout_run_is_recorded_with_its_corpus_hash(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    gate, matrix, entry = holdout.score(_stub_run, config_label="v1", note="first",
                                        ledger_path=ledger)
    assert gate.outcome == metrics.PASS
    assert matrix.sensitivity == 1.0
    assert entry.corpus == holdout.corpus_hash()
    assert len(holdout.read_ledger(ledger)) == 1


def test_editing_the_holdout_after_a_run_is_visible_and_refused(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    holdout.score(_stub_run, config_label="v1", note="first", ledger_path=ledger)
    entries = holdout.read_ledger(ledger)
    tampered = entries[0].as_dict()
    tampered["corpus"] = "0" * 64
    ledger.write_text(json.dumps(tampered) + "\n", encoding="utf-8")
    with pytest.raises(holdout.HoldoutRefused, match="content has changed"):
        holdout.score(_stub_run, config_label="v2", note="second", ledger_path=ledger)


def test_changing_the_validator_changes_its_fingerprint(tmp_path):
    source = tmp_path / "validator"
    source.mkdir()
    (source / "a.py").write_text("x = 1\n", encoding="utf-8")
    first = holdout.validator_fingerprint("cfg", source)
    (source / "a.py").write_text("x = 2\n", encoding="utf-8")
    assert holdout.validator_fingerprint("cfg", source) != first


# ------------------------------------------------------------ the reviewers

def test_one_person_answering_twice_is_not_two_reviewers():
    sheet = review.Sheet("Dr A", {"x": review.Judgement("x", review.USABLE)})
    with pytest.raises(review.ReviewError, match="One person answering twice"):
        review.merge(sheet, review.Sheet("dr a", dict(sheet.judgements)))


def test_an_adjudicator_may_not_be_one_of_the_reviewers():
    a = review.Sheet("Dr A", {"x": review.Judgement("x", review.USABLE)})
    b = review.Sheet("Dr B", {"x": review.Judgement("x", review.UNSURE)})
    with pytest.raises(review.ReviewError, match="not adjudication"):
        review.merge(a, b, {"x": review.Judgement("x", review.USABLE)}, adjudicator="Dr A")


def test_broken_without_a_defect_class_is_refused():
    with pytest.raises(review.ReviewError, match="in what way"):
        review.Judgement("x", review.BROKEN)


def test_a_disputed_item_blocks_scoring_until_adjudicated():
    a = review.Sheet("Dr A", {"x": review.Judgement("x", review.USABLE)})
    b = review.Sheet("Dr B", {"x": review.Judgement("x", review.UNSURE)})
    result = review.merge(a, b)
    assert result["settled"]["x"]["label_status"] == DISPUTED
    assert result["usable_for_scoring"] is False

    ruled = review.merge(a, b, {"x": review.Judgement("x", review.USABLE)}, adjudicator="Dr C")
    assert ruled["settled"]["x"]["label_status"] == ADJUDICATED
    assert ruled["settled"]["x"]["reviewers"] == ["Dr A", "Dr B", "Dr C"]
    assert ruled["usable_for_scoring"] is True


def test_kappa_is_not_fooled_by_two_reviewers_who_agree_by_default():
    ids = [f"i{n}" for n in range(20)]
    a = review.Sheet("Dr A", {i: review.Judgement(i, review.USABLE) for i in ids})
    b = review.Sheet("Dr B", {i: review.Judgement(i, review.USABLE) for i in ids})
    agreement = review.kappa(a, b)
    assert agreement["raw_agreement"] == 1.0
    assert agreement["kappa"] is None
    assert "single label" in agreement["note"]


def test_the_review_sheet_does_not_show_the_reviewer_the_answer(dev):
    text = review.template(dev.by_label(DEFECTIVE)[:3], "Dr A")
    for line in text.strip().splitlines():
        row = json.loads(line)
        assert "correct_index" not in row
        assert "defect_class" in row and row["defect_class"] == ""
        assert "label" in row and row["label"] == ""


def test_the_shipped_corpus_is_honest_about_being_unreviewed(dev):
    assert all(case.label_status == "unreviewed" for case in dev.cases)


# --------------------------------------- what an arm of a given size can prove

def test_an_arm_too_small_for_its_threshold_is_insufficient_evidence_not_a_failure():
    """Thirty clean items, every one correctly passed, cannot establish 90 per cent."""
    matrix = metrics.confusion([metrics.DEFECTIVE] * 30 + [metrics.CLEAN] * 30,
                               [metrics.FLAGGED] * 30 + [metrics.PASSED] * 30)
    assert matrix.specificity == 1.0
    result = metrics.gate(matrix)
    assert result.outcome == metrics.INSUFFICIENT
    assert any("cannot establish the threshold at any performance" in r
               for r in result.reasons)


def test_the_required_arm_size_is_reported_rather_than_left_to_be_discovered():
    assert metrics.min_items_for(metrics.MIN_SPECIFICITY) == 35
    assert metrics.min_items_for(metrics.MIN_SPECIFICITY, 1) == 53
    assert metrics.min_items_for(metrics.MIN_SENSITIVITY) == 16
    assert metrics.min_items_for(metrics.MIN_SENSITIVITY, 1) == 25


def test_an_arm_that_cannot_certify_even_a_perfect_run_says_so():
    assert metrics.tolerated_errors(30, metrics.MIN_SPECIFICITY) == -1
    assert metrics.tolerated_errors(53, metrics.MIN_SPECIFICITY) == 1


def test_the_holdout_is_large_enough_to_certify_a_validator_that_is_not_perfect(hold):
    clean = len(hold.by_label(CLEAN))
    defective = len(hold.by_label(DEFECTIVE))
    assert metrics.tolerated_errors(clean, metrics.MIN_SPECIFICITY) >= 1
    assert metrics.tolerated_errors(defective, metrics.MIN_SENSITIVITY) >= 1


def test_a_perfect_run_on_the_holdout_now_passes_the_gate(tmp_path, hold):
    gate, matrix, _ = holdout.score(_stub_run, config_label="perfect", note="capacity check",
                                    ledger_path=tmp_path / "ledger.jsonl")
    assert matrix.specificity == 1.0 and matrix.sensitivity == 1.0
    assert gate.outcome == metrics.PASS


# ------------------------------- the status report, and what it refuses to say

import importlib
import pathlib

track_d = importlib.import_module("tools_track_d_status")


def _run(kind, *, oracle, sensitivity=1.0, specificity=1.0, gate=metrics.PASS):
    return runs.Run(
        at="2026-01-01T00:00:00Z", kind=kind, corpus="corpus/validator_dev",
        corpus_hash="x", validator_version="0.2.0", config="v0.2.0[ABCD]",
        providers=[runs.ProviderRecord("grounding", "p", "m", "f", oracle),
                   runs.ProviderRecord("judge", "p", "m2", "f2", oracle)],
        counts={"false_positive": 0, "false_negative": 0},
        sensitivity=sensitivity, specificity=specificity, gate=gate)


def test_a_ceiling_run_can_never_be_counted_as_a_measurement(tmp_path):
    runs.record(_run(runs.KIND_CEILING, oracle=True), runs_dir=tmp_path)
    assert runs.real_runs(runs_dir=tmp_path) == []
    assert runs.development_metrics(tmp_path)["status"] == runs.NOT_RUN


def test_a_run_with_real_providers_is_a_measurement(tmp_path):
    runs.record(_run(runs.KIND_DEVELOPMENT, oracle=False), runs_dir=tmp_path)
    result = runs.development_metrics(tmp_path)
    assert result["status"] == "RUN"
    assert result["sensitivity"] == 1.0


def test_development_metrics_reports_the_latest_run_not_the_best(tmp_path):
    poor = _run(runs.KIND_DEVELOPMENT, oracle=False, sensitivity=0.4, gate=metrics.FAIL)
    good = _run(runs.KIND_DEVELOPMENT, oracle=False, sensitivity=0.99)
    good.at = "2025-01-01T00:00:00Z"      # earlier, and better
    poor.at = "2026-06-01T00:00:00Z"      # later, and worse
    runs.record(good, runs_dir=tmp_path)
    runs.record(poor, runs_dir=tmp_path)
    assert runs.development_metrics(tmp_path)["sensitivity"] == 0.4


def test_production_readiness_is_derived_and_cannot_be_asserted():
    not_run = {"status": runs.NOT_RUN}
    verdict = track_d._production_status(not_run, not_run, not_run, not_run)
    assert verdict["status"] == track_d.NOT_ESTABLISHED
    assert len(verdict["blocking"]) == 4

    everything = track_d._production_status(
        {"status": "RUN", "gate": metrics.PASS},
        {"status": "RUN", "latest": {"outcome": metrics.PASS}},
        {"status": track_d.COMPLETE},
        {"status": "RUN"})
    assert everything["status"] == track_d.ESTABLISHED
    assert everything["blocking"] == []


def test_a_passing_development_run_alone_does_not_establish_readiness():
    verdict = track_d._production_status(
        {"status": "RUN", "gate": metrics.PASS},
        {"status": runs.NOT_RUN},
        {"status": runs.NOT_RUN},
        {"status": "RUN"})
    assert verdict["status"] == track_d.NOT_ESTABLISHED
    assert any("holdout has never been scored" in r for r in verdict["blocking"])
    assert any("qualified reviewer" in r for r in verdict["blocking"])


def test_the_status_report_separates_the_ceiling_from_the_measurement():
    report = track_d.build()
    assert report["design_ceiling"]["is_a_measurement"] is False
    assert report["design_ceiling"]["sensitivity"] == 1.0
    assert report["dev_metrics"]["status"] == runs.NOT_RUN
    assert report["validator_production_status"]["status"] == track_d.NOT_ESTABLISHED


def test_the_status_report_reads_human_review_off_the_corpus():
    report = track_d.build()
    assert report["human_review"]["status"] == runs.NOT_RUN
    assert report["human_review"]["items_reviewed"] == 0
    assert report["human_review"]["items_total"] == 193


def test_the_status_report_carries_the_corpus_counts_it_claims():
    report = track_d.build()
    assert (report["dev_n"], report["dev_clean"], report["dev_defect"],
            report["dev_edge"]) == (100, 40, 40, 20)
    assert (report["holdout_n"], report["holdout_clean"], report["holdout_defect"],
            report["holdout_edge"]) == (93, 53, 30, 10)


def test_the_committed_status_report_has_not_drifted_from_the_repository():
    """
    A generated report committed to the repository goes stale silently, which
    would reintroduce exactly the problem it was built to prevent. Regenerate
    with `python3 tools_track_d_status.py --out reports/track_d_status.json`.
    """
    path = pathlib.Path("reports/track_d_status.json")
    assert path.exists(), "the committed status report is missing"
    committed = json.loads(path.read_text(encoding="utf-8"))
    fresh = track_d.build()
    for key in ("validator_version", "dev_n", "dev_clean", "dev_defect", "dev_edge",
                "holdout_n", "holdout_clean", "holdout_defect", "holdout_edge",
                "implementation", "gate_thresholds"):
        assert committed[key] == fresh[key], f"{key} has drifted"
    assert (committed["validator_production_status"]["status"]
            == fresh["validator_production_status"]["status"])
    assert committed["design_ceiling"]["is_a_measurement"] is False


# ------------------------------------- the frozen experiment set and its report

from validator import ablation, freeze as freeze_mod  # noqa: E402

eval_tool = importlib.import_module("tools_validator_eval")


def _freeze(**over):
    base = dict(corpus="corpus/validator_dev", corpus_hash="hash",
                models=[{"role": "judge", "model": "m"}],
                experiments=[{"name": "1", "layers": "ABD"}])
    base.update(over)
    return freeze_mod.build(**base)


def test_a_manifest_refuses_to_carry_a_credential():
    for key in ("api_key", "Authorization", "SECRET"):
        with pytest.raises(freeze_mod.FreezeViolation, match="refusing to freeze"):
            _freeze(models=[{"role": "judge", key: "value"}])


def test_the_digest_moves_when_anything_that_changes_an_answer_moves():
    first = _freeze().digest()
    assert _freeze(corpus_hash="different").digest() != first
    assert _freeze(models=[{"role": "judge", "model": "other"}]).digest() != first
    assert _freeze(sampling={"temperature": 0.7}).digest() != first


def test_the_digest_ignores_the_clock_and_the_note():
    assert _freeze(note="a", created_at="2020").digest() == \
        _freeze(note="b", created_at="2026").digest()


def test_a_configuration_that_moved_mid_set_is_refused_by_name(tmp_path):
    path = tmp_path / "freeze.json"
    freeze_mod.write(_freeze(), path)
    assert freeze_mod.assert_unchanged(_freeze(), path).digest() == _freeze().digest()
    with pytest.raises(freeze_mod.FreezeViolation, match="corpus_hash"):
        freeze_mod.assert_unchanged(_freeze(corpus_hash="moved"), path)


def _arm(name, layers, tp, fn, tn, fp, outages=0, expected=80):
    matrix = metrics.ConfusionMatrix(tp, fn, tn, fp)
    return ablation.Arm(name=name, layers=layers, matrix=matrix, outages=outages,
                        items_expected=expected, items_decided=matrix.total)


def test_the_ablation_reports_what_the_judge_contributed():
    data = ablation.report([_arm("1", "ABD", 29, 11, 40, 0),
                            _arm("2", "C", 12, 28, 40, 0),
                            _arm("3", "ABCD", 34, 6, 38, 2)], model="m")
    contrib = data["judge_contribution"]
    assert contrib["status"] == ablation.COMPLETE
    assert contrib["delta_sensitivity"] == pytest.approx(0.125)
    assert contrib["delta_false_positive"] == 2
    assert "more of the planted defects" in data["experiment_conclusion"]["answer"]


def test_a_judge_that_changes_nothing_is_reported_as_cost_without_contribution():
    data = ablation.report([_arm("1", "ABD", 29, 11, 40, 0),
                            _arm("2", "C", 12, 28, 40, 0),
                            _arm("3", "ABCD", 29, 11, 40, 0)], model="m")
    assert "cost without contribution" in data["experiment_conclusion"]["answer"]


def test_an_incomplete_run_is_never_subtracted_from_a_complete_one():
    data = ablation.report([_arm("1", "ABD", 29, 11, 40, 0),
                            _arm("2", "C", 12, 28, 40, 0),
                            _arm("3", "ABCD", 20, 4, 30, 1, outages=25)], model="m")
    assert data["judge_contribution"]["status"] == ablation.NOT_COMPARABLE
    assert data["comparable"] is False
    assert "not determined" in data["experiment_conclusion"]["answer"]


def test_the_model_conclusion_is_deferred_rather_than_omitted():
    data = ablation.report([_arm("1", "ABD", 29, 11, 40, 0),
                            _arm("2", "C", 12, 28, 40, 0),
                            _arm("3", "ABCD", 34, 6, 38, 2)], model="llama-8b")
    assert data["model_conclusion"]["answer"] == "DEFERRED"
    assert "llama-8b" in data["model_conclusion"]["question"]
    assert "does not by itself disqualify" in data["model_conclusion"]["why"]


def test_the_experiment_set_runs_end_to_end_and_records_a_frozen_configuration(
        tmp_path, monkeypatch, dev, capsys):
    """Drives the real runner with oracles, which must be recorded as ceilings."""
    import benchmark.providers.registry as registry
    ground, judge_provider, conform = scripted.oracle(dev.cases)
    by_name = {"g": ground, "j": judge_provider}
    monkeypatch.setattr(registry, "build_provider", lambda spec: by_name[spec])

    code = eval_tool.main([
        "--runs-dir", str(tmp_path / "runs"), "--freeze-dir", str(tmp_path),
        "--out", str(tmp_path / "ablation.json"),
        "experiments", "--provider", "g", "--judge", "j", "--note", "smoke"])
    assert code == 0

    recorded = runs.load_all(tmp_path / "runs")
    assert len(recorded) == 3
    assert {r.config for r in recorded} == {"v0.2.0[ABD]", "v0.2.0[C]", "v0.2.0[ABCD]"}
    assert all(r.kind == runs.KIND_CEILING for r in recorded), \
        "an oracle run must never be filed as a development measurement"
    assert len({r.freeze for r in recorded}) == 1, \
        "the three arms must share one frozen configuration"
    assert runs.real_runs(runs_dir=tmp_path / "runs") == []

    payload = json.loads((tmp_path / "ablation.json").read_text())
    assert payload["freeze"]["prompt_versions"]["judge"]
    assert payload["ablation"]["model_conclusion"]["answer"] == "DEFERRED"


def test_the_runner_refuses_a_judge_that_is_the_same_model(tmp_path, monkeypatch, dev):
    import benchmark.providers.registry as registry
    ground, _, _ = scripted.oracle(dev.cases)
    monkeypatch.setattr(registry, "build_provider", lambda spec: ground)
    code = eval_tool.main(["--runs-dir", str(tmp_path / "runs"), "--freeze-dir",
                           str(tmp_path), "experiments", "--provider", "g",
                           "--judge", "g"])
    assert code == 2
