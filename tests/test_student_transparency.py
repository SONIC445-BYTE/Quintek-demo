"""
Tests for the learner-facing AI transparency surface.

The defining requirement of this screen is that it never shows a figure that
was not measured. Most of these tests are therefore about absence: an empty
archive, an unscored task, a model evaluated once, a model serving without
evidence behind it. Each has a correct rendering, and none of them is a
plausible-looking number.
"""

from __future__ import annotations

import json

import pytest

from benchmark.analytics import RunArchive
from student.ai import AIEngine
from student.api import StudentAPI
from student.db import Database
from student.transparency import CATEGORIES, SCOPE_NOTE, TransparencyService


def write_run(root, run_id, candidate_id, gates, *, outcome="PASS",
              timestamp="2026-01-01T00:00:00Z", model_id="vendor/model-a",
              provider="nvidia", version="0.4"):
    """
    `gates` is {gate_id: (track, estimate, n, direction, status)}.

    Written as report.json rather than through a fixture builder so these
    tests exercise the same loading path the real archive uses.
    """
    d = root / run_id
    d.mkdir(parents=True, exist_ok=True)
    scores = {}
    for gate_id, (track, estimate, n, direction, status) in gates.items():
        scores[gate_id] = {
            "gate_id": gate_id, "track": track, "metric": gate_id.lower(),
            "estimate": estimate, "n": n, "required_n": n, "mandatory": True,
            "direction": direction, "status": status, "scale_max": 1.0,
            "ci_lower": max(0.0, estimate - 0.05), "ci_upper": min(1.0, estimate + 0.05),
        }
    (d / "report.json").write_text(json.dumps({
        "run_id": run_id, "candidate_id": candidate_id, "benchmark_version": version,
        "outcome": outcome, "rankable": True, "timestamp": timestamp,
        "integrity": {"satisfied": True}, "scores": scores,
        "candidate_manifest": {"model_id": model_id, "provider": provider,
                               "model_version": "1.0"},
    }))


PASSING = {
    "GATE-A-ACC": ("A_medical_qa", 0.82, 400, "lower", "PASS"),
    "GATE-E-RUBRIC": ("E_generation", 0.75, 120, "lower", "PASS"),
    "GATE-F-FALSEAPPROVE": ("F_validation", 0.03, 200, "upper", "PASS"),
}


@pytest.fixture()
def archive(tmp_path):
    root = tmp_path / "runs"
    root.mkdir()
    return root


# ---------- the empty install ----------

def test_an_install_with_no_runs_renders_and_says_why(tmp_path):
    service = TransparencyService()
    overview = service.overview()
    assert overview["title"] == "Quintek AI Benchmark"
    assert overview["ranking"]["entries"] == []
    assert "nothing measured to show" in overview["ranking"]["empty_reason"]


def test_an_empty_archive_never_invents_entries(archive):
    service = TransparencyService(archive=RunArchive(archive))
    for category in CATEGORIES:
        result = service.ranking(category.key)
        assert result["entries"] == []
        assert result["measured_count"] == 0


# ---------- scope framing ----------

def test_every_payload_carries_the_scope_note(archive):
    write_run(archive, "r1", "cand-a", PASSING)
    service = TransparencyService(archive=RunArchive(archive))
    for payload in (service.overview(), service.categories(),
                    service.ranking("overall"), service.ranking("medical_qa"),
                    service.powering(), service.profile("cand-a")):
        assert payload["scope_note"] == SCOPE_NOTE


def test_the_disclaimer_denies_being_a_general_ranking(archive):
    overview = TransparencyService(archive=RunArchive(archive)).overview()
    assert "not a general ranking of AI models" in overview["disclaimer"]
    assert "not a general ranking of AI models" in overview["scope_note"]


def test_the_screen_never_calls_itself_a_leaderboard(archive):
    """
    The word may appear only where the page denies being one. Anywhere it
    names this screen -- title, tab labels, the ranking payload -- it is
    claiming a general comparison the data does not support.
    """
    service = TransparencyService(archive=RunArchive(archive))
    overview = service.overview()
    assert overview["title"] == "Quintek AI Benchmark"

    self_describing = [overview["title"], overview["scope_note"],
                       json.dumps(overview["categories"]),
                       json.dumps(service.ranking("overall"))]
    for text in self_describing:
        assert "leaderboard" not in text.lower()

    # The one permitted use, and it is a denial.
    assert "not comparable to public leaderboards" in overview["disclaimer"]


# ---------- rankings ----------

def test_ranking_orders_by_score_and_assigns_ranks(archive):
    write_run(archive, "r1", "cand-a", PASSING, model_id="vendor/a")
    weaker = dict(PASSING)
    weaker["GATE-A-ACC"] = ("A_medical_qa", 0.61, 400, "lower", "PASS")
    write_run(archive, "r2", "cand-b", weaker, model_id="vendor/b")

    board = TransparencyService(archive=RunArchive(archive)).ranking("medical_qa")
    assert [e["candidate_id"] for e in board["entries"]] == ["cand-a", "cand-b"]
    assert [e["rank"] for e in board["entries"]] == [1, 2]


def test_every_ranked_entry_carries_its_sample_size_and_interval(archive):
    write_run(archive, "r1", "cand-a", PASSING)
    board = TransparencyService(archive=RunArchive(archive)).ranking("medical_qa")
    entry = board["entries"][0]
    assert entry["sample_size"] == 400
    assert entry["confidence_interval"] is not None
    assert len(entry["confidence_interval"]) == 2


def test_an_unmeasured_candidate_gets_a_null_score_and_no_rank(archive):
    """Zero is a measurement. 'Not measured' must not render as zero."""
    write_run(archive, "r1", "cand-a", PASSING)
    write_run(archive, "r2", "cand-b", {
        "GATE-E-RUBRIC": ("E_generation", 0.5, 50, "lower", "PASS")})

    board = TransparencyService(archive=RunArchive(archive)).ranking("medical_qa")
    by_id = {e["candidate_id"]: e for e in board["entries"]}
    assert by_id["cand-b"]["score"] is None
    assert by_id["cand-b"]["rank"] is None
    assert board["measured_count"] == 1
    # ...and it sorts last, not first-with-a-zero.
    assert board["entries"][-1]["candidate_id"] == "cand-b"


def test_an_error_rate_gate_is_inverted_so_lower_is_not_shown_as_worse(archive):
    """
    GATE-F-FALSEAPPROVE is a false-approval RATE: 0.03 is excellent. If it
    were ranked raw, the best validator would appear bottom of the table.
    """
    write_run(archive, "good", "cand-good", {
        "GATE-F-FALSEAPPROVE": ("F_validation", 0.03, 200, "upper", "PASS")})
    write_run(archive, "bad", "cand-bad", {
        "GATE-F-FALSEAPPROVE": ("F_validation", 0.40, 200, "upper", "FAIL")})

    board = TransparencyService(archive=RunArchive(archive)).ranking("question_validation")
    assert board["entries"][0]["candidate_id"] == "cand-good"
    assert board["entries"][0]["score"] > board["entries"][1]["score"]


def test_ranking_is_stable_between_identical_loads(archive):
    for i in range(4):
        write_run(archive, f"r{i}", f"cand-{i}", PASSING, model_id=f"vendor/m{i}")
    service = TransparencyService(archive=RunArchive(archive))
    first = [e["candidate_id"] for e in service.ranking("medical_qa")["entries"]]
    second = [e["candidate_id"] for e in service.ranking("medical_qa")["entries"]]
    assert first == second


def test_an_unknown_category_raises_rather_than_returning_the_overall_table(archive):
    with pytest.raises(KeyError):
        TransparencyService(archive=RunArchive(archive)).ranking("astrology")


def test_every_category_names_gates_that_exist_in_the_gate_registry():
    """
    A typo in a category's gate_ids renders that tab permanently empty and
    looks exactly like "no candidate scored this yet". This is the guard.
    """
    registry_text = (__import__("pathlib").Path("configs/gate_registry_v0_4.json")
                     .read_text())
    for category in CATEGORIES:
        for gate_id in category.gate_ids:
            assert f'"{gate_id}"' in registry_text, (
                f"category {category.key!r} names {gate_id}, which is not in the gate registry")


def test_categories_report_how_much_each_tab_has_to_show(archive):
    write_run(archive, "r1", "cand-a", PASSING)
    tabs = TransparencyService(archive=RunArchive(archive)).categories()["categories"]
    by_key = {t["key"]: t for t in tabs}
    assert by_key["medical_qa"]["measured_count"] == 1
    # Nothing in PASSING touches relationships.
    assert by_key["relationships"]["measured_count"] == 0


# ---------- what is powering Quintek ----------

def test_powering_reports_every_task_even_with_nothing_configured(tmp_path):
    from benchmark.tasks import TaskType

    engine = AIEngine(Database(tmp_path / "s.db"))
    state = TransparencyService(ai_engine=engine).powering()
    assert len(state["tasks"]) == len(list(TaskType))
    assert state["all_evidence_backed"] is False
    assert "no model at all" in state["unresolved_note"]


def test_powering_warns_plainly_when_an_unevaluated_model_is_serving(tmp_path):
    engine = AIEngine(Database(tmp_path / "s.db"), development_candidate="cand-dev")
    state = TransparencyService(ai_engine=engine).powering()
    assert state["all_evidence_backed"] is False
    assert "has not passed a Quintek benchmark run" in state["warning"]
    assert all(t["source"] == "development_override" for t in state["tasks"])
    assert all(t["evidence_backed"] is False for t in state["tasks"])


def test_powering_reflects_a_promotion(tmp_path, archive):
    write_run(archive, "r1", "cand-a", PASSING)
    engine = AIEngine(Database(tmp_path / "s.db"))
    engine.promote("EXPLANATION", "cand-a", "r1", outcome="PASS")

    state = TransparencyService(archive=RunArchive(archive), ai_engine=engine).powering()
    task = next(t for t in state["tasks"] if t["task_type"] == "EXPLANATION")
    assert task["source"] == "promoted"
    assert task["evidence_backed"] is True
    assert "reviewed this model's benchmark run" in task["basis"]


def test_powering_uses_learner_language_for_task_names(tmp_path):
    engine = AIEngine(Database(tmp_path / "s.db"))
    state = TransparencyService(ai_engine=engine).powering()
    labels = {t["task_label"] for t in state["tasks"]}
    assert "Writing your questions" in labels
    assert "Spotting what you don't know" in labels


# ---------- model profile ----------

def test_a_profile_carries_a_fingerprint_marking_unmeasured_bars_as_unmeasured(archive):
    write_run(archive, "r1", "cand-a", PASSING)
    profile = TransparencyService(archive=RunArchive(archive)).profile("cand-a")

    by_key = {f["category"]: f for f in profile["fingerprint"]}
    assert by_key["medical_qa"]["measured"] is True
    assert by_key["medical_qa"]["score"] is not None
    # Nothing in PASSING scores relationships: the bar must be absent, not zero.
    assert by_key["relationships"]["measured"] is False
    assert by_key["relationships"]["score"] is None


def test_a_profile_lists_what_the_model_is_trusted_with_today(tmp_path, archive):
    write_run(archive, "r1", "cand-a", PASSING)
    engine = AIEngine(Database(tmp_path / "s.db"))
    engine.promote("QUESTION_GENERATION", "cand-a", "r1", outcome="PASS")

    profile = TransparencyService(archive=RunArchive(archive),
                                  ai_engine=engine).profile("cand-a")
    assert [s["task_type"] for s in profile["currently_serving"]] == ["QUESTION_GENERATION"]
    assert profile["currently_serving"][0]["task_label"] == "Writing your questions"


def test_an_unknown_model_has_no_profile(archive):
    with pytest.raises(KeyError):
        TransparencyService(archive=RunArchive(archive)).profile("cand-imaginary")


# ---------- history ----------

def test_a_model_evaluated_once_is_not_drawn_as_a_trend(archive):
    write_run(archive, "r1", "cand-a", PASSING)
    history = TransparencyService(archive=RunArchive(archive)).history("cand-a")
    assert len(history["points"]) == 1
    assert history["chartable"] is False
    assert "no trend to draw yet" in history["note"]


def test_history_is_oldest_first_and_chartable_with_two_points(archive):
    write_run(archive, "r1", "cand-a", PASSING, timestamp="2026-01-01T00:00:00Z")
    write_run(archive, "r2", "cand-a", PASSING, timestamp="2026-03-01T00:00:00Z")
    history = TransparencyService(archive=RunArchive(archive)).history("cand-a")
    assert [p["run_id"] for p in history["points"]] == ["r1", "r2"]
    assert history["chartable"] is True


def test_history_warns_when_runs_span_benchmark_versions(archive):
    write_run(archive, "r1", "cand-a", PASSING, timestamp="2026-01-01T00:00:00Z", version="0.3")
    write_run(archive, "r2", "cand-a", PASSING, timestamp="2026-03-01T00:00:00Z", version="0.4")
    history = TransparencyService(archive=RunArchive(archive)).history("cand-a")
    assert "not directly comparable" in history["version_warning"]


def test_history_does_not_warn_within_one_benchmark_version(archive):
    write_run(archive, "r1", "cand-a", PASSING, timestamp="2026-01-01T00:00:00Z")
    write_run(archive, "r2", "cand-a", PASSING, timestamp="2026-03-01T00:00:00Z")
    assert TransparencyService(archive=RunArchive(archive)).history("cand-a")[
        "version_warning"] == ""


# ---------- HTTP surface ----------

@pytest.fixture()
def client(tmp_path, archive):
    write_run(archive, "r1", "cand-a", PASSING)
    db = Database(tmp_path / "s.db")
    engine = AIEngine(db)
    engine.archive = RunArchive(archive)
    api = StudentAPI(db, ai=engine)
    uid = db.create_user("learner@example.test", "a-long-enough-password")
    token = db.issue_token(uid)

    def call(method, path, params=None):
        return api.handle(method, path, params or {}, {}, token)

    return call


def test_all_transparency_routes_answer(client):
    for path in ("/ai/benchmark", "/ai/benchmark/categories", "/ai/benchmark/ranking",
                 "/ai/benchmark/powering", "/ai/models/cand-a", "/ai/models/cand-a/history"):
        status, _ = client("GET", path)
        assert status == 200, path


def test_the_screen_requires_a_logged_in_learner_but_nothing_more(tmp_path, archive):
    db = Database(tmp_path / "s.db")
    api = StudentAPI(db)
    assert api.handle("GET", "/ai/benchmark", {}, {}, None)[0] == 401
    uid = db.create_user("learner@example.test", "a-long-enough-password")
    # A plain learner, not an admin, can read it -- the point is that the
    # person being marked can check the marker.
    assert api.handle("GET", "/ai/benchmark", {}, {}, db.issue_token(uid))[0] == 200


def test_the_screen_is_read_only(client):
    assert client("POST", "/ai/benchmark")[0] == 405


def test_an_unknown_model_is_a_404_not_an_empty_page(client):
    assert client("GET", "/ai/models/cand-imaginary")[0] == 404
    assert client("GET", "/ai/models/cand-imaginary/history")[0] == 404


def test_an_unknown_category_is_a_404(client):
    assert client("GET", "/ai/benchmark/ranking", {"category": "astrology"})[0] == 404


def test_category_selection_reaches_the_service(client):
    status, body = client("GET", "/ai/benchmark/ranking", {"category": "question_validation"})
    assert status == 200
    assert body["category"] == "question_validation"
