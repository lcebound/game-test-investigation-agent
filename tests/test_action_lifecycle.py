from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from game_test_agent.activities import ActionService
from game_test_agent.adapters import ReferenceProviderAdapter
from game_test_agent.capabilities import CapabilityRegistry
from game_test_agent.contracts.models import (
    ActionSpec,
    ActionState,
    CapabilityDescriptor,
    InvestigationRequest,
    RunRef,
    RunResult,
    TestObjective as Objective,
)
from game_test_agent.mcp_gateway import ActionDispatcher
from game_test_agent.policies import PolicyEngine


@pytest.mark.parametrize("interrupted_cleanup", [False, True])
def test_action_submit_and_reconcile(repository, interrupted_cleanup):
    async def scenario() -> None:
        investigation, _ = repository.create_investigation(
            InvestigationRequest(
                tenant_id="studio-a",
                project_id="game-a",
                objective=Objective(objective_id="smoke", description="运行参考测试"),
            ),
            "req-action",
        )
        registry = CapabilityRegistry(repository)
        registry.register(
            CapabilityDescriptor.model_validate(
                {
                    "capability_id": "test.run",
                    "capability_version": "1.0.0",
                    "provider_id": "reference-simulated",
                    "provider_revision": "1",
                    "tool_name": "gt_runs_submit",
                    "project_ids": ["game-a"],
                    "input_schema": {
                        "type": "object",
                        "properties": {"case": {"type": "string"}},
                        "required": ["case"],
                        "additionalProperties": False,
                    },
                    "risk_level": "bounded_execution",
                    "required_scopes": ["runs:submit"],
                    "execution_owner": "reference-simulated",
                    "resource_domain": "reference",
                    "estimated_cost": {"device_minutes": 1},
                }
            )
        )
        dispatcher = ActionDispatcher()
        dispatcher.register(ReferenceProviderAdapter())
        service = ActionService(repository, PolicyEngine(repository, registry), dispatcher)
        action = ActionSpec(
            tenant_id="studio-a",
            project_id="game-a",
            investigation_id=investigation.investigation_id,
            action_id="act-reference-1",
            idempotency_key=f"{investigation.investigation_id}/act-reference-1",
            capability_id="test.run",
            capability_version="1.0.0",
            provider_id="reference-simulated",
            parameters={"case": "smoke"},
            expected_observation="模拟协议结果",
            plan_revision=1,
        )
        operation = await service.submit(action, principal_id="test", scopes={"runs:submit"})
        assert operation.accepted
        if interrupted_cleanup:
            # A crash after storing a terminal result can leave the old lease active.
            service.scheduler.release = lambda *_args, **_kwargs: False
        result = await service.reconcile(action.action_id)
        assert result is not None
        assert result.execution_source == "simulated"
        replayed = await service.reconcile(action.action_id)
        assert replayed == result
        snapshot = repository.get_latest_run_result(action.action_id)
        assert snapshot is not None
        assert snapshot.ingestion_state == "pending"
        _, state, _ = repository.get_action(action.action_id)
        assert state == ActionState.SUCCEEDED
        permit = repository.get_action_permit(action.action_id)
        assert permit.admission_ticket_id is not None
        assert permit.resource_domain == "reference"
        assert permit.fencing_token == 1
        ticket_row = repository._connection.execute(
            "SELECT state FROM resource_admission_tickets WHERE ticket_id=?",
            (permit.admission_ticket_id,),
        ).fetchone()
        if interrupted_cleanup:
            assert ticket_row["state"] == "active"
            assert service.scheduler.admit_next(
                now=repository.get_admission_ticket(
                    investigation.investigation_id, action.action_id
                ).expires_at + timedelta(seconds=1)
            ) is None
            request_state = repository._connection.execute(
                "SELECT state FROM resource_admission_requests WHERE node_id=?",
                (action.action_id,),
            ).fetchone()["state"]
            assert request_state == "canceled"
        else:
            assert ticket_row["state"] == "released"

    asyncio.run(scenario())


def test_run_result_identity_is_scoped_by_tenant(repository):
    registry = CapabilityRegistry(repository)
    registry.register(
        CapabilityDescriptor.model_validate(
            {
                "capability_id": "test.run",
                "capability_version": "1",
                "provider_id": "shared-lab",
                "provider_revision": "1",
                "tool_name": "run",
                "project_ids": ["shared-project"],
                "input_schema": {"type": "object", "additionalProperties": False},
                "risk_level": "bounded_execution",
                "required_scopes": [],
                "execution_owner": "shared-lab",
                "resource_domain": "pool",
            }
        )
    )
    policy = PolicyEngine(repository, registry)
    action_ids: dict[str, str] = {}
    for tenant in ("tenant-a", "tenant-b"):
        investigation, _ = repository.create_investigation(
            InvestigationRequest(
                tenant_id=tenant,
                project_id="shared-project",
                objective=Objective(objective_id="scope", description="验证租户隔离"),
            ),
            f"run-scope-{tenant}",
        )
        action_id = f"action-{tenant}"
        action_ids[tenant] = action_id
        policy.reserve(
            ActionSpec(
                tenant_id=tenant,
                project_id="shared-project",
                investigation_id=investigation.investigation_id,
                action_id=action_id,
                idempotency_key=f"{tenant}/{action_id}",
                capability_id="test.run",
                capability_version="1",
                provider_id="shared-lab",
                parameters={},
                expected_observation="运行结果",
                plan_revision=1,
            ),
            principal_id="test",
            scopes=set(),
        )

    result = RunResult(
        run_ref=RunRef(provider_id="shared-lab", project_id="shared-project", run_id="same-run-id"),
        revision=1,
        execution_status="succeeded",
        test_verdict="passed",
        execution_source="real",
        provider_status="DONE",
    )
    for tenant, action_id in action_ids.items():
        snapshot, created = repository.record_run_result(action_id, result)
        assert created
        assert snapshot.tenant_id == tenant
        assert repository.get_run_result_by_ref(tenant, result.run_ref, 1).action_id == action_id
