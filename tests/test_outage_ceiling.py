"""
A ceiling stop is not a backend failure.

WHAT THIS FILE IS ABOUT
-----------------------
The first real Groq run recorded 33 outages: 9 `rate_limited` and 24
`grounding/transport`. Read cold, that artifact says a quarter of the corpus
was dropped by the host.

None of the 24 were Groq. The wall-clock ceiling stopped the run,
`WallClockExceeded` arrived at the layer as an ordinary failed
`GenerationResponse`, and items that were never asked at all were filed under
the mode that means "the call failed". The provider was exonerated by hand,
from outside the artifact -- which is exactly the position D018 was in, and
exactly what `validator/outage.py` exists to prevent.

The mechanism was the same one twice. `transport` is the DEFAULT bucket, and
each of the three layers decided membership for itself with its own copy of

    mode=(MODE_RATE_LIMITED if was_rate_limited(r) else MODE_TRANSPORT)

so a failure nobody had thought about became a failure blamed on the host, in
triplicate. The fix is one classifier, and a test that fails when a layer goes
back to hand-rolling it.

STRUCTURE
---------
`TestTheClassifier`  -- the dispatch itself.
`TestEveryLayer`     -- parameterised over layers DISCOVERED from the source,
                        so a fourth layer that misclassifies a ceiling stop
                        fails here without anyone remembering to add it.
`TestNoLayerHandRolls` -- the structural property, read off the AST.
`TestTheSummary`     -- the split a reader needs before drawing a conclusion.
"""

from __future__ import annotations

import ast
import copy
import pathlib

import pytest

from validator import conformance, grounding, judge, outage
from validator.outage import (MODE_BUDGET, MODE_RATE_LIMITED, MODE_TRANSPORT,
                              MODE_WALL_CLOCK, LayerUnavailable)

ROOT = pathlib.Path(__file__).resolve().parent.parent
LAYER_DIR = ROOT / "validator"

# A real ceiling message, copied from `WallClock.check` and `Budget.spend`, so
# this tests the string the run actually produces rather than a convenient one.
WALL_CLOCK_ERROR = (
    "WallClockExceeded: the wall-clock ceiling of 45 minute(s) was reached after "
    "45.0 minute(s). No new calls are being started; whatever arm was in progress "
    "is incomplete and produces no delta.")
BUDGET_ERROR = (
    "BudgetExhausted: the total call budget of 500 outbound attempts is spent.")
RATE_LIMIT_ERROR = (
    "RateLimited: groq (api.groq.com) HTTP 429 rate limited: over quota")
TRANSPORT_ERROR = "RuntimeError: groq (api.groq.com) HTTP 503: upstream unavailable"


class FailingResponse:
    """
    The shape a layer sees. Deliberately not `ReplayProvider`, whose error
    string is fixed at "scripted backend failure" -- the whole point here is
    which string arrived.
    """

    def __init__(self, error: str):
        self.ok = False
        self.error = error
        self.raw_output = ""
        self.parsed = None
        self.attempts = 3


class FailingProvider:
    """A provider whose every call comes back failed with one fixed error."""

    name = "ceiling-double"
    model = "none"
    model_version = "v0"
    model_family = "none"
    is_model = False
    is_oracle = False

    def __init__(self, error: str):
        self.error = error

    def generate(self, request):
        return FailingResponse(self.error)


ITEM = {
    "id": "it-ceiling",
    "stem": "A patient has FeNa of 0.4%. What does this indicate?",
    "options": ["Pre-renal AKI", "Intrinsic AKI", "Post-renal AKI", "Normal"],
    "correct_index": 0,
    "explanation": "FeNa below 1% indicates pre-renal disease.",
    "source_passage": ("In pre-renal acute kidney injury the fractional excretion of "
                       "sodium is below one percent, whereas intrinsic injury shows "
                       "values above two."),
    "question_type": "mcq",
    "concept": "Pre-renal AKI",
    "difficulty": "foundation",
}


def discovered_layers() -> dict:
    """
    Every module under `validator/` that declares a `LayerUnavailable`
    subclass and a `check` entry point.

    DERIVED, not listed. A test that hard-codes three layer names goes on
    passing when a fourth is added, and the fourth is precisely the one whose
    classification nobody checked.
    """
    found = {}
    for path in sorted(LAYER_DIR.glob("*.py")):
        tree = ast.parse(path.read_text())
        declares_outage = any(
            isinstance(n, ast.ClassDef)
            and any(getattr(b, "id", "") == "LayerUnavailable" for b in n.bases)
            for n in ast.walk(tree))
        has_check = any(isinstance(n, ast.FunctionDef) and n.name == "check"
                        for n in tree.body)
        if declares_outage and has_check:
            found[path.stem] = path
    return found


LAYERS = discovered_layers()
LAYER_MODULES = {"grounding": grounding, "conformance": conformance, "judge": judge}


def test_every_discovered_layer_is_exercised_below():
    """
    The guard on the guard. If a new layer module appears and this mapping is
    not extended, say so HERE rather than letting the parameterised tests
    quietly cover a subset.
    """
    assert set(LAYERS) == set(LAYER_MODULES), (
        f"validator/ declares layers {sorted(LAYERS)} but this file exercises "
        f"{sorted(LAYER_MODULES)}. A layer that is not exercised here is a layer "
        "whose ceiling classification is unverified.")


class TestTheClassifier:

    @pytest.mark.parametrize("error, expected", [
        (WALL_CLOCK_ERROR, MODE_WALL_CLOCK),
        (BUDGET_ERROR, MODE_BUDGET),
        (RATE_LIMIT_ERROR, MODE_RATE_LIMITED),
        (TRANSPORT_ERROR, MODE_TRANSPORT),
        ("TimeoutError: groq (api.groq.com) request failed: [SSL] bad handshake",
         MODE_TRANSPORT),
    ])
    def test_dispatch(self, error, expected):
        assert outage.classify_failure(FailingResponse(error)) == expected

    def test_a_ceiling_stop_is_not_recorded_as_reaching_a_model(self):
        for mode in outage.RUN_CEILING:
            record = LayerUnavailable("stopped", mode=mode).as_dict()
            assert record["model_was_called"] is False, (
                f"{mode} says the run stopped before asking. Recording it as having "
                "reached a model puts an item the host never saw into the evidence "
                "about that host.")

    def test_a_ceiling_stop_is_self_inflicted(self):
        for mode in outage.RUN_CEILING:
            assert mode in outage.SELF_INFLICTED

    def test_transport_is_not_self_inflicted(self):
        assert MODE_TRANSPORT not in outage.SELF_INFLICTED


class TestEveryLayer:
    """
    The behavioural half. Each layer is driven with a real ceiling error and
    must classify it as a ceiling stop -- not as the host failing.
    """

    @pytest.mark.parametrize("layer_name", sorted(LAYER_MODULES))
    @pytest.mark.parametrize("error, expected", [
        (WALL_CLOCK_ERROR, MODE_WALL_CLOCK),
        (BUDGET_ERROR, MODE_BUDGET),
    ])
    def test_ceiling_stop_is_not_blamed_on_the_host(self, layer_name, error, expected):
        module = LAYER_MODULES[layer_name]
        with pytest.raises(LayerUnavailable) as caught:
            module.check(copy.deepcopy(ITEM), FailingProvider(error))
        assert caught.value.mode == expected, (
            f"{layer_name} recorded a ceiling stop as {caught.value.mode!r}. The run "
            "stopped itself; the host was never asked, and an artifact that says "
            "otherwise accuses a provider that did nothing.")
        assert caught.value.model_was_called is False

    @pytest.mark.parametrize("layer_name", sorted(LAYER_MODULES))
    def test_a_real_backend_failure_is_still_transport(self, layer_name):
        """The fix must not swallow the case the bucket is FOR."""
        module = LAYER_MODULES[layer_name]
        with pytest.raises(LayerUnavailable) as caught:
            module.check(copy.deepcopy(ITEM), FailingProvider(TRANSPORT_ERROR))
        assert caught.value.mode == MODE_TRANSPORT
        assert caught.value.model_was_called is True

    @pytest.mark.parametrize("layer_name", sorted(LAYER_MODULES))
    def test_a_429_is_still_rate_limited(self, layer_name):
        module = LAYER_MODULES[layer_name]
        with pytest.raises(LayerUnavailable) as caught:
            module.check(copy.deepcopy(ITEM), FailingProvider(RATE_LIMIT_ERROR))
        assert caught.value.mode == MODE_RATE_LIMITED


class TestNoLayerHandRolls:
    """
    The structural half, read off the AST.

    The behavioural tests above pass if a layer happens to get the answer
    right. This one fails if a layer decides the answer for itself at all --
    which is the shape that let `WallClockExceeded` be missed in three files
    simultaneously.
    """

    @pytest.mark.parametrize("layer_name", sorted(LAYERS))
    def test_mode_is_never_a_conditional_at_the_raise_site(self, layer_name):
        tree = ast.parse(LAYERS[layer_name].read_text())
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
                continue
            for kw in node.exc.keywords:
                if kw.arg == "mode" and isinstance(kw.value, ast.IfExp):
                    offenders.append(node.lineno)
        assert not offenders, (
            f"validator/{layer_name}.py picks an outage mode with an inline conditional "
            f"at line(s) {offenders}. Classification belongs in "
            "outage.classify_failure, so a newly-recognised failure is classified in "
            "every layer at once or in none.")

    @pytest.mark.parametrize("layer_name", sorted(LAYERS))
    def test_layers_do_not_import_the_raw_predicate(self, layer_name):
        """
        `was_rate_limited` is the building block `classify_failure` is made of.
        A layer importing it directly is a layer about to reinvent the ternary.
        """
        tree = ast.parse(LAYERS[layer_name].read_text())
        imported = {
            alias.name
            for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
            for alias in node.names
            if node.module == "validator.outage"
        }
        assert "was_rate_limited" not in imported, (
            f"validator/{layer_name}.py imports was_rate_limited directly. Use "
            "classify_failure; the predicate is one input to the decision, not the "
            "decision.")


class TestTheSummary:

    def test_the_groq_run_reads_correctly_now(self):
        """
        The 33 outages of the first real Groq run, rebuilt. Every one was ours.
        The summary must say so without the reader doing arithmetic.
        """
        records = (
            [LayerUnavailable("ceiling", mode=MODE_WALL_CLOCK, item_id=f"w{n}").as_dict()
             for n in range(24)]
            + [LayerUnavailable("429", mode=MODE_RATE_LIMITED, item_id=f"r{n}").as_dict()
               for n in range(9)])
        for record in records[:24]:
            record["layer"] = "grounding"
        summary = outage.summarise(records)

        assert summary["total"] == 33
        assert summary["self_inflicted"] == 33
        assert summary["stopped_by_run_ceiling"] == 24
        assert summary["attributable_to_host"] == 0, (
            "every one of these 33 was the run's own doing -- 24 to our ceiling, 9 to "
            "our pace. A summary that leaves a host to blame here is the bug.")
        assert summary["by_mode"] == {MODE_WALL_CLOCK: 24, MODE_RATE_LIMITED: 9}
        assert MODE_TRANSPORT not in summary["by_mode"]

    def test_a_genuine_host_failure_is_still_attributed_to_the_host(self):
        records = [
            LayerUnavailable("503", mode=MODE_TRANSPORT, item_id="t1").as_dict(),
            LayerUnavailable("ceiling", mode=MODE_WALL_CLOCK, item_id="w1").as_dict(),
        ]
        summary = outage.summarise(records)
        assert summary["attributable_to_host"] == 1
        assert summary["self_inflicted"] == 1
