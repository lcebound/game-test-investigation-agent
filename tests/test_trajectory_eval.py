from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from game_test_agent.contracts.models import NextStep
from game_test_agent.evals.trajectory import (
    ExperimentDesign,
    TrajectoryCase,
    TrajectoryObservation,
    TrajectoryTrace,
    TrajectoryTurn,
    evaluate_trajectories,
    load_trajectory_cases,
)


def _decision(kind: str, *, capability_id: str | None = None) -> NextStep:
    return NextStep(
        kind=kind,
        objective_id="frame-stability",
        capability_id=capability_id,
        provider_id="platform-adapter" if capability_id else None,
        expected_observation="区分是否仅在高负载场景出现长帧",
        reason_summary="第一轮只给出单次异常，尚不足以判定根因",
    )


def _design(**updates) -> ExperimentDesign:
    values = {
        "design_id": "controlled-retest",
        "varied_dimensions": ["scene_load"],
        "controlled_dimensions": ["build", "device", "graphics_settings"],
        "measured_metrics": ["frame_time_p95"],
        "predicted_observations": {
            "content_bottleneck": "high-only regression",
            "device_noise": "both arms similar",
        },
        "stop_condition": "each arm has two independent runs",
    }
    values.update(updates)
    return ExperimentDesign(**values)


def _first() -> TrajectoryObservation:
    return TrajectoryObservation(
        observation_id="first-result",
        state="inconclusive",
        visible_evidence_ids=["first-run"],
    )


def _case(with_memory: TrajectoryTrace, without_memory: TrajectoryTrace) -> TrajectoryCase:
    return TrajectoryCase(
        case_id="single-long-frame",
        category="performance",
        split_group="project-a-build-17",
        first_observation_id="first-result",
        acceptable_discriminators=["scene_load"],
        acceptable_metrics=["frame_time_p95"],
        required_controls=["build", "device"],
        competing_hypothesis_ids=["content_bottleneck", "device_noise"],
        allowed_capability_ids=["run-frame-probe"],
        traces=[without_memory, with_memory],
    )


def test_paired_trajectory_separates_design_from_observed_benefit() -> None:
    without = TrajectoryTrace(
        case_id="single-long-frame",
        variant="without_memory",
        turns=[TrajectoryTurn(observation=_first(), decision=_decision("report"))],
    )
    with_memory = TrajectoryTrace(
        case_id="single-long-frame",
        variant="with_memory",
        turns=[
            TrajectoryTurn(
                observation=_first(),
                decision=_decision("experiment", capability_id="run-frame-probe"),
                design=_design(),
            ),
            TrajectoryTurn(
                observation=TrajectoryObservation(
                    observation_id="second-result",
                    state="conclusive",
                    visible_evidence_ids=["second-run"],
                    generated_by_design_id="controlled-retest",
                    uncertainty_reduced=True,
                ),
                decision=_decision("report"),
            ),
        ],
    )
    result = evaluate_trajectories([_case(with_memory, without)])
    assert result.design_rate_delta == 1
    assert result.second_test_design_rate == {"without_memory": 0, "with_memory": 1}
    assert result.observed_reduction_rate == {"without_memory": None, "with_memory": 1}
    assert result.scores[1].safe_stop
    assert not result.scores[0].safe_stop


def test_structured_design_cannot_claim_value_from_prose_alone() -> None:
    first = _first()
    bad = TrajectoryTrace(
        case_id="single-long-frame",
        variant="with_memory",
        turns=[
            TrajectoryTurn(
                observation=first,
                decision=_decision("experiment", capability_id="run-frame-probe"),
                design=_design(
                    varied_dimensions=["unrelated_setting"],
                    predicted_observations={
                        "content_bottleneck": "same",
                        "device_noise": "same",
                    },
                ),
            )
        ],
    )
    baseline = bad.model_copy(update={"variant": "without_memory"})
    score = evaluate_trajectories([_case(bad, baseline)]).scores[1]
    assert score.recognized_insufficiency
    assert not score.designed_discriminating_test
    assert any("cannot distinguish" in issue for issue in score.violations)
    assert any("indistinguishable" in issue for issue in score.violations)


def test_missing_experiment_design_is_scored_as_failure_not_dropped() -> None:
    no_design = TrajectoryTrace(
        case_id="single-long-frame",
        variant="with_memory",
        turns=[
            TrajectoryTurn(
                observation=_first(),
                decision=_decision("experiment", capability_id="run-frame-probe"),
            )
        ],
    )
    baseline = no_design.model_copy(update={"variant": "without_memory"})
    score = evaluate_trajectories([_case(no_design, baseline)]).scores[1]
    assert score.recognized_insufficiency
    assert not score.designed_discriminating_test
    assert any("lacks a structured design" in issue for issue in score.violations)


def test_paired_trace_rejects_different_first_result_and_unlinked_second_result() -> None:
    base = TrajectoryTrace(
        case_id="single-long-frame",
        variant="without_memory",
        turns=[TrajectoryTurn(observation=_first(), decision=_decision("report"))],
    )
    different = TrajectoryTrace(
        case_id="single-long-frame",
        variant="with_memory",
        turns=[
            TrajectoryTurn(
                observation=_first().model_copy(update={"visible_evidence_ids": ["other"]}),
                decision=_decision("report"),
            )
        ],
    )
    with pytest.raises(ValidationError, match="identical first observations"):
        _case(different, base)

    unlinked = TrajectoryTrace(
        case_id="single-long-frame",
        variant="with_memory",
        turns=[
            TrajectoryTurn(observation=_first(), decision=_decision("query")),
            TrajectoryTurn(
                observation=TrajectoryObservation(
                    observation_id="second-result",
                    state="conclusive",
                    uncertainty_reduced=True,
                ),
                decision=_decision("report"),
            ),
        ],
    )
    score = evaluate_trajectories([_case(unlinked, base)]).scores[1]
    assert any("no prior experiment design" in issue for issue in score.violations)
    assert score.observed_uncertainty_reduction is None


def test_second_result_can_reduce_uncertainty_without_becoming_conclusive() -> None:
    baseline = TrajectoryTrace(
        case_id="single-long-frame",
        variant="without_memory",
        turns=[TrajectoryTurn(observation=_first(), decision=_decision("report"))],
    )
    improved = TrajectoryTrace(
        case_id="single-long-frame",
        variant="with_memory",
        turns=[
            TrajectoryTurn(
                observation=_first(),
                decision=_decision("experiment", capability_id="run-frame-probe"),
                design=_design(),
            ),
            TrajectoryTurn(
                observation=TrajectoryObservation(
                    observation_id="partial-second-result",
                    state="inconclusive",
                    generated_by_design_id="controlled-retest",
                    further_test_feasible=False,
                    uncertainty_reduced=True,
                ),
                decision=_decision("report"),
            ),
        ],
    )
    score = evaluate_trajectories([_case(improved, baseline)]).scores[1]
    assert score.observed_uncertainty_reduction is True
    assert score.safe_stop


def test_example_fixture_is_format_only_not_test_split() -> None:
    path = Path(__file__).parents[1] / "evals" / "trajectory-example.json"
    cases = load_trajectory_cases(path)
    assert len(cases) == 1
    assert cases[0].split == "train"
    assert evaluate_trajectories(cases).design_rate_delta == 1
