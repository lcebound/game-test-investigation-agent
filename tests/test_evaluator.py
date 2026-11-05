from __future__ import annotations

from game_test_agent.contracts.models import (
    ConclusionLevel,
    Coverage,
    InvestigationReport,
    InvestigationRequest,
    Resolution,
    TestObjective as Objective,
)
from game_test_agent.reports import ConclusionEvaluator


def test_evaluator_requires_primary_objective_coverage(repository):
    investigation, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective=Objective(objective_id="perf", description="调查性能"),
        ),
        "req-eval",
    )
    report = InvestigationReport(
        investigation_id=investigation.investigation_id,
        revision=1,
        status="completed",
        resolution=Resolution.INCONCLUSIVE,
        summary="证据不足",
        conclusion_level=ConclusionLevel.UNKNOWN,
        coverage=Coverage(),
        stop_reason="required_capability_unavailable",
    )
    result = ConclusionEvaluator(repository).evaluate(report)
    assert not result.accepted
    assert "primary objective" in result.blocking_issues[0]
