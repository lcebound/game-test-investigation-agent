from __future__ import annotations

import asyncio

import pytest

from game_test_agent.activities import ActionService
from game_test_agent.capabilities import CapabilityRegistry
from game_test_agent.contracts.models import (
    ActionSpec,
    CapabilityDescriptor,
    InvestigationRequest,
    TestObjective as Objective,
)
from game_test_agent.mcp_gateway import ActionDispatcher, StandardToolError
from game_test_agent.policies import PolicyEngine


class UnknownProvider:
    provider_id = "unknown-provider"

    def __init__(self) -> None:
        self.submit_calls = 0

    async def submit(self, action):
        self.submit_calls += 1
        raise StandardToolError(
            "CONNECTION_LOST",
            "response lost",
            action_id=action.action_id,
            side_effect_status="unknown",
        )

    async def lookup_action(self, action_id):
        return None

    async def query_operation(self, operation_id):
        raise AssertionError("not reached")

    async def cancel(self, action_id):
        return {"accepted": True}


def test_unknown_non_idempotent_effect_is_not_blindly_retried(repository):
    async def scenario() -> None:
        investigation, _ = repository.create_investigation(
            InvestigationRequest(
                tenant_id="studio-a",
                project_id="game-a",
                objective=Objective(objective_id="test", description="执行测试"),
            ),
            "req-unknown",
        )
        registry = CapabilityRegistry(repository)
        registry.register(
            CapabilityDescriptor.model_validate(
                {
                    "capability_id": "test.run",
                    "capability_version": "1",
                    "provider_id": "unknown-provider",
                    "provider_revision": "1",
                    "tool_name": "submit",
                    "project_ids": ["game-a"],
                    "input_schema": {"type": "object", "additionalProperties": False},
                    "risk_level": "bounded_execution",
                    "required_scopes": ["runs:submit"],
                    "execution_owner": "unknown-provider",
                    "resource_domain": "pool",
                    "idempotency": "supported",
                    "supports_action_lookup": True,
                }
            )
        )
        provider = UnknownProvider()
        dispatcher = ActionDispatcher()
        dispatcher.register(provider)
        service = ActionService(repository, PolicyEngine(repository, registry), dispatcher)
        action = ActionSpec(
            tenant_id="studio-a",
            project_id="game-a",
            investigation_id=investigation.investigation_id,
            action_id="act-unknown",
            idempotency_key="unknown/act",
            capability_id="test.run",
            capability_version="1",
            provider_id="unknown-provider",
            parameters={},
            expected_observation="结果",
            plan_revision=1,
        )
        with pytest.raises(StandardToolError):
            await service.submit(action, principal_id="test", scopes={"runs:submit"})
        with pytest.raises(StandardToolError, match="UNKNOWN_SIDE_EFFECT"):
            await service.submit(action, principal_id="test", scopes={"runs:submit"})
        assert provider.submit_calls == 1

    asyncio.run(scenario())
