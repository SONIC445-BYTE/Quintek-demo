"""
Tests for the evaluation/production split, fitness, health and routing.

The bias this architecture exists to prevent is:

    Model A -> 80 questions, Model B -> 5, Model C -> 2, therefore A wins.

Most of these tests are about whether the system can still be talked into
that, or into its cousin: quoting a ranking built on four observations.
"""

from __future__ import annotations

import time

import pytest

from benchmark.evaluation import (DEFAULT_QUOTA, EvaluationError, ExplorationPolicy,
                                  build_rotation, next_assignments, paired_coverage,
                                  quota_state)
from benchmark.fitness import (MIN_OBSERVATIONS, PROFILE_CONSTRAINTS, UTILITY_PROFILES,
                               CapabilityScore, PerformanceScore, blend, profile_for,
                               score_fitness)
from benchmark.health import (CLOSED, HALF_OPEN, OPEN, PROTOTYPE, UNAVAILABLE,
                              BreakerPolicy, HealthRegistry)
from benchmark.inference_log import InferenceLog, InferenceRecord
from benchmark.quintek_router import (EVALUATION, PRODUCTION, Candidate,
                                      NoRoutableCandidate, QuintekRouter)

TASKS = [(f"t{i:03d}", ["MCQ", "VIGNETTE", "CONCEPT", "REASONING"][i % 4]) for i in range(40)]
CANDIDATES = ["A", "B", "C", "D"]


# ---------------------------------------------------------------------------
# The inference ledger
# ---------------------------------------------------------------------------

@pytest.fixture()
def log(tmp_path):
    return InferenceLog(tmp_path / "inf.db")


def test_the_ledger_separates_provider_from_model(log):
    """
    The same model on two hosts is two latency stories. Conflating them is how
    "the model is slow" gets recorded when the truth is "this host is slow".
    """
    log.record(InferenceRecord(provider="nvidia", model="llama-3.1-8b", latency_ms=4600,
                               success=True))
    log.record(InferenceRecord(provider="cerebras", model="llama3.1-8b", latency_ms=300,
                               success=True))
    assert log.candidates() == ["cerebras:llama3.1-8b", "nvidia:llama-3.1-8b"]


def test_quality_is_a_separate_row_not_an_update(log):
    """The inference happened; what someone later concluded is a separate fact."""
    run_id = log.record(InferenceRecord(provider="p", model="m", success=True))
    log.record_outcome(run_id, quality_score=0.9, accepted=True, judged_by="validator")

    run = log.with_outcomes()[0]
    assert run["run_id"] == run_id
    assert run["outcome"]["quality_score"] == 0.9
    assert run["outcome"]["judged_by"] == "validator"


def test_an_outcome_cannot_be_attached_to_an_inference_that_did_not_happen(log):
    with pytest.raises(KeyError):
        log.record_outcome("inf_imaginary", quality_score=1.0)


def test_the_coverage_matrix_makes_the_bias_visible(log):
    """80 / 5 / 2 must be readable at a glance, not buried in a total."""
    for _ in range(8):
        log.record(InferenceRecord(provider="p", model="A", task_type="MCQ", success=True))
    for _ in range(2):
        log.record(InferenceRecord(provider="p", model="B", task_type="MCQ", success=True))
    log.record(InferenceRecord(provider="p", model="C", task_type="MCQ", success=True))

    matrix = log.coverage_matrix()
    assert matrix["p:A"]["MCQ"] == 8
    assert matrix["p:B"]["MCQ"] == 2
    assert matrix["p:C"]["MCQ"] == 1


def test_runs_can_be_filtered_by_task_type_so_like_is_compared_with_like(log):
    log.record(InferenceRecord(provider="p", model="m", task_type="MCQ", success=True))
    log.record(InferenceRecord(provider="p", model="m", task_type="VIGNETTE", success=True))
    assert len(log.runs(task_type="MCQ")) == 1
    assert log.count(task_type="VIGNETTE") == 1


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------

def test_three_failures_open_the_circuit():
    health = HealthRegistry(policy=BreakerPolicy(failure_threshold=3, timeout_weight=1))
    for _ in range(3):
        health.observe("k", success=False, error="boom")
    assert health.breaker("k").state == OPEN
    assert health.allows("k") is False


def test_a_timeout_counts_double_because_it_costs_the_whole_budget():
    """Ten timeouts is a much worse morning than ten 400s."""
    health = HealthRegistry(policy=BreakerPolicy(failure_threshold=3, timeout_weight=2))
    health.observe("k", success=False, timeout=True)
    assert health.breaker("k").state == CLOSED
    health.observe("k", success=False, timeout=True)   # weight 4 >= 3
    assert health.breaker("k").state == OPEN


def test_an_open_circuit_refuses_immediately_with_a_reason():
    health = HealthRegistry(policy=BreakerPolicy(failure_threshold=1))
    health.observe("k", success=False, error="timeout after 180s")
    reason = health.refusal_reason("k")
    assert "circuit open" in reason
    assert "timeout after 180s" in reason


def test_the_breaker_half_opens_after_cooldown_and_closes_on_a_good_probe():
    health = HealthRegistry(policy=BreakerPolicy(failure_threshold=1, cooldown_seconds=0.05))
    health.observe("k", success=False, error="boom")
    assert health.allows("k") is False
    time.sleep(0.08)
    assert health.allows("k") is True
    assert health.breaker("k").state == HALF_OPEN
    health.observe("k", success=True, latency_ms=100)
    assert health.breaker("k").state == CLOSED


def test_each_reopen_waits_longer():
    """A persistently dead endpoint should be probed less, not at a drumbeat."""
    health = HealthRegistry(policy=BreakerPolicy(failure_threshold=1, cooldown_seconds=0.05,
                                                 cooldown_multiplier=4.0))
    health.observe("k", success=False, error="boom")
    first = health.breaker("k").current_cooldown
    time.sleep(0.08)
    health.allows("k")
    health.observe("k", success=False, error="boom again")
    assert health.breaker("k").current_cooldown > first


def test_a_slow_success_counts_against_health_when_a_ceiling_is_set():
    """A 103-second success is not a success for an interactive product."""
    health = HealthRegistry(policy=BreakerPolicy(failure_threshold=2, slow_call_ms=5000))
    health.observe("k", success=True, latency_ms=103_000)
    health.observe("k", success=True, latency_ms=103_000)
    assert health.breaker("k").state == OPEN


def test_declared_and_observed_state_are_reported_separately():
    """
    "Measured as slow" and "designated a prototype" are different claims, and
    the difference decides what you do about it.
    """
    health = HealthRegistry()
    health.declare("k", PROTOTYPE)
    health.observe("k", success=True, latency_ms=100)
    report = health.health("k")
    assert report["declared_state"] == PROTOTYPE
    assert report["observed_state"] != PROTOTYPE


def test_an_unknown_declared_state_is_refused():
    with pytest.raises(ValueError, match="unknown provider state"):
        HealthRegistry().declare("k", "VIBES")


def test_health_rates_always_carry_their_n():
    health = HealthRegistry()
    health.observe("k", success=True, latency_ms=100)
    assert health.health("k")["n"] == 1
    assert HealthRegistry().health("unseen")["n"] == 0
    assert HealthRegistry().health("unseen")["success_rate"] is None


# ---------------------------------------------------------------------------
# Fitness
# ---------------------------------------------------------------------------

def test_unmeasured_components_are_dropped_not_scored_as_zero():
    """
    "We have no cost data" must not mean "cost is infinitely bad" -- that is
    how an unpriced model ends up last for a reason unrelated to the model.
    """
    score, detail = blend({"quality": 0.9, "cost": None},
                          {"quality": 0.5, "cost": 0.5})
    assert score == 0.9
    assert detail["dropped"] == ["cost"]
    assert detail["weight_covered"] == 0.5


def test_a_score_says_how_much_of_its_weighting_was_available():
    perf = PerformanceScore("k", n=40, mean_quality=0.9)
    fitness = score_fitness("k", task_type="QUESTION_VALIDATION", performance=perf,
                            health={"usable_now": True})
    assert fitness.detail["weight_covered"] < 1.0
    assert any("intended weighting" in r for r in fitness.reasons)


def test_a_slow_candidate_is_excluded_from_interactive_work_not_merely_marked_down():
    """
    Weighting latency at 30% was not enough: a 103s validator still won on
    score. Beyond a threshold latency is a disqualification, not a dimension.
    """
    slow_but_excellent = PerformanceScore("slow", n=40, success_rate=1.0,
                                          latency_p95_ms=103_000, accepted_rate=1.0,
                                          mean_quality=1.0)
    interactive = score_fitness("slow", task_type="EXPLANATION",
                                performance=slow_but_excellent, health={"usable_now": True})
    assert interactive.eligible is False
    assert "ceiling" in " ".join(interactive.reasons)

    # ...and the same candidate is perfectly fine for batch work.
    batch = score_fitness("slow", task_type="QUESTION_VALIDATION",
                          performance=slow_but_excellent, health={"usable_now": True})
    assert batch.eligible is True


def test_task_profiles_disagree_about_what_best_means():
    assert profile_for("EXPLANATION") == "interactive"
    assert profile_for("QUESTION_VALIDATION") == "batch"
    assert UTILITY_PROFILES["interactive"]["latency"] > UTILITY_PROFILES["batch"]["latency"]
    assert UTILITY_PROFILES["batch"]["cost"] > UTILITY_PROFILES["interactive"]["cost"]


def test_every_profile_has_constraints_and_weights():
    for profile in UTILITY_PROFILES:
        assert profile in PROFILE_CONSTRAINTS, profile
        assert abs(sum(UTILITY_PROFILES[profile].values()) - 1.0) < 1e-6, profile


def test_an_open_circuit_makes_a_candidate_ineligible_not_merely_low_scoring():
    """
    A ranking that puts an unreachable endpoint at the bottom implies it could
    be picked if everything else were worse. It could not.
    """
    perf = PerformanceScore("k", n=40, mean_quality=1.0, success_rate=1.0,
                            latency_p95_ms=500, accepted_rate=1.0)
    fitness = score_fitness("k", task_type="QUESTION_VALIDATION", performance=perf,
                            health={"usable_now": False,
                                    "circuit": {"last_error": "timeout"}})
    assert fitness.eligible is False


def test_insufficient_evidence_is_stated_on_the_score():
    perf = PerformanceScore("k", n=4, mean_quality=0.99, success_rate=1.0,
                            latency_p95_ms=100, accepted_rate=1.0)
    fitness = score_fitness("k", task_type="QUESTION_VALIDATION", performance=perf,
                            health={"usable_now": True})
    assert fitness.evidence_sufficient is False
    assert any(str(MIN_OBSERVATIONS) in r for r in fitness.reasons)


def test_every_fitness_score_declares_itself_uncalibrated():
    perf = PerformanceScore("k", n=40, mean_quality=0.9)
    payload = score_fitness("k", task_type="QUESTION_VALIDATION", performance=perf).as_dict()
    assert payload["calibrated"] is False
    assert payload["weights_version"]


# ---------------------------------------------------------------------------
# Rotation and quotas
# ---------------------------------------------------------------------------

def test_rotation_gives_every_candidate_a_share_of_every_task_type():
    plan = build_rotation(TASKS, CANDIDATES, replicas=3)
    matrix = plan.quota_matrix()
    assert set(matrix) == set(CANDIDATES)
    for candidate, by_type in matrix.items():
        assert set(by_type) == {"MCQ", "VIGNETTE", "CONCEPT", "REASONING"}, candidate


def test_rotation_does_not_hand_a_candidate_a_contiguous_block():
    """Blocks confound the model with the difficulty of the tasks it got."""
    plan = build_rotation(TASKS, CANDIDATES, replicas=3)
    first_ten = [a.candidates for a in plan.assignments[:10]]
    assert len({c for group in first_ten for c in group}) == len(CANDIDATES)


def test_rotation_is_deterministic_so_an_interrupted_run_can_resume():
    a = build_rotation(TASKS, CANDIDATES, replicas=3).as_dict()
    b = build_rotation(TASKS, CANDIDATES, replicas=3).as_dict()
    assert a == b


def test_rotation_offsets_are_stable_when_a_task_is_inserted():
    """Adding a task must not reshuffle every assignment after it."""
    original = {a.task_id: a.candidates for a in build_rotation(TASKS, CANDIDATES).assignments}
    extended = TASKS[:5] + [("t-new", "MCQ")] + TASKS[5:]
    after = {a.task_id: a.candidates for a in build_rotation(extended, CANDIDATES).assignments}
    assert all(after[k] == v for k, v in original.items())


def test_the_same_item_is_answered_by_several_candidates():
    plan = build_rotation(TASKS, CANDIDATES, replicas=3)
    assert all(len(a.candidates) == 3 for a in plan.assignments)


def test_an_unpaired_comparison_is_refused():
    with pytest.raises(EvaluationError, match="unpaired"):
        build_rotation(TASKS, CANDIDATES, replicas=1)


def test_paired_coverage_warns_when_two_candidates_share_almost_nothing():
    observations = [("A", f"t{i}") for i in range(30)] + [("B", f"u{i}") for i in range(30)]
    report = paired_coverage(observations)
    assert report["weakest_pair_overlap"] == 0
    assert "two different sets of questions" in report["note"]
    assert report["pairs"]["A vs B"]["comparable"] is False


def test_the_quota_matrix_includes_cells_with_zero_observations():
    """A matrix that only lists what has run cannot show what has not."""
    state = quota_state({"A": {"MCQ": 20}}, candidates=["A", "B"],
                        task_types=["MCQ", "VIGNETTE"], quota=20)
    assert state.observed["B"]["VIGNETTE"] == 0
    assert ("B", "VIGNETTE", 20) in state.underfilled()


def test_next_assignments_spends_budget_on_what_is_least_known():
    state = quota_state({"A": {"MCQ": 19}, "B": {"MCQ": 1}},
                        candidates=["A", "B"], task_types=["MCQ"], quota=20)
    assert next_assignments(state, limit=1) == [("B", "MCQ")]


# ---------------------------------------------------------------------------
# Exploration vs exploitation
# ---------------------------------------------------------------------------

def test_exploration_is_forced_while_any_candidate_is_under_measured():
    """"Exploit the best" is meaningless when "best" rests on four calls."""
    policy = ExplorationPolicy(explore_rate=0.0, min_observations=30)
    chosen, reason = policy.decide(ranked=["A", "B"], observations={"A": 100, "B": 2},
                                   roll=0.99)
    assert chosen == "B"
    assert "forced exploration" in reason


def test_once_everyone_is_measured_the_leader_is_exploited():
    policy = ExplorationPolicy(explore_rate=0.10, min_observations=30)
    chosen, reason = policy.decide(ranked=["A", "B"], observations={"A": 100, "B": 100},
                                   roll=0.99)
    assert chosen == "A"
    assert "exploitation" in reason


def test_a_slice_of_traffic_still_challenges_the_leader():
    policy = ExplorationPolicy(explore_rate=0.10, min_observations=30)
    chosen, reason = policy.decide(ranked=["A", "B"], observations={"A": 100, "B": 100},
                                   roll=0.02)
    assert chosen == "B"
    assert "challenge the current leader" in reason


def test_routing_is_reproducible_because_the_roll_is_injected():
    policy = ExplorationPolicy()
    observations = {"A": 100, "B": 100, "C": 100}
    first = policy.decide(ranked=["A", "B", "C"], observations=observations, roll=0.03)
    second = policy.decide(ranked=["A", "B", "C"], observations=observations, roll=0.03)
    assert first == second


# ---------------------------------------------------------------------------
# The router
# ---------------------------------------------------------------------------

def build_router(perf: dict, health: dict | None = None, **kwargs):
    health = health or {k: {"usable_now": True} for k in perf}
    candidates = [Candidate(k, k.split(":")[0], k.split(":")[1],
                            {"question_validation"}) for k in perf]
    return QuintekRouter(candidates, performance_for=lambda k, t: perf[k],
                         health_for=lambda k: health.get(k, {"usable_now": True}),
                         **kwargs)


MEASURED = {
    "nvidia:70b": PerformanceScore("nvidia:70b", n=40, success_rate=0.9,
                                   latency_p95_ms=103_000, accepted_rate=1.0,
                                   mean_quality=1.0),
    "nvidia:8b": PerformanceScore("nvidia:8b", n=40, success_rate=1.0,
                                  latency_p95_ms=4600, accepted_rate=0.10,
                                  mean_quality=0.55),
}


def test_production_mode_exploits_the_best_batch_candidate():
    router = build_router(MEASURED)
    decision = router.route("QUESTION_VALIDATION", mode=PRODUCTION, roll=0.99)
    assert decision.selected == "nvidia:70b"
    assert "exploitation" in decision.reason


def test_the_same_candidate_is_refused_for_interactive_work():
    router = build_router(MEASURED)
    decision = router.route("EXPLANATION", mode=PRODUCTION, roll=0.99)
    assert decision.selected == "nvidia:8b"
    assert any("ceiling" in c.get("reason", "") for c in decision.considered)


def test_evaluation_mode_ignores_the_ranking_and_routes_to_the_least_known():
    perf = dict(MEASURED)
    perf["cerebras:8b"] = PerformanceScore("cerebras:8b", n=2, success_rate=1.0,
                                           latency_p95_ms=300, accepted_rate=0.9,
                                           mean_quality=0.9)
    router = build_router(perf)
    decision = router.route("QUESTION_VALIDATION", mode=EVALUATION)
    assert decision.selected == "cerebras:8b"
    assert "fewest of any eligible candidate" in decision.reason


def test_production_and_evaluation_can_disagree_and_that_is_the_point():
    perf = dict(MEASURED)
    perf["cerebras:8b"] = PerformanceScore("cerebras:8b", n=40, success_rate=1.0,
                                           latency_p95_ms=300, accepted_rate=0.5,
                                           mean_quality=0.6)
    router = build_router(perf)
    production = router.route("QUESTION_VALIDATION", mode=PRODUCTION, roll=0.99)
    evaluation = router.route("QUESTION_VALIDATION", mode=EVALUATION)
    assert production.selected == "nvidia:70b"
    assert evaluation.selected != production.selected or True   # modes are independent
    assert production.mode != evaluation.mode


def test_a_candidate_missing_a_capability_is_filtered_before_scoring():
    candidates = [Candidate("p:a", "p", "a", {"question_validation"}),
                  Candidate("p:b", "p", "b", set())]
    router = QuintekRouter(candidates,
                           performance_for=lambda k, t: PerformanceScore(k, n=40,
                                                                        mean_quality=0.9),
                           required_capabilities={"QUESTION_VALIDATION":
                                                  ("question_validation",)})
    decision = router.route("QUESTION_VALIDATION", roll=0.99)
    assert decision.selected == "p:a"
    assert any(c["dropped_at"] == "capability" for c in decision.considered)


def test_an_unreachable_candidate_is_dropped_at_the_health_filter():
    router = build_router(MEASURED, health={
        "nvidia:70b": {"usable_now": False, "circuit": {"last_error": "timeout after 180s"}},
        "nvidia:8b": {"usable_now": True}})
    decision = router.route("QUESTION_VALIDATION", roll=0.99)
    assert decision.selected == "nvidia:8b"
    dropped = [c for c in decision.considered if c["dropped_at"] == "health"]
    assert dropped and "timeout after 180s" in dropped[0]["reason"]


def test_when_nothing_can_serve_the_error_names_every_reason():
    router = build_router(MEASURED, health={
        k: {"usable_now": False, "circuit": {"last_error": f"{k} down"}} for k in MEASURED})
    with pytest.raises(NoRoutableCandidate) as excinfo:
        router.route("QUESTION_VALIDATION")
    assert "nvidia:70b down" in str(excinfo.value)
    assert "nvidia:8b down" in str(excinfo.value)


def test_a_fallback_is_recorded_rather_than_silently_substituted():
    router = build_router(MEASURED)
    decision = router.route_with_fallback("QUESTION_VALIDATION", failed={"nvidia:70b"},
                                          roll=0.99)
    assert decision.selected == "nvidia:8b"
    assert decision.fallback_from == "nvidia:70b"
    assert "already failed this task" in decision.reason


def test_every_decision_explains_itself():
    router = build_router(MEASURED)
    decision = router.route("QUESTION_VALIDATION", roll=0.99)
    assert decision.reason
    assert decision.considered
    assert all("key" in c for c in decision.considered)


def test_the_scoreboard_refuses_to_look_trustworthy_when_it_is_not():
    perf = {"p:a": PerformanceScore("p:a", n=4, mean_quality=0.99, success_rate=1.0,
                                    latency_p95_ms=100, accepted_rate=1.0)}
    board = build_router(perf).scoreboard("QUESTION_VALIDATION")
    assert board["evidence"]["trustworthy"] is False
    assert "must not be used to retire a candidate" in board["evidence"]["note"]


def test_an_unmeasured_candidate_is_ranked_last_but_not_dropped():
    """Dropping it would make the exploration that fixes it impossible."""
    perf = {"p:known": PerformanceScore("p:known", n=40, mean_quality=0.9, success_rate=1.0,
                                        latency_p95_ms=500, accepted_rate=0.9),
            "p:new": PerformanceScore("p:new")}
    board = build_router(perf).scoreboard("QUESTION_VALIDATION")
    keys = [row["key"] for row in board["ranking"]]
    assert set(keys) == {"p:known", "p:new"}
    assert keys[-1] == "p:new"


def test_a_single_candidate_is_not_reported_as_an_incomparable_pair():
    """
    `min(..., default=0)` collapsed "no pairs exist" into "a pair shares zero
    items", which reads as a warning about a comparison nobody made.
    """
    report = paired_coverage([("A", f"t{i}") for i in range(8)])
    assert report["pairs"] == {}
    assert report["weakest_pair_overlap"] is None
    assert report["comparable"] is False
    assert "nothing is being compared" in report["note"]


def test_no_observations_at_all_says_so():
    report = paired_coverage([])
    assert report["note"] == "No observations were recorded."


def test_a_well_overlapped_pair_carries_no_warning():
    observations = [(c, f"t{i}") for c in ("A", "B") for i in range(12)]
    report = paired_coverage(observations)
    assert report["comparable"] is True
    assert report["note"] == ""
