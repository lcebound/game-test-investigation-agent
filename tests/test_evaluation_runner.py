from __future__ import annotations

from pathlib import Path

from game_test_agent.evals.runner import EvaluationRunner, load_cases


def test_baseline_evaluation_suite_passes() -> None:
    fixture = Path(__file__).parents[1] / "evals" / "baseline.json"
    cases = load_cases(fixture)
    result = EvaluationRunner().run(cases)

    assert result.passed
    assert result.metrics.total_cases == 8
    assert result.metrics.case_pass_rate == 1
    assert result.metrics.acceptance_accuracy == 1
    assert result.metrics.unsafe_case_rejection_rate == 1


def test_evaluation_fixture_has_independent_split_groups() -> None:
    fixture = Path(__file__).parents[1] / "evals" / "baseline.json"
    cases = load_cases(fixture)

    assert all(case.split == "test" for case in cases)
    assert len({case.split_group for case in cases}) == len(cases)
