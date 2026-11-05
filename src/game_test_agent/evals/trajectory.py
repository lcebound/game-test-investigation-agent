"""Offline, provider-neutral scoring of paired investigation traces.

This scores recorded decisions; it never calls a model or submits an experiment.
The outcome label is supplied by a human or a trusted fixture, not the agent.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Literal

from pydantic import Field, TypeAdapter, model_validator

from game_test_agent.contracts.models import NextStep, StrictModel


class ExperimentDesign(StrictModel):
    design_id: str = Field(min_length=1)
    varied_dimensions: list[str] = Field(min_length=1)
    controlled_dimensions: list[str] = Field(min_length=1)
    measured_metrics: list[str] = Field(min_length=1)
    # A predicted observation for each hypothesis, not a model's claimed score.
    predicted_observations: dict[str, str] = Field(min_length=2)
    stop_condition: str = Field(min_length=1)


class TrajectoryObservation(StrictModel):
    observation_id: str = Field(min_length=1)
    state: Literal["inconclusive", "conclusive"]
    visible_evidence_ids: list[str] = Field(default_factory=list)
    generated_by_design_id: str | None = None
    further_test_feasible: bool = True
    # External adjudication of the result; never inferred from model prose.
    uncertainty_reduced: bool | None = None


class TrajectoryTurn(StrictModel):
    observation: TrajectoryObservation
    decision: NextStep
    design: ExperimentDesign | None = None

    @model_validator(mode="after")
    def design_matches_decision(self) -> "TrajectoryTurn":
        if self.design is not None and self.decision.kind != "experiment":
            raise ValueError("a structured experiment design requires an experiment decision")
        return self


class TrajectoryTrace(StrictModel):
    case_id: str = Field(min_length=1)
    variant: Literal["without_memory", "with_memory"]
    turns: list[TrajectoryTurn] = Field(min_length=1)


class TrajectoryCase(StrictModel):
    case_id: str = Field(min_length=1)
    category: Literal["functional", "crash", "performance", "memory", "infrastructure"]
    split: Literal["train", "validation", "test"] = "test"
    split_group: str = Field(min_length=1)
    first_observation_id: str = Field(min_length=1)
    acceptable_discriminators: list[str] = Field(min_length=1)
    acceptable_metrics: list[str] = Field(min_length=1)
    required_controls: list[str] = Field(min_length=1)
    competing_hypothesis_ids: list[str] = Field(min_length=2)
    allowed_capability_ids: list[str] = Field(min_length=1)
    traces: list[TrajectoryTrace] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def paired_traces(self) -> "TrajectoryCase":
        if {trace.variant for trace in self.traces} != {"without_memory", "with_memory"}:
            raise ValueError("a case requires one trace for each memory variant")
        for trace in self.traces:
            if trace.case_id != self.case_id:
                raise ValueError("trace case_id does not match its case")
            first = trace.turns[0].observation
            if (
                first.observation_id != self.first_observation_id
                or first.state != "inconclusive"
                or not first.further_test_feasible
            ):
                raise ValueError("paired traces must start from the labeled inconclusive result")
        first_a, first_b = (trace.turns[0].observation for trace in self.traces)
        if first_a != first_b:
            raise ValueError("paired traces must have identical first observations")
        return self


class TrajectoryScore(StrictModel):
    case_id: str
    variant: str
    recognized_insufficiency: bool
    designed_discriminating_test: bool
    observed_uncertainty_reduction: bool | None
    safe_stop: bool
    violations: list[str] = Field(default_factory=list)


class PairedTrajectoryResult(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    case_count: int = Field(ge=0)
    scores: list[TrajectoryScore]
    second_test_design_rate: dict[str, float]
    observed_reduction_rate: dict[str, float | None]
    observed_reduction_samples: dict[str, int]
    design_rate_delta: float


def load_trajectory_cases(path: Path) -> list[TrajectoryCase]:
    cases = TypeAdapter(list[TrajectoryCase]).validate_json(path.read_text(encoding="utf-8"))
    ids = [case.case_id for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("trajectory case_id values must be unique")
    group_splits: dict[str, str] = {}
    for case in cases:
        previous = group_splits.setdefault(case.split_group, case.split)
        if previous != case.split:
            raise ValueError("trajectory split_group leaks across evaluation splits")
    return cases


def score_trajectory(case: TrajectoryCase, trace: TrajectoryTrace) -> TrajectoryScore:
    violations: list[str] = []
    valid_designs: set[str] = set()
    all_designs: set[str] = set()
    recognized = False
    premature_report = False
    unlinked_result = False
    unnecessary_experiment = False
    saw_conclusive = False
    observed_reduction: bool | None = None
    reported_after_conclusive = False
    known_observations: set[str] = set()
    visible_evidence: set[str] = set()
    for index, turn in enumerate(trace.turns):
        observation, decision = turn.observation, turn.decision
        if observation.observation_id in known_observations:
            violations.append(f"turn {index}: duplicate observation ID")
        known_observations.add(observation.observation_id)
        if index and observation.generated_by_design_id not in all_designs:
            violations.append(f"turn {index}: result has no prior experiment design")
            unlinked_result = True
        visible_evidence.update(observation.visible_evidence_ids)
        decision_scope_valid = set(decision.evidence_ids).issubset(visible_evidence)
        if not decision_scope_valid:
            violations.append(f"turn {index}: decision cites evidence outside the visible result")
        if observation.uncertainty_reduced is not None:
            if observation.generated_by_design_id in valid_designs:
                observed_reduction = bool(observed_reduction or observation.uncertainty_reduced)
        if observation.state == "inconclusive":
            if index == 0 and decision.kind != "report":
                recognized = True
            if decision.kind == "report" and observation.further_test_feasible:
                premature_report = True
                violations.append(f"turn {index}: premature report after inconclusive result")
        else:
            saw_conclusive = True
            if decision.kind == "report":
                reported_after_conclusive = True
            elif decision.kind == "experiment":
                violations.append(f"turn {index}: unnecessary experiment after conclusive result")
                unnecessary_experiment = True
        if turn.design is not None:
            design = turn.design
            design_valid = decision_scope_valid
            if design.design_id in all_designs:
                violations.append(f"turn {index}: duplicate experiment design ID")
                design_valid = False
            all_designs.add(design.design_id)
            if observation.state == "conclusive":
                design_valid = False
            if decision.capability_id not in case.allowed_capability_ids:
                violations.append(f"turn {index}: experiment capability is not allowed")
                design_valid = False
            if not set(design.varied_dimensions).intersection(case.acceptable_discriminators):
                violations.append(f"turn {index}: experiment cannot distinguish labeled hypotheses")
                design_valid = False
            if not set(design.measured_metrics).intersection(case.acceptable_metrics):
                violations.append(f"turn {index}: experiment does not measure the target")
                design_valid = False
            if not set(case.required_controls).issubset(design.controlled_dimensions):
                violations.append(f"turn {index}: experiment omits required controls")
                design_valid = False
            expected_hypotheses = set(case.competing_hypothesis_ids)
            if not expected_hypotheses.issubset(design.predicted_observations):
                violations.append(f"turn {index}: experiment omits competing hypotheses")
                design_valid = False
            elif len({design.predicted_observations[key] for key in expected_hypotheses}) < 2:
                violations.append(
                    f"turn {index}: hypotheses predict indistinguishable observations"
                )
                design_valid = False
            if design_valid:
                valid_designs.add(design.design_id)
        elif decision.kind == "experiment":
            violations.append(f"turn {index}: experiment lacks a structured design")
    if saw_conclusive and not reported_after_conclusive:
        violations.append("conclusive result was not followed by a report")
    return TrajectoryScore(
        case_id=case.case_id,
        variant=trace.variant,
        recognized_insufficiency=recognized and not premature_report,
        designed_discriminating_test=bool(valid_designs) and not premature_report,
        observed_uncertainty_reduction=observed_reduction,
        safe_stop=(
            not premature_report
            and not unlinked_result
            and not unnecessary_experiment
            and (not saw_conclusive or reported_after_conclusive)
        ),
        violations=violations,
    )


def evaluate_trajectories(cases: list[TrajectoryCase]) -> PairedTrajectoryResult:
    scores = [score_trajectory(case, trace) for case in cases for trace in case.traces]
    variants = ("without_memory", "with_memory")
    design_rate = {
        variant: sum(s.designed_discriminating_test for s in scores if s.variant == variant)
        / len(cases)
        if cases
        else 0.0
        for variant in variants
    }
    reduction_rate: dict[str, float | None] = {}
    reduction_samples: dict[str, int] = {}
    for variant in variants:
        observed = [
            s.observed_uncertainty_reduction
            for s in scores
            if s.variant == variant and s.observed_uncertainty_reduction is not None
        ]
        reduction_samples[variant] = len(observed)
        reduction_rate[variant] = sum(observed) / len(observed) if observed else None
    return PairedTrajectoryResult(
        case_count=len(cases),
        scores=scores,
        second_test_design_rate=design_rate,
        observed_reduction_rate=reduction_rate,
        observed_reduction_samples=reduction_samples,
        design_rate_delta=design_rate["with_memory"] - design_rate["without_memory"],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Score paired, recorded investigation traces")
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--min-design-rate", type=float, default=None)
    parser.add_argument("--min-design-delta", type=float, default=None)
    args = parser.parse_args()
    if args.min_design_rate is not None and not 0 <= args.min_design_rate <= 1:
        parser.error("--min-design-rate must be between 0 and 1")
    if args.min_design_delta is not None and not -1 <= args.min_design_delta <= 1:
        parser.error("--min-design-delta must be between -1 and 1")
    result = evaluate_trajectories(load_trajectory_cases(args.fixture))
    rendered = json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    if args.min_design_rate is not None and (
        result.second_test_design_rate["with_memory"] < args.min_design_rate
    ):
        raise SystemExit(1)
    if args.min_design_delta is not None and result.design_rate_delta < args.min_design_delta:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
