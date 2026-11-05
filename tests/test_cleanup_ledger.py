from __future__ import annotations

from game_test_agent.contracts.models import InvestigationRequest, TestObjective as Objective


def test_cleanup_operations_remain_queryable_until_resolved(repository):
    investigation, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective=Objective(objective_id="cancel", description="取消测试"),
        ),
        "cleanup-1",
    )
    repository.record_cleanup_required(investigation.investigation_id, "act-1")
    repository.note_cleanup_attempt("act-1", "provider_unavailable")
    assert repository.pending_cleanup_actions(investigation.investigation_id) == ["act-1"]
    assert repository.operational_metrics("studio-a", "game-a")["pending_cleanup_operations"] == 1
    repository.resolve_cleanup("act-1")
    assert repository.pending_cleanup_actions(investigation.investigation_id) == []
