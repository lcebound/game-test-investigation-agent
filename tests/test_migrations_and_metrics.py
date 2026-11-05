from __future__ import annotations

from game_test_agent.contracts.models import InvestigationRequest, TestObjective as Objective


def test_schema_migrations_and_scoped_metrics(repository):
    assert repository.schema_version() == 24
    assert repository.ping()
    repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective=Objective(objective_id="smoke", description="测试"),
        ),
        "metrics-1",
    )
    repository.create_investigation(
        InvestigationRequest(
            tenant_id="other",
            project_id="other-game",
            objective=Objective(objective_id="hidden", description="隔离"),
        ),
        "metrics-2",
    )
    metrics = repository.operational_metrics("studio-a", "game-a")
    assert metrics["investigations_by_status"] == {"accepted": 1}
    assert metrics["pending_workflow_starts"] == 1
