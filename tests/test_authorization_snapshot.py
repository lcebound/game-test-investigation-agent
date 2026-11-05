from __future__ import annotations

from game_test_agent.contracts.models import InvestigationRequest, TestObjective as Objective


def test_intake_freezes_principal_scopes_and_policy(repository):
    investigation, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective=Objective(objective_id="smoke", description="测试"),
        ),
        "same-client-key",
        principal_id="user-1",
        scopes={"runs:submit", "evidence:read"},
        policy_profile="bounded-diagnosis",
    )
    authorization = repository.get_authorization(investigation.investigation_id)
    assert authorization["principal_id"] == "user-1"
    assert set(authorization["scopes"]) == {"runs:submit", "evidence:read"}


def test_idempotency_key_is_scoped_to_principal(repository):
    request = InvestigationRequest(
        tenant_id="studio-a",
        project_id="game-a",
        objective=Objective(objective_id="smoke", description="测试"),
    )
    first, _ = repository.create_investigation(request, "same-key", principal_id="user-1")
    second, _ = repository.create_investigation(request, "same-key", principal_id="user-2")
    assert first.investigation_id != second.investigation_id
