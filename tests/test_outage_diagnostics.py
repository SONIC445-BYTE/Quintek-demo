"""
An outage has to be diagnosable afterwards, from the artifact alone.

WHY THIS FILE EXISTS
--------------------
Phase 0 (D018) terminated INCOMPLETE with 12 and 14 items lost from two arms.
The run artifact recorded `outages: 14`. That integer was everything that
survived: the per-item reasons went to `render()`'s stdout, which printed the
first five, and the console output is gone. Afterwards it was not possible to
say whether an item died in layer B or layer D, whether the call failed or the
reply was unreadable, or whether some of the lost items had ever reached a
model at all.

D017 was solvable for the opposite reason -- a journal had kept every raw
reply, and the cause was readable straight off it.

So these tests assert the artifact, not the console. Each one drives a
deliberately failing provider through the real pipeline and then reads the
recorded run back.
"""

from __future__ import annotations

import ast
import copy
import json
import pathlib

import pytest

from validator import analysis, outage, pipeline, runs
from validator.devset import CLEAN, DEFECTIVE, EDGE
from validator.conformance import ConformanceUnavailable
from validator.grounding import GroundingUnavailable
from validator.judge import JudgeUnavailable
from validator.scripted import ReplayProvider
from tools_validator_eval import evaluate

ROOT = pathlib.Path(__file__).resolve().parent.parent

# The evidence span must appear VERBATIM in the passage: `grounding.check`
# abstains and returns early otherwise, before the explanation call, which
# would silently make the two-call tests below test only one call.
GOOD_KEY = {"supported": ["A"],
            "evidence": {"A": "the fractional excretion of sodium is below one percent"},
            "passage_addresses_question": True}
GOOD_EXPL = {"contradicted": [], "gives_a_reason": True}
GOOD_JUDGE = {"answer": "A", "confidence": 0.9, "reasoning": "the passage says so"}
GOOD_CONF = {"concept_tested": "Pre-renal AKI", "matches_requested_concept": True,
             "cognitive_level": "application", "answerable_from_wording_alone": False}

ITEM = {
    "id": "it-1",
    "stem": "A patient has FeNa of 0.4%. What does this indicate?",
    "options": ["Pre-renal AKI", "Intrinsic AKI", "Post-renal AKI", "Normal"],
    "correct_index": 0,
    "explanation": "FeNa below 1% indicates pre-renal disease.",
    "source_passage": ("In pre-renal acute kidney injury the fractional excretion of sodium "
                       "is below one percent, whereas intrinsic injury shows values above two."),
    "question_type": "mcq",
    # Layer D reads `concept` and `difficulty`; without them it raises a
    # PRECONDITION outage before ever calling a model.
    "concept": "Pre-renal AKI",
    "difficulty": "pg_entry",
}


def _replies(item_id="it-1"):
    return {f"{item_id}:key": GOOD_KEY, f"{item_id}:explanation": GOOD_EXPL,
            f"{item_id}:judge": GOOD_JUDGE, f"{item_id}:conformance": GOOD_CONF}


def _run(item=None, *, errors=(), garbage=(), attempts=1, config=None, error_reply=""):
    item = item or ITEM
    provider = ReplayProvider(_replies(item["id"]), errors=set(errors), garbage=set(garbage),
                              attempts=attempts, error_reply=error_reply)
    cfg = config or pipeline.Config()
    return provider, pipeline.run(item, grounding_provider=provider, judge_provider=provider,
                                  conformance_provider=provider, config=cfg)


# ---------------------------------------------------------------------------
# Each mode is recorded as itself
# ---------------------------------------------------------------------------

def test_a_transport_failure_records_the_layer_the_mode_and_the_attempts():
    with pytest.raises(GroundingUnavailable) as exc:
        _run(errors={"it-1:key"}, attempts=3, error_reply="")
    rec = exc.value.as_dict()
    assert rec["layer"] == "grounding"
    assert rec["mode"] == outage.MODE_TRANSPORT
    assert rec["purpose"] == "key"
    assert rec["attempts"] == 3, "the attempt count at the point of failure must survive"
    assert rec["provider_error"] == "scripted backend failure"
    assert rec["model_was_called"] is True


def test_an_unparseable_reply_keeps_the_raw_text():
    """The raw reply is the evidence. D017 was solved by reading it."""
    with pytest.raises(GroundingUnavailable) as exc:
        _run(garbage={"it-1:key"})
    rec = exc.value.as_dict()
    assert rec["mode"] == outage.MODE_UNPARSEABLE
    assert rec["raw_reply"] == "I had a look and it seems fine to me."
    assert rec["raw_reply_truncated"] is False
    assert rec["model_was_called"] is True


def test_the_second_grounding_call_is_recorded_as_its_own_purpose():
    """B makes two calls. Which one failed is part of the diagnosis."""
    with pytest.raises(GroundingUnavailable) as exc:
        _run(garbage={"it-1:explanation"})
    rec = exc.value.as_dict()
    assert rec["mode"] == outage.MODE_UNPARSEABLE
    assert rec["purpose"] == "explanation"


def test_a_precondition_failure_says_no_model_was_called():
    """
    The distinction the bare count destroyed. An item with no passage cannot be
    grounded, and that is a corpus finding -- counting it against a candidate
    model's reliability would be wrong.
    """
    item = dict(ITEM, source_passage="")
    provider = ReplayProvider(_replies())
    # require_source=False so Layer A does not flag the missing passage first:
    # this is Layer B's own precondition guard being exercised.
    with pytest.raises(GroundingUnavailable) as exc:
        pipeline.run(item, grounding_provider=provider, judge_provider=provider,
                     conformance_provider=provider,
                     config=pipeline.Config(require_source=False))
    rec = exc.value.as_dict()
    assert rec["mode"] == outage.MODE_PRECONDITION
    assert rec["model_was_called"] is False
    assert rec["raw_reply"] is None
    assert provider.seen == [], "no call may be made for a precondition failure"


def test_a_reply_that_parses_but_does_not_answer_is_its_own_mode():
    provider = ReplayProvider({**_replies(), "it-1:judge": {"answer": "Z", "confidence": 0.9}})
    with pytest.raises(JudgeUnavailable) as exc:
        pipeline.run(ITEM, grounding_provider=provider, judge_provider=provider,
                     conformance_provider=provider, config=pipeline.Config())
    rec = exc.value.as_dict()
    assert rec["layer"] == "judge"
    assert rec["mode"] == outage.MODE_UNUSABLE
    assert "Z" in rec["raw_reply"]


def test_conformance_failures_are_attributed_to_layer_D_not_to_grounding():
    """B and D share a provider seat. Without the layer, the split is guesswork."""
    with pytest.raises(ConformanceUnavailable) as exc:
        _run(garbage={"it-1:conformance"})
    rec = exc.value.as_dict()
    assert rec["layer"] == "conformance"
    assert rec["mode"] == outage.MODE_UNPARSEABLE


def test_a_missing_provider_is_configuration_not_a_model_failure():
    with pytest.raises(GroundingUnavailable) as exc:
        pipeline.run(ITEM, grounding_provider=None, judge_provider=None,
                     conformance_provider=None, config=pipeline.Config())
    rec = exc.value.as_dict()
    assert rec["mode"] == outage.MODE_CONFIGURATION
    assert rec["model_was_called"] is False


def test_an_unknown_mode_is_refused_at_the_raise_site():
    with pytest.raises(ValueError, match="unknown outage mode"):
        GroundingUnavailable("x", mode="something-invented")


# ---------------------------------------------------------------------------
# The split D018 could not report
# ---------------------------------------------------------------------------

class _Case:
    """
    Stand-in for `validator.devset.Case`.

    Carries every attribute `analysis.report` reads, not only the ones the
    assertions below touch: a stub that is missing one fails inside the
    analysis rather than in the test, which reads as a defect in the code
    under test when it is a defect in the fixture.
    """

    def __init__(self, item, label, *, defect_class="", derived_from="",
                 mutation="", edge_reason=""):
        self.item = _Item(item)
        self.id = item["id"]
        self.label = label
        self.in_arm = label in (CLEAN, DEFECTIVE)
        self.defect_class = defect_class
        self.derived_from = derived_from
        self.mutation = mutation
        self.edge_reason = edge_reason


class _Item:
    def __init__(self, raw):
        self._raw = raw
        self.subject = raw.get("subject", "Medicine")
        self.defect_note = ""

    def as_dict(self):
        return copy.deepcopy(self._raw)


def _mixed_run():
    """
    Five items, chosen so every layer and every reachable mode appears:

        it-1  succeeds
        it-2  B, transport      (the call failed)
        it-3  B, unparseable    (the explanation reply had no JSON)
        it-4  D, unparseable    (the conformance reply had no JSON)
        it-5  D, precondition   (no declared concept -- NO model call)
    """
    cases, replies, errors, garbage = [], {}, set(), set()
    for n, (label, how) in enumerate([
            (CLEAN, "ok"), (CLEAN, "transport-B"), (DEFECTIVE, "unparseable-B"),
            (DEFECTIVE, "unparseable-D"), (CLEAN, "precondition")], start=1):
        item = dict(ITEM, id=f"it-{n}")
        if how == "precondition":
            item["concept"] = ""      # Layer D cannot compare against nothing
        replies.update(_replies(item["id"]))
        if how == "transport-B":
            errors.add(f"it-{n}:key")
        if how == "unparseable-B":
            garbage.add(f"it-{n}:explanation")
        if how == "unparseable-D":
            garbage.add(f"it-{n}:conformance")
        cases.append(_Case(item, label))
    provider = ReplayProvider(replies, errors=errors, garbage=garbage, attempts=2)
    verdicts, outages = evaluate(cases, grounding_provider=provider, judge_provider=provider,
                                 config=pipeline.Config(), conformance_provider=provider)
    return cases, verdicts, outages


def test_the_b_versus_d_split_is_readable():
    _cases, verdicts, outages = _mixed_run()
    assert len(verdicts) == 1, "only the healthy item should have produced a verdict"
    assert len(outages) == 4

    summary = outage.summarise(outages)
    assert summary["by_layer"] == {"conformance": 2, "grounding": 2}
    assert summary["by_mode"] == {outage.MODE_PRECONDITION: 1,
                                  outage.MODE_TRANSPORT: 1,
                                  outage.MODE_UNPARSEABLE: 2}
    assert summary["by_layer_and_mode"] == {
        "conformance/precondition": 1,
        "conformance/unparseable": 1,
        "grounding/transport": 1,
        "grounding/unparseable": 1,
    }


def test_the_summary_separates_items_that_never_reached_a_model():
    _cases, _verdicts, outages = _mixed_run()
    summary = outage.summarise(outages)
    assert summary["model_was_called"] == 3
    assert summary["model_was_not_called"] == 1, (
        "a precondition failure is a corpus finding; counting it as a model failure "
        "is the misattribution the bare count made unavoidable")


def test_every_outage_record_carries_its_item_id_and_label():
    _cases, _verdicts, outages = _mixed_run()
    assert {o["id"] for o in outages} == {"it-2", "it-3", "it-4", "it-5"}
    assert all(o["label"] in (CLEAN, DEFECTIVE) for o in outages)


# ---------------------------------------------------------------------------
# It reaches the artifact, which is the whole point
# ---------------------------------------------------------------------------

def test_the_detail_survives_a_round_trip_through_the_run_record(tmp_path):
    _cases, _verdicts, outages = _mixed_run()
    run = runs.Run(
        at=runs.now(), kind="development", corpus="x", corpus_hash="h",
        validator_version=pipeline.VALIDATOR_VERSION, config="v[ABCD]",
        providers=[], counts={}, outages=len(outages),
        outage_detail=list(outages), outage_summary=outage.summarise(outages))
    path = runs.record(run, runs_dir=tmp_path)

    raw = json.loads(pathlib.Path(path).read_text())
    assert raw["outages"] == 4
    assert raw["outage_summary"]["by_layer_and_mode"]["conformance/unparseable"] == 1
    assert len(raw["outage_detail"]) == 4

    unparseable = [o for o in raw["outage_detail"] if o["mode"] == outage.MODE_UNPARSEABLE]
    assert all(o["raw_reply"] for o in unparseable), "the raw reply must be in the FILE"

    back = runs.Run.from_dict(raw) if hasattr(runs.Run, "from_dict") else None
    if back is not None:
        assert len(back.outage_detail) == 4


def test_the_artifact_answers_the_question_d018_could_not(tmp_path):
    """
    The exit condition, stated as a test: from the artifact alone, without the
    console, can a reader get the B-vs-D and transport-vs-unparseable split?
    """
    _cases, _verdicts, outages = _mixed_run()
    run = runs.Run(at=runs.now(), kind="development", corpus="x", corpus_hash="h",
                   validator_version=pipeline.VALIDATOR_VERSION, config="v[ABCD]",
                   providers=[], counts={}, outages=len(outages),
                   outage_detail=list(outages), outage_summary=outage.summarise(outages))
    raw = json.loads(pathlib.Path(runs.record(run, runs_dir=tmp_path)).read_text())

    by = raw["outage_summary"]["by_layer_and_mode"]
    grounding_deaths = sum(v for k, v in by.items() if k.startswith("grounding/"))
    conformance_deaths = sum(v for k, v in by.items() if k.startswith("conformance/"))
    unparseable = sum(v for k, v in by.items() if k.endswith("/unparseable"))
    transport = sum(v for k, v in by.items() if k.endswith("/transport"))

    assert (grounding_deaths, conformance_deaths) == (2, 2), (
        "the B-vs-D split is the thing D018 could not report")
    assert (unparseable, transport) == (2, 1), (
        "transport and unparseable must stay distinguished, not merged")


# ---------------------------------------------------------------------------
# An outage is not a coverage gap
# ---------------------------------------------------------------------------

def test_an_edge_item_lost_to_an_outage_is_not_reported_as_a_coverage_gap():
    """
    `edge_behaviour` used to assign "no layer ran that could have seen this"
    whenever a verdict was missing. That string means the design has no check
    for something -- the opposite of a layer that broke.
    """
    item = dict(ITEM, id="edge-1")
    case = _Case(item, EDGE, edge_reason="negatively phrased stem")
    lost = [{"id": "edge-1", "layer": "grounding", "mode": outage.MODE_TRANSPORT,
             "model_was_called": True}]

    report = analysis.edge_behaviour([case], [], lost)
    assert report["by_verdict"] == {analysis.OUTAGE: 1}
    assert analysis.NOBODY_LOOKED not in report["by_verdict"]
    assert report["items"][0]["outage"] == {
        "layer": "grounding", "mode": outage.MODE_TRANSPORT, "model_was_called": True}


def test_report_requires_the_outages_so_the_mislabelling_cannot_return():
    with pytest.raises(TypeError):
        analysis.report([], [])


# ---------------------------------------------------------------------------
# A new raise site cannot forget to say which mode it is
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("module", ["grounding", "judge", "conformance", "pipeline"])
def test_every_raise_site_declares_a_mode(module):
    tree = ast.parse((ROOT / "validator" / f"{module}.py").read_text(encoding="utf-8"))
    missing = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Raise) or node.exc is None:
            continue
        call = node.exc
        if not isinstance(call, ast.Call):
            continue
        name = getattr(call.func, "id", None) or getattr(call.func, "attr", None) or ""
        if not name.endswith("Unavailable") and not name.endswith("NotIndependent"):
            continue
        if not any(kw.arg == "mode" for kw in call.keywords):
            missing.append(f"{module}.py:{node.lineno}")
    assert not missing, (
        "these raise sites do not say which failure mode they are, so an item lost "
        "there would be unattributable in the artifact:\n  " + "\n  ".join(missing))


# ---------------------------------------------------------------------------
# The false-positive union, which D018 could not answer either
# ---------------------------------------------------------------------------

def test_false_positives_record_every_item_id_not_a_sample():
    """
    D018 stored three examples per check. Afterwards "how many of the 19 false
    positives would a fix to these two checks remove" was unanswerable, because
    23 check-hits across 19 items means some carry several flags and which ones
    was not recoverable. The adjudication that followed had to rebuild the set
    from the run journal, and recovered only 16 of 17.
    """
    from validator.metrics import FLAGGED

    class _V:
        def __init__(self, item_id, flags):
            self.item_id = item_id
            self.verdict = FLAGGED
            self.flags = flags
            self.detail = []

    cases = [_Case(dict(ITEM, id=f"c{i}"), CLEAN) for i in range(1, 6)]
    verdicts = [
        _V("c1", [("conformance", "below_declared_difficulty")]),
        _V("c2", [("conformance", "below_declared_difficulty"),
                  ("conformance", "answerable_from_wording_alone")]),
        _V("c3", [("conformance", "answerable_from_wording_alone")]),
        _V("c4", [("grounding", "explanation_contradicts_passage")]),
        _V("c5", [("conformance", "below_declared_difficulty"),
                  ("grounding", "explanation_contradicts_passage")]),
    ]
    report = analysis.false_positives(cases, verdicts)

    assert report["count"] == 5
    assert report["check_hits"] == 7, "hits exceed items when a check overlaps"
    assert report["multi_check_items"] == 2

    # Every id, per check -- not a three-item sample.
    by_check = {f"{r['layer']}/{r['check']}": r["item_ids"] for r in report["by_check"]}
    assert by_check["conformance/below_declared_difficulty"] == ["c1", "c2", "c5"]
    assert by_check["conformance/answerable_from_wording_alone"] == ["c2", "c3"]

    # And the union, which is what prices a fix: fixing BOTH conformance
    # checks clears c1, c2 and c3 but leaves c4 and c5, because c5 is also
    # flagged by grounding. That is the question the counts alone cannot answer.
    per_item = {r["id"]: set(r["checks"]) for r in report["items"]}
    conformance_only = {i for i, ch in per_item.items()
                        if all(c.startswith("conformance/") for c in ch)}
    assert conformance_only == {"c1", "c2", "c3"}
    assert set(per_item) - conformance_only == {"c4", "c5"}


# ---------------------------------------------------------------------------
# Checks that run and report but decide nothing
# ---------------------------------------------------------------------------

def test_a_non_gating_check_reports_without_flagging():
    """
    `below_declared_difficulty` compares an item's declared difficulty against
    the level the candidate reports. It applies that rule faithfully -- the
    adjudication found zero implementation issues. What has no evidence is the
    other side: every corpus item is `gold_standard: false`, `reviewed_by`
    empty, so a disagreement is not evidence about either.

    It still runs and still reports. It just stops deciding.
    """
    from validator import gating

    provider = ReplayProvider({
        **_replies(),
        "it-1:conformance": {"concept_tested": "Pre-renal AKI",
                             "matches_requested_concept": True,
                             "cognitive_level": "recall",          # below pg_entry
                             "answerable_from_wording_alone": False},
    })
    verdict = pipeline.run(ITEM, grounding_provider=provider, judge_provider=provider,
                           conformance_provider=provider, config=pipeline.Config())

    assert ("conformance", "below_declared_difficulty") in verdict.non_gating
    assert ("conformance", "below_declared_difficulty") not in verdict.flags
    assert verdict.verdict != "FLAGGED", "a non-gating finding must not decide the verdict"

    recorded = verdict.as_dict()["non_gating"]
    assert recorded and recorded[0]["why"], "the record must carry WHY it does not gate"
    assert "gold_standard" in recorded[0]["why"]
    assert gating.is_gating("key_not_supported_by_passage") is True


def test_a_gating_check_still_decides():
    """The exclusion is one check, not a general softening."""
    from validator import gating

    provider = ReplayProvider({
        **_replies(),
        "it-1:key": {"supported": ["B"],
                     "evidence": {"B": "the fractional excretion of sodium is below one percent"},
                     "passage_addresses_question": True},
    })
    verdict = pipeline.run(ITEM, grounding_provider=provider, judge_provider=provider,
                           conformance_provider=provider, config=pipeline.Config())
    assert verdict.verdict == "FLAGGED"
    assert verdict.flags, "a check with gold behind it still gates"
    assert gating.is_gating("below_declared_difficulty") is False


def test_the_cost_of_the_exclusion_is_recorded_not_hidden():
    """
    Excluding a check removes a capability. The blind spot it creates is
    declared, with the reason, so nobody reads the resulting specificity as
    free.
    """
    from validator import scripted

    assert "trivial" in scripted.UNCOVERED_BY_DESIGN
    why = scripted.UNCOVERED_REASONS["trivial"]
    assert "below_declared_difficulty" in why
    assert "gold" in why, "the reason must say what would recover it"
