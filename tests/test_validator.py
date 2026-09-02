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
review_cli = importlib.import_module("tools_validator_review")


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
    """
    The invariant is the SEPARATION, not the absence of a run.

    This used to assert `dev_metrics["status"] == NOT_RUN`, which was a fact
    about the repository's data rather than about the code, and it stopped
    being true the moment Phase 0 wrote a real development record on
    2026-09-02. Pinning "no run exists" would mean this test could only pass
    while the project had done nothing.

    What must hold either way: a ceiling is never a measurement, and a
    100% ceiling never promotes the validator to ESTABLISHED on its own.
    """
    report = track_d.build()
    assert report["design_ceiling"]["is_a_measurement"] is False
    assert report["design_ceiling"]["sensitivity"] == 1.0
    assert report["dev_metrics"]["status"] in (runs.NOT_RUN, "RUN")
    # The ceiling is perfect and production readiness is still not established.
    assert report["validator_production_status"]["status"] == track_d.NOT_ESTABLISHED


def test_a_ceiling_run_never_counts_as_the_measurement():
    """
    The actual invariant the test above was reaching for, asserted directly:
    oracle/ceiling runs are excluded from dev metrics however many exist, so
    "the design could score 100%" can never be read off as "it did".
    """
    report = track_d.build()
    ceiling, measured = report["design_ceiling"], report["dev_metrics"]
    assert ceiling["is_a_measurement"] is False
    assert ceiling["sensitivity"] == 1.0 and ceiling["specificity"] == 1.0

    if measured["status"] != runs.NOT_RUN:
        # Three ceiling runs sit in reports/validator_runs/ alongside the real
        # one. If any had leaked into the measurement, specificity would read
        # the ceiling's 100%. Phase 0 measured 0%, so the separation held on
        # real data rather than only in principle.
        assert measured["specificity"] != ceiling["specificity"]


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
                            _arm("3", "ABCD", 34, 6, 38, 2)], judge_model="m")
    contrib = data["judge_contribution"]
    assert contrib["status"] == ablation.COMPLETE
    assert contrib["delta_sensitivity"] == pytest.approx(0.125)
    assert contrib["delta_false_positive"] == 2
    assert "more of the planted defects" in data["experiment_conclusion"]["answer"]


def test_a_judge_that_changes_nothing_is_reported_as_cost_without_contribution():
    data = ablation.report([_arm("1", "ABD", 29, 11, 40, 0),
                            _arm("2", "C", 12, 28, 40, 0),
                            _arm("3", "ABCD", 29, 11, 40, 0)], judge_model="m")
    assert "cost without contribution" in data["experiment_conclusion"]["answer"]


def test_an_incomplete_run_is_never_subtracted_from_a_complete_one():
    data = ablation.report([_arm("1", "ABD", 29, 11, 40, 0),
                            _arm("2", "C", 12, 28, 40, 0),
                            _arm("3", "ABCD", 20, 4, 30, 1, outages=25)], judge_model="m")
    assert data["judge_contribution"]["status"] == ablation.NOT_COMPARABLE
    assert data["comparable"] is False
    assert "not determined" in data["experiment_conclusion"]["answer"]


def test_the_model_conclusion_is_deferred_rather_than_omitted():
    data = ablation.report([_arm("1", "ABD", 29, 11, 40, 0),
                            _arm("2", "C", 12, 28, 40, 0),
                            _arm("3", "ABCD", 34, 6, 38, 2)], judge_model="llama-70b",
                           candidate_model="llama-8b")
    assert data["model_conclusion"]["answer"] == "DEFERRED"
    assert "llama-70b" in data["model_conclusion"]["question"]
    assert "llama-8b" in data["model_conclusion"]["question"]
    assert "does not by itself disqualify" in data["model_conclusion"]["why"]


def test_the_experiment_set_runs_end_to_end_and_records_a_frozen_configuration(
        tmp_path, monkeypatch, dev, capsys):
    """Drives the real runner with oracles, which must be recorded as ceilings."""
    import benchmark.providers.registry as registry
    ground, judge_provider, conform = scripted.oracle(dev.cases)
    by_name = {"g": ground, "j": judge_provider}
    monkeypatch.setattr(registry, "build_provider",
                        lambda spec: by_name[spec["provider"]])

    code = eval_tool.main([
        "--runs-dir", str(tmp_path / "runs"), "--freeze-dir", str(tmp_path),
        "--out", str(tmp_path / "ablation.json"),
        "experiments", "--candidate", "g", "--judge", "j", "--note", "smoke"])
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
    monkeypatch.setattr(registry, "build_provider", lambda spec: ground)  # same model both seats
    code = eval_tool.main(["--runs-dir", str(tmp_path / "runs"), "--freeze-dir",
                           str(tmp_path), "experiments", "--candidate", "g",
                           "--judge", "g"])
    assert code == 2


def test_a_seat_names_a_provider_and_a_model():
    assert eval_tool.parse_seat("nvidia:meta/llama-3.1-8b-instruct") == {
        "provider": "nvidia", "model_id": "meta/llama-3.1-8b-instruct"}
    assert eval_tool.parse_seat("scripted") == {"provider": "scripted"}
    assert eval_tool.parse_seat("nvidia:a:b")["model_id"] == "a:b", \
        "a model id containing a colon must survive intact"
    assert eval_tool.parse_seat("nvidia:m", endpoint="https://h/v1")["base_url"] \
        == "https://h/v1"
    with pytest.raises(ValueError):
        eval_tool.parse_seat("  ")


def test_a_seat_spec_never_carries_a_credential():
    """The key is read from the environment by name, so it has nowhere to leak."""
    spec = eval_tool.parse_seat("nvidia:meta/llama-3.1-70b-instruct",
                                endpoint="https://integrate.api.nvidia.com/v1")
    assert set(spec) <= {"provider", "model_id", "base_url"}


def test_provider_is_rejected_by_name_rather_than_silently_aliased(tmp_path, capsys):
    code = eval_tool.main(["--runs-dir", str(tmp_path), "experiments",
                           "--candidate", "a", "--judge", "b", "--provider", "a"])
    assert code == 2
    assert "--provider has been removed" in capsys.readouterr().err


def test_a_run_record_distinguishes_the_seat_from_the_layer():
    records = [runs.describe_provider("grounding", object(), seat=runs.SEAT_CANDIDATE),
               runs.describe_provider("judge", object(), seat=runs.SEAT_JUDGE),
               runs.describe_provider("conformance", object(), seat=runs.SEAT_CANDIDATE)]
    assert [r.as_dict()["seat"] for r in records] == ["candidate", "judge", "candidate"]
    assert [r.as_dict()["role"] for r in records] == ["grounding", "judge", "conformance"]
    with pytest.raises(ValueError):
        runs.describe_provider("grounding", object(), seat="whatever")


# --------------------------------------- the forecast and the hard call budget

from validator import budget as budget_mod, forecast as forecast_mod  # noqa: E402
from benchmark.providers.base import (BaseProvider,  # noqa: E402
                                      GenerationRequest, RetryPolicy)


def test_the_forecast_is_exact_because_layer_a_is_free(dev):
    plan = forecast_mod.plan(dev, eval_tool.EXPERIMENTS, max_retries=2)
    assert plan["items"] == 100
    # Five items carry a fatal structural finding, so they never reach a model.
    assert plan["stopped_by_layer_a"] == 5
    assert plan["reach_model_layers"] == 95
    assert plan["planned"]["total"] == 765
    assert plan["planned"]["judge"] == 195
    assert plan["planned"]["candidate"] == 570


def test_experiment_one_spends_nothing_in_the_judge_seat(dev):
    plan = forecast_mod.plan(dev, eval_tool.EXPERIMENTS, max_retries=2)
    first = plan["experiments"][0]
    assert first["layers"] == "ABD"
    assert first["judge"] == 0, "a judge failure must not be able to cost experiment 1"


def test_the_judge_alone_is_not_gated_by_layer_a(dev):
    """Experiment 2 has no structural layer, so every item reaches the judge."""
    plan = forecast_mod.plan(dev, eval_tool.EXPERIMENTS, max_retries=2)
    assert plan["experiments"][1]["judge"] == 100


def test_planned_and_spendable_differ_by_the_frozen_retry_policy(dev):
    plan = forecast_mod.plan(dev, eval_tool.EXPERIMENTS, max_retries=2)
    assert plan["retry"]["attempts_per_logical_call"] == 3
    assert plan["max_spendable"]["total"] == 765 * 3
    assert plan["max_spendable"]["judge"] == 195 * 3


def test_a_budget_that_covers_the_plan_but_not_the_worst_case_warns(dev):
    plan = forecast_mod.plan(dev, eval_tool.EXPERIMENTS, max_retries=2,
                             max_calls=800, max_judge_calls=200)
    assert plan["verdict"] == forecast_mod.WILL_EXCEED
    assert plan["impossible"] == []
    assert "may stop early" in forecast_mod.render(plan)


def test_a_budget_below_the_measurement_is_impossible_not_merely_tight(dev):
    plan = forecast_mod.plan(dev, eval_tool.EXPERIMENTS, max_retries=0, max_calls=100)
    assert plan["verdict"] == forecast_mod.IMPOSSIBLE
    assert any("cannot complete even with no retries" in r for r in plan["impossible"])


def test_no_budget_is_reported_as_a_decision_not_a_default(dev):
    plan = forecast_mod.plan(dev, eval_tool.EXPERIMENTS)
    assert plan["verdict"] == forecast_mod.NO_BUDGET
    assert "Nothing will stop this run early" in forecast_mod.render(plan)


class _Retrying(BaseProvider):
    """
    A provider whose outbound attempt always fails, so the retry loop runs.

    Subclasses BaseProvider rather than ReplayProvider on purpose: ReplayProvider
    overrides generate() and therefore never reaches _call, which is the boundary
    under test here.
    """
    name = "retrying-test-double"
    model = "retrying"
    model_family = "none"
    is_model = True
    # Stated rather than inherited: this test is about a logical call with two
    # retries, so it sets two retries instead of trusting whatever the process
    # default happens to be by the time it runs.
    retry_policy = RetryPolicy(max_retries=2, timeout_seconds=1.0)

    def _call(self, request, timeout_seconds):
        raise RuntimeError("transport failure")


def test_the_budget_counts_retries_not_logical_calls():
    """
    The whole point of metering at the outbound boundary. One logical call with
    max_retries=2 sends three requests, and all three are counted.
    """
    provider = _Retrying()
    spend = budget_mod.Budget(max_calls=10)
    _, boundary = budget_mod.meter(provider, spend, budget_mod.SEAT_CANDIDATE)
    assert boundary == budget_mod.BOUNDARY_OUTBOUND
    response = provider.generate(GenerationRequest(item_id="i", prompt="p"))
    assert response.attempts == 3
    assert spend.total == 3, "a budget around generate() would have counted 1"


def test_exhaustion_does_not_itself_consume_budget():
    spend = budget_mod.Budget(max_calls=2)
    for _ in range(2):
        spend.spend(budget_mod.SEAT_CANDIDATE)
    for _ in range(5):
        with pytest.raises(budget_mod.BudgetExhausted):
            spend.spend(budget_mod.SEAT_CANDIDATE)
    assert spend.total == 2


def test_the_judge_ceiling_is_separate_from_the_total():
    spend = budget_mod.Budget(max_calls=100, max_judge_calls=1)
    spend.spend(budget_mod.SEAT_JUDGE)
    with pytest.raises(budget_mod.BudgetExhausted, match="judge call budget"):
        spend.spend(budget_mod.SEAT_JUDGE)
    spend.spend(budget_mod.SEAT_CANDIDATE)          # the candidate is unaffected
    assert spend.spent[budget_mod.SEAT_CANDIDATE] == 1


def test_metering_a_provider_twice_does_not_double_count():
    """The candidate occupies two layers with one object."""
    provider = scripted.ReplayProvider(default={"ok": True})
    spend = budget_mod.Budget()
    budget_mod.meter(provider, spend, budget_mod.SEAT_CANDIDATE)
    budget_mod.meter(provider, spend, budget_mod.SEAT_CANDIDATE)
    provider.generate(GenerationRequest(item_id="i", prompt="p"))
    assert spend.total == 1


def test_budget_exhaustion_lands_in_the_existing_incomplete_path():
    """
    No new outcome. The layer raises Unavailable, which the runner counts as an
    outage, which makes the arm INCOMPLETE, which produces no delta.
    """
    provider = scripted.ReplayProvider(default={"passage_addresses_question": True,
                                                "supported": ["A"], "evidence": {}})
    spend = budget_mod.Budget(max_calls=0)
    budget_mod.meter(provider, spend, budget_mod.SEAT_CANDIDATE)
    with pytest.raises(grounding.GroundingUnavailable, match="backend failed"):
        grounding.check(item(), provider)


def test_exhaustion_reaches_the_caller_the_same_way_at_both_boundaries():
    """
    A caller must not need two code paths for one event. At the outbound
    boundary the provider's retry loop turns the exception into a failed
    response; at the logical boundary the meter does it.
    """
    for provider in (_Retrying(), scripted.ReplayProvider(default={"ok": True})):
        spend = budget_mod.Budget(max_calls=0)
        budget_mod.meter(provider, spend, budget_mod.SEAT_CANDIDATE)
        response = provider.generate(GenerationRequest(item_id="i", prompt="p"))
        assert response.ok is False
        assert "BudgetExhausted" in (response.error or "")


def test_a_run_using_real_models_refuses_without_budgets_and_confirmation(
        tmp_path, monkeypatch, dev, capsys):
    import benchmark.providers.registry as registry

    class _Real(scripted.ReplayProvider):
        is_model = True
        is_oracle = False

    ground, judge_provider, _ = scripted.oracle(dev.cases)
    real_ground = _Real(dict(ground.replies), model="candidate-x")
    real_judge = _Real(dict(judge_provider.replies), model="judge-y")
    by_name = {"g": real_ground, "j": real_judge}
    monkeypatch.setattr(registry, "build_provider", lambda spec: by_name[spec["provider"]])

    base = ["--runs-dir", str(tmp_path / "runs"), "--freeze-dir", str(tmp_path),
            "experiments", "--candidate", "g", "--judge", "j", "--note", "n"]
    assert eval_tool.main(base) == 2
    assert "needs both --max-calls" in capsys.readouterr().err

    assert eval_tool.main(base + ["--max-calls", "3000", "--max-judge-calls", "600"]) == 2
    assert "pass --confirm-spend" in capsys.readouterr().err


def test_a_budget_below_the_measurement_refuses_before_any_request(
        tmp_path, monkeypatch, dev, capsys):
    import benchmark.providers.registry as registry
    ground, judge_provider, _ = scripted.oracle(dev.cases)
    by_name = {"g": ground, "j": judge_provider}
    monkeypatch.setattr(registry, "build_provider", lambda spec: by_name[spec["provider"]])
    code = eval_tool.main(["--runs-dir", str(tmp_path / "runs"), "--freeze-dir",
                           str(tmp_path), "experiments", "--candidate", "g",
                           "--judge", "j", "--note", "n", "--max-calls", "10"])
    assert code == 2
    assert "below what the measurement needs" in capsys.readouterr().err
    assert not (tmp_path / "runs").exists(), "nothing may be recorded before a refusal"


def test_the_manifest_records_the_credential_name_and_never_a_value():
    entry = freeze_mod.describe_model("judge", object(), seat="judge",
                                      endpoint="https://integrate.api.nvidia.com/v1",
                                      credential_ref="NVIDIA_API_KEY")
    assert entry["credential_ref"] == "NVIDIA_API_KEY"
    assert "nvapi-" not in json.dumps(entry)


@pytest.mark.parametrize("value", [
    "nvapi-ABCDEFGHIJKLMNOPQRSTUVWX",
    "sk-ABCDEFGHIJKLMNOPQRSTUVWX",
    "Bearer ABCDEFGHIJKLMNOPQRSTUVWX",
    "AKIAIOSFODNN7EXAMPLE",
])
def test_a_credential_shaped_value_is_refused_whatever_field_it_arrives_in(value):
    """
    Defence in depth: the forbidden-key list is closed today and the schema will
    grow. This catches a credential in a field nobody thought to forbid.
    """
    with pytest.raises(freeze_mod.FreezeViolation, match="shaped like a credential"):
        _freeze(models=[{"role": "judge", "some_new_field": value}])


# ------------------------------------- one accounting unit, not one per provider

def test_a_real_model_that_cannot_be_metered_at_the_outbound_boundary_is_refused():
    """
    Counting a real model at generate() would record logical calls under the
    same name as outbound attempts, and the two differ by the retry policy.
    """
    class _RealWithoutCall(scripted.ReplayProvider):
        is_model = True
        is_oracle = False

    with pytest.raises(budget_mod.UnmeterableProvider, match="does not implement _call"):
        budget_mod.meter(_RealWithoutCall(), budget_mod.Budget(),
                         budget_mod.SEAT_CANDIDATE)


def test_the_canonical_unit_is_the_outbound_attempt():
    assert budget_mod.CANONICAL_UNIT == budget_mod.BOUNDARY_OUTBOUND
    provider = _Retrying()
    _, boundary = budget_mod.meter(provider, budget_mod.Budget(),
                                   budget_mod.SEAT_CANDIDATE)
    assert boundary == budget_mod.CANONICAL_UNIT


def test_a_run_counted_in_the_wrong_unit_is_not_a_real_measurement(tmp_path):
    """
    Belt and braces on top of the refusal above: even if such a record existed,
    it is not comparable with one counted in outbound attempts, so it is
    excluded rather than silently placed in the same column.
    """
    run = _run(runs.KIND_DEVELOPMENT, oracle=False)
    run.measurement_unit = budget_mod.BOUNDARY_LOGICAL
    runs.record(run, runs_dir=tmp_path)
    assert runs.real_runs(runs_dir=tmp_path) == []
    assert runs.development_metrics(tmp_path)["status"] == runs.NOT_RUN

    run.measurement_unit = budget_mod.CANONICAL_UNIT
    runs.record(run, runs_dir=tmp_path)
    assert len(runs.real_runs(runs_dir=tmp_path)) == 1


def test_test_doubles_keep_their_own_accounting_without_claiming_to_be_real():
    """A fixture reply costs nothing, so counting it logically is fine -- and
    the run it produces is excluded from measurements on other grounds."""
    provider = scripted.ReplayProvider(default={"ok": True})
    _, boundary = budget_mod.meter(provider, budget_mod.Budget(),
                                   budget_mod.SEAT_CANDIDATE)
    assert boundary == budget_mod.BOUNDARY_LOGICAL
    record = runs.describe_provider("grounding", provider)
    assert record.is_model is False


def test_a_providers_retry_policy_cannot_leak_into_every_other_provider():
    """
    `BaseProvider.retry_policy` is a class attribute, so it is ONE object shared
    by every provider that has not been given its own. While RetryPolicy was
    mutable, `provider.retry_policy.max_retries = 0` retuned every provider in
    the process and the manifest went on recording the mutated value for all of
    them -- a run that looked consistent in the record and was not.

    It is caught here rather than only in the provider tests because the freeze
    pins max_retries and the spend forecast multiplies by 1 + max_retries: a
    silent mutation makes the frozen number and the actual behaviour disagree.
    """
    from dataclasses import replace as dataclass_replace
    from benchmark.providers.scripted import ScriptedProvider

    one, other = ScriptedProvider(), ScriptedProvider()
    with pytest.raises(Exception) as caught:
        one.retry_policy.max_retries = 0
    assert "Frozen" in type(caught.value).__name__

    one.retry_policy = dataclass_replace(one.retry_policy, max_retries=0)
    assert one.retry_policy.max_retries == 0
    assert other.retry_policy.max_retries == 2, "the change escaped to another provider"
    assert BaseProvider.retry_policy.max_retries == 2, "the change escaped to the default"


# ------------------------------------------------- --only: scoped experiments

from validator import wallclock  # noqa: E402


def test_only_accepts_the_real_layer_codes_case_insensitively():
    selected = eval_tool._select_experiments("c")
    assert [layers for _t, layers, _f in selected] == ["C"]
    selected = eval_tool._select_experiments("ABCD,abd")
    assert [layers for _t, layers, _f in selected] == ["ABD", "ABCD"], \
        "EXPERIMENTS order is preserved regardless of the order given on the CLI"


def test_only_with_nothing_selects_everything():
    assert eval_tool._select_experiments(None) == list(eval_tool.EXPERIMENTS)
    assert eval_tool._select_experiments("") == list(eval_tool.EXPERIMENTS)


def test_only_refuses_an_unknown_name_without_running_anything():
    with pytest.raises(ValueError, match="ZZZ"):
        eval_tool._select_experiments("C,ZZZ")


def test_only_c_alone_is_forecast_at_its_own_cost_not_the_full_sets(dev):
    """
    This is the whole point of the flag: a budget sized for experiment C alone
    (100 judge calls) must not be evaluated against the full set's 765/195, or
    it is refused as BUDGET TOO SMALL for a measurement it was never meant to
    fund.
    """
    selected = eval_tool._select_experiments("C")
    # 100 planned judge calls fits a budget of 100 exactly at zero retries;
    # WITHIN needs the WORST case to fit too, so size the budget for that.
    scoped = forecast_mod.plan(dev, selected, max_retries=2, max_calls=300,
                               max_judge_calls=300)
    assert scoped["planned"]["total"] == 100
    assert scoped["planned"]["candidate"] == 0
    assert scoped["verdict"] == forecast_mod.WITHIN

    tight = forecast_mod.plan(dev, selected, max_retries=2, max_calls=100,
                              max_judge_calls=100)
    assert tight["verdict"] == forecast_mod.WILL_EXCEED, \
        "planned fits a budget of 100; the worst case (300) does not"

    full = forecast_mod.plan(dev, eval_tool.EXPERIMENTS, max_retries=2,
                             max_calls=100, max_judge_calls=100)
    assert full["verdict"] == forecast_mod.IMPOSSIBLE


def test_only_c_alones_row_matches_its_row_in_a_full_forecast(dev):
    """Line-for-line comparability: the row for C does not depend on its siblings."""
    scoped = forecast_mod.plan(dev, eval_tool._select_experiments("C"), max_retries=2)
    full = forecast_mod.plan(dev, eval_tool.EXPERIMENTS, max_retries=2)
    scoped_row = scoped["experiments"][0]
    full_row = next(r for r in full["experiments"] if r["layers"] == "C")
    assert scoped_row["by_layer"] == full_row["by_layer"]
    assert scoped_row["candidate"] == full_row["candidate"]
    assert scoped_row["judge"] == full_row["judge"]


def test_a_freeze_built_with_only_c_matches_one_built_for_the_full_set(tmp_path):
    """
    The frozen configuration always describes all three experiments, so an
    invocation using --only does not get treated as a different configuration
    from a full run -- which is what makes a natural follow-up invocation for
    the remaining experiments trivial rather than a fresh freeze.
    """
    common = dict(corpus="corpus/validator_dev", corpus_hash="h",
                 models=[{"role": "judge", "model": "m"}],
                 sampling={"temperature": 0.0})
    full_defs = [{"name": t, "layers": layers, "config": f}
                for t, layers, f in eval_tool.EXPERIMENTS]
    a = freeze_mod.build(experiments=full_defs, **common)
    b = freeze_mod.build(experiments=full_defs, **common)  # as a --only C run would build it
    assert a.digest() == b.digest()


def test_the_experiments_end_to_end_run_honours_only(tmp_path, monkeypatch, dev):
    import benchmark.providers.registry as registry
    ground, judge_provider, conform = scripted.oracle(dev.cases)
    by_name = {"g": ground, "j": judge_provider}
    monkeypatch.setattr(registry, "build_provider",
                        lambda spec: by_name[spec["provider"]])

    code = eval_tool.main([
        "--runs-dir", str(tmp_path / "runs"), "--freeze-dir", str(tmp_path),
        "experiments", "--candidate", "g", "--judge", "j", "--only", "C",
        "--note", "smoke"])
    assert code == 0

    recorded = runs.load_all(tmp_path / "runs")
    assert len(recorded) == 1
    assert recorded[0].config == "v0.2.0[C]"

    # A second invocation for the remaining experiments must not be refused
    # as a configuration change.
    code = eval_tool.main([
        "--runs-dir", str(tmp_path / "runs"), "--freeze-dir", str(tmp_path),
        "experiments", "--candidate", "g", "--judge", "j", "--only", "ABD,ABCD",
        "--note", "smoke continued"])
    assert code == 0
    assert len(runs.load_all(tmp_path / "runs")) == 3


# --------------------------------------------------- wall-clock ceiling

def test_unset_wall_clock_never_raises():
    clock = wallclock.WallClock(max_minutes=None)
    for _ in range(1000):
        clock.check()  # must never raise


def test_a_set_wall_clock_raises_once_elapsed_exceeds_it():
    clock = wallclock.WallClock(max_minutes=0.0)  # already "elapsed" at t=0
    with pytest.raises(wallclock.WallClockExceeded, match="wall-clock ceiling"):
        clock.check()


def test_meter_without_a_clock_behaves_exactly_as_before():
    """No `clock` argument: byte-for-byte the pre-existing behaviour."""
    provider = scripted.ReplayProvider(default={"ok": True})
    spend = budget_mod.Budget(max_calls=5)
    budget_mod.meter(provider, spend, budget_mod.SEAT_CANDIDATE)
    response = provider.generate(GenerationRequest(item_id="i", prompt="p"))
    assert response.ok
    assert spend.total == 1


def test_a_logical_provider_converts_wall_clock_exceeded_to_a_failed_response():
    provider = scripted.ReplayProvider(default={"ok": True})
    spend = budget_mod.Budget(max_calls=100)
    clock = wallclock.WallClock(max_minutes=0.0)
    budget_mod.meter(provider, spend, budget_mod.SEAT_CANDIDATE, clock=clock)
    response = provider.generate(GenerationRequest(item_id="i", prompt="p"))
    assert response.ok is False
    assert "WallClockExceeded" in response.error
    assert spend.total == 0, "the clock is checked before spend, so nothing was spent"


def test_an_outbound_provider_lets_wall_clock_exceeded_reach_its_own_retry_loop():
    """
    Same treatment as BudgetExhausted at this boundary: uncaught from the
    counted wrapper, handled by BaseProvider.generate's existing retry loop,
    which already converts any exception from _call into a failed response.
    """
    provider = scripted.ReplayProvider(default={"ok": True})  # placeholder, replaced below

    class _Real(BaseProvider):
        name, model, model_family, is_model = "real-test", "real", "none", True
        retry_policy = RetryPolicy(max_retries=1, timeout_seconds=1.0)

        def _call(self, request, timeout_seconds):
            return "raw", {"ok": True}, None, None

    provider = _Real()
    spend = budget_mod.Budget(max_calls=100)
    clock = wallclock.WallClock(max_minutes=0.0)
    budget_mod.meter(provider, spend, budget_mod.SEAT_CANDIDATE, clock=clock)
    response = provider.generate(GenerationRequest(item_id="i", prompt="p"))
    assert response.ok is False
    assert "WallClockExceeded" in response.error
    assert response.attempts == 2, "the retry loop ran, exactly as it does for BudgetExhausted"


def test_forecast_reports_the_wall_clock_ceiling_separately_from_the_money_verdict(dev):
    plan = forecast_mod.plan(dev, eval_tool.EXPERIMENTS, max_calls=2400,
                             max_judge_calls=600, max_wall_minutes=30)
    assert plan["verdict"] == forecast_mod.WITHIN          # money verdict untouched
    assert plan["wall_clock"] == {"max_minutes": 30, "verdict": forecast_mod.WALL_CLOCK_SET}

    unset = forecast_mod.plan(dev, eval_tool.EXPERIMENTS)
    assert unset["wall_clock"]["verdict"] == forecast_mod.NO_WALL_CLOCK
    assert "NO WALL-CLOCK CEILING SET" in forecast_mod.render(unset)


def test_the_experiments_run_stops_starting_new_experiments_past_the_ceiling(
        tmp_path, monkeypatch, dev):
    """
    The arm in progress when the clock trips still gets recorded (it may be
    INCOMPLETE from per-call outages); the NEXT experiment in this invocation
    does not start at all.
    """
    import benchmark.providers.registry as registry
    ground, judge_provider, conform = scripted.oracle(dev.cases)
    by_name = {"g": ground, "j": judge_provider}
    monkeypatch.setattr(registry, "build_provider",
                        lambda spec: by_name[spec["provider"]])

    code = eval_tool.main([
        "--runs-dir", str(tmp_path / "runs"), "--freeze-dir", str(tmp_path),
        "experiments", "--candidate", "g", "--judge", "j",
        "--only", "ABD,C", "--max-wall-minutes", "0", "--note", "smoke"])
    assert code == 0
    recorded = runs.load_all(tmp_path / "runs")
    # The first selected experiment (ABD) is recorded; the clock is already
    # past its ceiling before the second (C) would start.
    assert len(recorded) == 1
    assert recorded[0].config == "v0.2.0[ABD]"


# ------------------------------------------------- Phase 3: the kappa gate

def test_the_default_gate_is_off_so_every_existing_caller_is_unaffected():
    """merge() without min_kappa behaves exactly as it did before this gate existed."""
    a = review.Sheet("Dr A", {"x": review.Judgement("x", review.USABLE)})
    b = review.Sheet("Dr B", {"x": review.Judgement("x", review.USABLE)})
    result = review.merge(a, b)
    assert result["kappa_gate"] is None
    assert result["usable_for_scoring"] is True


@pytest.mark.parametrize("value,expected_band,expected_pass", [
    (0.0, review.KAPPA_BLOCKED, False),
    (0.669, review.KAPPA_BLOCKED, False),
    (0.67, review.KAPPA_ACCEPTABLE, True),          # kappa = 0.67 -> accepted
    (0.75, review.KAPPA_ACCEPTABLE, True),          # 0.67 < kappa < 0.80 -> accepted, not strong
    (0.7999, review.KAPPA_ACCEPTABLE, True),
    (0.80, review.KAPPA_STRONG, True),              # kappa >= 0.80 -> accepted and strong
    (0.95, review.KAPPA_STRONG, True),
    (1.0, review.KAPPA_STRONG, True),
])
def test_kappa_band_matches_the_phase_3_specification_exactly(value, expected_band,
                                                               expected_pass):
    band = review.kappa_band(value)
    assert band == expected_band
    assert (band != review.KAPPA_BLOCKED) == expected_pass


def test_kappa_below_067_is_blocked():
    band = review.kappa_band(0.5)
    assert band == review.KAPPA_BLOCKED


def test_kappa_undefined_never_passes_the_gate():
    """Both reviewers using one label for everything is not measured agreement."""
    assert review.kappa_band(None) is None


def test_the_min_kappa_default_constant_is_the_phase_3_floor():
    assert review.MIN_KAPPA_PHASE_3 == 0.67
    assert review.STRONG_KAPPA == 0.80


def _balanced_sheets(n_pairs: int, n_swaps_each_direction: int):
    """
    Sheets with BALANCED marginals -- n_pairs items rated USABLE by Dr A,
    n_pairs rated BROKEN -- so kappa varies with agreement instead of
    collapsing to raw agreement, which is what happens whenever one rater uses
    only a single label (as an earlier, wrong version of this helper did: it
    made every disagreement a Dr-A-only-ever-says-USABLE split, which forces
    kappa to exactly 0.0 regardless of how much they actually disagreed).

    Returns (sheet_a, sheet_b, disagreeing_item_ids), so a caller can adjudicate
    every disagreement and isolate the kappa gate from the dispute gate --
    merge() already treats an unresolved same-item mismatch as `disputed`,
    which would otherwise block `usable_for_scoring` for a reason that has
    nothing to do with kappa.
    """
    U, B = review.USABLE, review.BROKEN
    a_labels = [U] * n_pairs + [B] * n_pairs
    b_labels = ([U] * (n_pairs - n_swaps_each_direction) + [B] * n_swaps_each_direction
               + [B] * (n_pairs - n_swaps_each_direction) + [U] * n_swaps_each_direction)
    a_j, b_j, disagree = {}, {}, []
    for k, (al, bl) in enumerate(zip(a_labels, b_labels)):
        item = f"i{k}"
        a_j[item] = review.Judgement(item, al, defect_class=("wrong_key" if al == B else ""))
        b_j[item] = review.Judgement(item, bl, defect_class=("wrong_key" if bl == B else ""))
        if al != bl:
            disagree.append(item)
    return review.Sheet("Dr A", a_j), review.Sheet("Dr B", b_j), disagree


def _adjudicate_all(disagreeing_ids):
    return {item: review.Judgement(item, review.USABLE, note="ruled by Dr C")
           for item in disagreeing_ids}


def test_merge_blocks_usable_for_scoring_when_kappa_is_below_the_floor():
    # Balanced marginals, half the items swapped between raters: raw agreement
    # exactly matches chance expectation, so kappa is 0.0 -- clearly below the
    # 0.67 floor without being a degenerate all-one-label construction.
    a, b, disagree = _balanced_sheets(n_pairs=10, n_swaps_each_direction=5)
    result = review.merge(a, b, _adjudicate_all(disagree), adjudicator="Dr C",
                          min_kappa=review.MIN_KAPPA_PHASE_3)
    assert result["disputed"] == [], "every disagreement was adjudicated"
    assert result["agreement"]["kappa"] == pytest.approx(0.0)
    assert result["kappa_gate"]["passed"] is False
    assert result["kappa_gate"]["band"] == review.KAPPA_BLOCKED
    # The dispute machinery is fully satisfied; only the kappa gate blocks this.
    assert result["usable_for_scoring"] is False


def test_merge_permits_usable_for_scoring_when_kappa_clears_the_floor():
    # One swap in each direction out of ten pairs: kappa 0.8, comfortably above
    # the 0.67 floor.
    a, b, disagree = _balanced_sheets(n_pairs=10, n_swaps_each_direction=1)
    result = review.merge(a, b, _adjudicate_all(disagree), adjudicator="Dr C",
                          min_kappa=review.MIN_KAPPA_PHASE_3)
    assert result["disputed"] == []
    assert result["agreement"]["kappa"] >= review.MIN_KAPPA_PHASE_3
    assert result["kappa_gate"]["passed"] is True
    assert result["usable_for_scoring"] is True


def test_the_kappa_gate_does_not_override_the_dispute_gate():
    """High agreement does not waive an unresolved disagreement on a shared item."""
    a = review.Sheet("Dr A", {f"i{n}": review.Judgement(f"i{n}", review.USABLE)
                              for n in range(20)})
    b_j = {f"i{n}": review.Judgement(f"i{n}", review.USABLE) for n in range(19)}
    b_j["i19"] = review.Judgement("i19", review.UNSURE)      # one real disagreement
    b = review.Sheet("Dr B", b_j)
    result = review.merge(a, b, min_kappa=0.0)                # gate itself would pass
    assert result["kappa_gate"]["passed"] is True
    assert result["usable_for_scoring"] is False              # but the dispute still blocks
    assert result["disputed"] == ["i19"]


def test_the_measured_kappa_is_preserved_whichever_way_the_gate_goes():
    for n_pairs, n_swaps in ((10, 5), (10, 1)):
        a, b, disagree = _balanced_sheets(n_pairs, n_swaps)
        result = review.merge(a, b, _adjudicate_all(disagree), adjudicator="Dr C",
                              min_kappa=review.MIN_KAPPA_PHASE_3)
        assert result["agreement"]["kappa"] == result["kappa_gate"]["observed_kappa"]


def test_rendering_a_blocked_gate_names_the_reason():
    a, b, disagree = _balanced_sheets(n_pairs=10, n_swaps_each_direction=5)
    result = review.merge(a, b, _adjudicate_all(disagree), adjudicator="Dr C",
                          min_kappa=review.MIN_KAPPA_PHASE_3)
    text = review.render(result)
    assert "kappa gate" in text
    assert "BLOCKED" in text


def test_review_data_and_kappa_computation_are_unchanged_by_the_gate():
    """
    The gate must not touch what is being measured, only whether the result is
    acted on. Same sheets, same raw agreement and kappa, gate on or off.
    """
    a, b, disagree = _balanced_sheets(n_pairs=10, n_swaps_each_direction=1)
    adjudications = _adjudicate_all(disagree)
    ungated = review.merge(a, b, adjudications, adjudicator="Dr C")
    gated = review.merge(a, b, adjudications, adjudicator="Dr C",
                         min_kappa=review.MIN_KAPPA_PHASE_3)
    assert ungated["agreement"] == gated["agreement"]
    assert ungated["settled"] == gated["settled"]
    assert ungated["counts"] == gated["counts"]


def test_the_cli_defaults_min_kappa_to_the_phase_3_floor(tmp_path):
    a_path = tmp_path / "a.jsonl"
    b_path = tmp_path / "b.jsonl"
    ids = [f"i{n}" for n in range(10)]
    with a_path.open("w") as fh:
        for i in ids:
            fh.write(json.dumps({"item_id": i, "reviewer": "Dr A", "label": "USABLE"}) + "\n")
    with b_path.open("w") as fh:
        for i in ids:
            fh.write(json.dumps({"item_id": i, "reviewer": "Dr B",
                                 "label": "BROKEN", "defect_class": "wrong_key"}) + "\n")
    code = review_cli.main(["merge", "--a", str(a_path), "--b", str(b_path)])
    assert code == 0  # merge only reports; it does not refuse

    out_path = tmp_path / "settled.jsonl"
    code = review_cli.main(["apply", "--a", str(a_path), "--b", str(b_path),
                            "--out", str(out_path)])
    assert code == 2, "total disagreement must be blocked by the default 0.67 floor"
    assert not out_path.exists()


def test_the_cli_min_kappa_flag_can_disable_the_gate(tmp_path):
    """
    --min-kappa with a negative value opts out, for inspecting what apply would
    write. Every disagreement is pre-adjudicated so the dispute gate is
    satisfied and the kappa gate is the only thing left that could block this.
    """
    a_path = tmp_path / "a.jsonl"
    b_path = tmp_path / "b.jsonl"
    adj_path = tmp_path / "adj.jsonl"
    out_path = tmp_path / "out.jsonl"
    ids = [f"i{n}" for n in range(10)]
    with a_path.open("w") as fh:
        for i in ids:
            fh.write(json.dumps({"item_id": i, "reviewer": "Dr A", "label": "USABLE"}) + "\n")
    with b_path.open("w") as fh:
        for i in ids:
            fh.write(json.dumps({"item_id": i, "reviewer": "Dr B",
                                 "label": "BROKEN", "defect_class": "wrong_key"}) + "\n")
    with adj_path.open("w") as fh:
        for i in ids:
            fh.write(json.dumps({"item_id": i, "label": "USABLE",
                                 "note": "ruled by Dr C"}) + "\n")

    blocked = review_cli.main(["apply", "--a", str(a_path), "--b", str(b_path),
                               "--adjudications", str(adj_path), "--adjudicator", "Dr C",
                               "--out", str(out_path)])
    assert blocked == 2, "kappa here is 0.0, well below the default 0.67 floor"
    assert not out_path.exists()

    code = review_cli.main(["apply", "--a", str(a_path), "--b", str(b_path),
                            "--adjudications", str(adj_path), "--adjudicator", "Dr C",
                            "--out", str(out_path), "--min-kappa", "-1"])
    assert code == 0, "a negative --min-kappa disables the gate entirely"
    assert out_path.exists()
    assert len(out_path.read_text().splitlines()) == 10


def test_the_fingerprint_covers_the_code_that_unpacks_a_reply():
    """
    `validator_fingerprint` hashed only `validator/`. The provider adapter --
    which decides whether a reply becomes a parsed answer or a crash -- sat
    outside it, so fixing the null-content defect would have changed what a
    run MEASURES while leaving the digest identical, and the repaired run
    would have been stamped comparable with the broken one.

    A fingerprint that excludes the code turning an HTTP reply into an answer
    is not fingerprinting the validator.
    """
    import tempfile
    from pathlib import Path

    from validator.holdout import ADAPTER_SOURCES, validator_fingerprint

    assert Path("benchmark/providers") in [Path(p) for p in ADAPTER_SOURCES]

    with tempfile.TemporaryDirectory() as tmp:
        adapter = Path(tmp) / "providers"
        adapter.mkdir()
        (adapter / "x.py").write_text("VERSION = 1\n")
        before = validator_fingerprint("cfg", adapters=(adapter,))
        (adapter / "x.py").write_text("VERSION = 2\n")
        after = validator_fingerprint("cfg", adapters=(adapter,))
    assert before != after, "an adapter change must move the fingerprint"
