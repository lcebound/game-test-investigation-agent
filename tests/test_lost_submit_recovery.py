from __future__ import annotations

import asyncio

from game_test_agent.activities import ActionService
from game_test_agent.capabilities import CapabilityRegistry
from game_test_agent.contracts.models import (
    ActionSpec,
    CapabilityDescriptor,
    InvestigationRequest,
    OperationRef,
    RunRef,
    TestObjective as Objective,
)
from game_test_agent.mcp_gateway import ActionDispatcher, StandardToolError
from game_test_agent.policies import PolicyEngine


class LostResponseProvider:
    provider_id = "lost-response"

    def __init__(self) -> None:
        self.submit_calls = 0
        self.operation = None

    async def submit(self, action):
        self.submit_calls += 1
        self.operation = OperationRef(
            accepted=True,
            action_id=action.action_id,
            run_ref=RunRef(
                provider_id=self.provider_id,
                project_id=action.project_id,
                run_id="run-existing",
            ),
            operation_id="op-existing",
            execution_status="queued",
        )
        raise StandardToolError(
            "CONNECTION_LOST",
            "response was lost after commit",
            retryable=True,
            action_id=action.action_id,
            side_effect_status="unknown",
        )

    async def lookup_action(self, action_id):
        return self.operation if self.operation and self.operation.action_id == action_id else None

    async def query_operation(self, operation_id):
        raise AssertionError("not needed")

    async def cancel(self, action_id):
        return {"accepted": True}


def test_lost_submit_response_is_reconciled_without_duplicate_submit(repository):
    async def scenario() -> None:
        investigation, _ = repository.create_investigation(
            InvestigationRequest(
                tenant_id="studio-a",
                project_id="game-a",
                objective=Objective(objective_id="run", description="执行测试"),
            ),
            "lost-submit",
        )
        registry = CapabilityRegistry(repository)
        registry.register(
            CapabilityDescriptor.model_validate(
                {
                    "capability_id": "test.run",
                    "capability_version": "1",
                    "provider_id": "lost-response",
                    "provider_revision": "1",
                    "tool_name": "submit",
                    "project_ids": ["game-a"],
                    "input_schema": {"type": "object", "additionalProperties": False},
                    "risk_level": "bounded_execution",
                    "execution_owner": "lost-response",
                    "resource_domain": "pool",
                    "idempotency": "required",
                    "supports_action_lookup": True,
                }
            )
        )
        provider = LostResponseProvider()
        dispatcher = ActionDispatcher()
        dispatcher.register(provider)
        service = ActionService(repository, PolicyEngine(repository, registry), dispatcher)
        action = ActionSpec(
            tenant_id="studio-a",
            project_id="game-a",
            investigation_id=investigation.investigation_id,
            action_id="act-lost",
            idempotency_key="lost/act",
            capability_id="test.run",
            capability_version="1",
            provider_id="lost-response",
            parameters={},
            expected_observation="结果",
            plan_revision=1,
        )
        operation = await service.submit(action, principal_id="worker", scopes=set())
        assert operation.operation_id == "op-existing"
        assert provider.submit_calls == 1
        replay = await service.submit(action, principal_id="worker", scopes=set())
        assert replay.operation_id == "op-existing"
        assert provider.submit_calls == 1

    asyncio.run(scenario())
