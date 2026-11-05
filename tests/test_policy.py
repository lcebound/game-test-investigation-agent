from __future__ import annotations

import pytest

from game_test_agent.capabilities import CapabilityRegistry
from game_test_agent.contracts.models import (
    ActionSpec,
    Budget,
    CapabilityDescriptor,
    InvestigationRequest,
    TestObjective as Objective,
)
from game_test_agent.policies import PolicyEngine
from game_test_agent.storage import ConflictError


def capability() -> CapabilityDescriptor:
    return CapabilityDescriptor.model_validate(
        {
            "capability_id": "test.run",
            "capability_version": "1.0.0",
            "provider_id": "lab-a",
            "provider_revision": "catalog-1",
            "tool_name": "gt_runs_submit",
            "project_ids": ["game-a"],
            "input_schema": {
                "type": "object",
                "properties": {"capture_profile": {"type": "string"}},
                "required": ["capture_profile"],
                "additionalProperties": False,
            },
            "risk_level": "bounded_execution",
            "required_scopes": ["runs:submit"],
            "execution_owner": "lab-a",
            "resource_domain": "device-pool-a",
            "estimated_cost": {"device_minutes": 5},
        }
    )


def make_action(
    investigation_id: str, action_id: str = "act-1", key: str = "inv/act-1"
) -> ActionSpec:
    return ActionSpec(
        tenant_id="studio-a",
        project_id="game-a",
        investigation_id=investigation_id,
        action_id=action_id,
        idempotency_key=key,
        capability_id="test.run",
        capability_version="1.0.0",
        provider_id="lab-a",
        parameters={"capture_profile": "frame_csv"},
        expected_observation="获得帧耗时 CSV",
        plan_revision=1,
    )


def test_policy_reserves_budget_and_replays_idempotently(repository):
    request = InvestigationRequest(
        tenant_id="studio-a",
        project_id="game-a",
        objective=Objective(objective_id="perf", description="验证帧耗时"),
    )
    investigation, _ = repository.create_investigation(request, "req-1")
    registry = CapabilityRegistry(repository)
    registry.register(capability())
    policy = PolicyEngine(repository, registry)
    action = make_action(investigation.investigation_id)
    first = policy.reserve(action, principal_id="user-1", scopes={"runs:submit"})
    second = policy.reserve(action, principal_id="user-1", scopes={"runs:submit"})
    assert first.created is True
    assert second.created is False
    assert first.permit.permit_id == second.permit.permit_id


def test_unknown_action_parameter_is_rejected(repository):
    request = InvestigationRequest(
        tenant_id="studio-a",
        project_id="game-a",
        objective=Objective(objective_id="perf", description="验证帧耗时"),
    )
    investigation, _ = repository.create_investigation(request, "req-1")
    registry = CapabilityRegistry(repository)
    registry.register(capability())
    action = make_action(investigation.investigation_id).model_copy(
        update={"parameters": {"capture_profile": "frame_csv", "shell": "unsafe"}}
    )
    with pytest.raises(Exception):
        PolicyEngine(repository, registry).reserve(
            action, principal_id="user-1", scopes={"runs:submit"}
        )


def test_real_run_count_budget_is_reserved_atomically(repository):
    request = InvestigationRequest(
        tenant_id="studio-a",
        project_id="game-a",
        objective=Objective(objective_id="perf", description="验证帧耗时"),
        budget=Budget(max_real_runs=1),
    )
    investigation, _ = repository.create_investigation(request, "req-budget")
    registry = CapabilityRegistry(repository)
    registry.register(capability())
    policy = PolicyEngine(repository, registry)
    policy.reserve(
        make_action(investigation.investigation_id),
        principal_id="user-1",
        scopes={"runs:submit"},
    )
    repeated_experiment = make_action(
        investigation.investigation_id, action_id="act-2", key="inv/act-2"
    ).model_copy(update={"repeat_index": 2})
    with pytest.raises(ConflictError, match="real_runs budget"):
        policy.reserve(repeated_experiment, principal_id="user-1", scopes={"runs:submit"})


def test_semantic_identity_ignores_description_but_repository_sequences_repeats(repository):
    investigation, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective=Objective(objective_id="perf", description="验证帧耗时"),
        ),
        "semantic-identity",
    )
    registry = CapabilityRegistry(repository)
    registry.register(capability())
    policy = PolicyEngine(repository, registry)

    baseline = make_action(investigation.investigation_id)
    policy.reserve(baseline, principal_id="user-1", scopes={"runs:submit"})
    wording_only = make_action(
        investigation.investigation_id, action_id="act-wording", key="inv/act-wording"
    ).model_copy(update={"expected_observation": "只改变自然语言描述"})
    with pytest.raises(ConflictError, match="semantically duplicate"):
        policy.reserve(wording_only, principal_id="user-1", scopes={"runs:submit"})

    first_repeat = make_action(
        investigation.investigation_id, action_id="act-exp-1", key="inv/act-exp-1"
    ).model_copy(update={"experiment_id": "exp-frame-stability"})
    second_repeat = make_action(
        investigation.investigation_id, action_id="act-exp-2", key="inv/act-exp-2"
    ).model_copy(
        update={
            "experiment_id": "exp-frame-stability",
            "expected_observation": "复测文案可以不同",
        }
    )
    first = policy.reserve(first_repeat, principal_id="user-1", scopes={"runs:submit"})
    second = policy.reserve(second_repeat, principal_id="user-1", scopes={"runs:submit"})
    assert first.action.run_sequence == 1
    assert first.action.repeat_index is None
    assert second.action.run_sequence == 2
    assert second.action.repeat_index == 2
