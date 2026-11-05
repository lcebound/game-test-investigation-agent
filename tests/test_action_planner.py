from __future__ import annotations

import asyncio

from game_test_agent.capabilities import CapabilityRegistry
from game_test_agent.contracts.models import (
    CapabilityDescriptor,
    InvestigationRequest,
    ModelUsage,
    NextStep,
    PlanStep,
    TestPlan as Plan,
    TestObjective as Objective,
)
from game_test_agent.evidence import ContextBuilder
from game_test_agent.planning import ActionFactory, ActionPlanner
from game_test_agent.models import ModelGateway, ModelProviderResponse


def test_action_proposal_model_schema_allows_capability_defined_parameters():
    from agents import AgentOutputSchema

    from game_test_agent.contracts.models import ActionProposal

    schema = AgentOutputSchema(ActionProposal, strict_json_schema=False).json_schema()

    assert schema["properties"]["parameters"]["additionalProperties"] is True


def test_action_planner_preserves_explicit_parameters_without_model_rewriting(repository):
    async def scenario() -> None:
        investigation, _ = repository.create_investigation(
            InvestigationRequest(
                tenant_id="studio-a",
                project_id="game-a",
                objective=Objective(objective_id="smoke", description="运行冒烟测试"),
                constraints={"action_parameters": {"case": "smoke"}},
            ),
            "planner-1",
        )
        registry = CapabilityRegistry(repository)
        registry.register(
            CapabilityDescriptor.model_validate(
                {
                    "capability_id": "test.run",
                    "capability_version": "1",
                    "provider_id": "lab-a",
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
                    "required_scopes": [],
                    "execution_owner": "lab-a",
                    "resource_domain": "pool-a",
                }
            )
        )
        context = ContextBuilder(repository, registry).build(
            investigation.investigation_id, scopes=set()
        )
        proposal = await ActionPlanner("unused", enabled=True).propose(
            context,
            NextStep(
                kind="experiment",
                objective_id="smoke",
                capability_id="test.run",
                provider_id="lab-a",
                expected_observation="测试结果",
                reason_summary="执行已登记测试",
            ),
        )
        action = ActionFactory.create(context, proposal, plan_revision=1, ordinal=1)
        assert action.parameters == {"case": "smoke"}
        assert action.provider_revision == "1"
        assert action.idempotency_key.endswith(action.action_id)

        planned_context = context.model_copy(
            update={
                "plan": Plan(
                    investigation_id=investigation.investigation_id,
                    revision=7,
                    steps=[
                        PlanStep(
                            step_id="collect-smoke",
                            objective_id="smoke",
                            action_type="experiment",
                            capability_id="test.run",
                            capability_version="1",
                            provider_id="lab-a",
                            expected_evidence=["run_result"],
                            status="ready",
                        )
                    ],
                )
            }
        )
        bound = ActionFactory.create(planned_context, proposal, plan_revision=1, ordinal=2)
        assert bound.plan_step_id == "collect-smoke"
        assert bound.plan_node_revision == 7

        context.actions = [{"action": action.model_dump(mode="json"), "state": "succeeded"}]
        repeated = await ActionPlanner("unused", enabled=True).propose(
            context,
            NextStep(
                kind="experiment",
                objective_id="smoke",
                capability_id="test.run",
                provider_id="lab-a",
                evidence_ids=["ev-first-run"],
                expected_observation="独立重复测试结果",
                reason_summary="验证第一次结果能否复现",
            ),
        )
        assert repeated.parameters == {"case": "smoke"}
        assert repeated.repeat_index is None

    asyncio.run(scenario())


def test_explicit_target_and_causal_metadata_survive_durable_planning(repository):
    from game_test_agent.planning import DurableActionPlanner
    from game_test_agent.runtime import InvestigationKernel

    async def scenario() -> None:
        investigation, _ = repository.create_investigation(
            InvestigationRequest(
                tenant_id="studio-a",
                project_id="game-a",
                root_event_id="user-root-1",
                objective=Objective(objective_id="smoke", description="运行明确目标的测试"),
                constraints={
                    "action_parameters": {"test_ref": {"test_id": "case:team-compare"}},
                    "action_target_selector": {"target_kind": "windows_client"},
                    "action_constraints": {"timeout_seconds": 180},
                },
            ),
            "planner-causal-1",
        )
        registry = CapabilityRegistry(repository)
        registry.register(
            CapabilityDescriptor.model_validate(
                {
                    "capability_id": "test.run",
                    "capability_version": "1.0.0",
                    "provider_id": "platform-local",
                    "provider_revision": "1",
                    "tool_name": "integration.runs.submit",
                    "project_ids": ["game-a"],
                    "input_schema": {"type": "object"},
                    "risk_level": "bounded_execution",
                    "execution_owner": "platform-local",
                    "resource_domain": "platform-local",
                }
            )
        )
        context = ContextBuilder(repository, registry).build(
            investigation.investigation_id, scopes=set()
        )
        kernel = InvestigationKernel(repository)
        prepared = kernel.prepare_round(
            context, plan_revision=1, model_name="disabled", prompt_version="test"
        )
        assert prepared.snapshot is not None
        decision = kernel.accept_decision(
            prepared.snapshot,
            NextStep(
                kind="experiment",
                objective_id="smoke",
                capability_id="test.run",
                provider_id="platform-local",
                expected_observation="真实用例结果",
                reason_summary="运行明确登记的用例",
            ),
        )
        planned = await DurableActionPlanner(repository, ActionPlanner("disabled")).plan(
            prepared.snapshot.context, decision
        )
        assert planned.action.target_selector.target_kind == "windows_client"
        assert planned.action.constraints["timeout_seconds"] == 180
        assert planned.action.constraints["root_event_id"] == "user-root-1"
        assert planned.action.constraints["causation_id"] == decision.decision_id
        assert planned.action.constraints["investigation_depth"] == 1
        outcome = repository.latest_decision_outcome(decision.decision_id)
        assert outcome is not None
        assert outcome.revision == 1
        assert outcome.chosen_action_id == planned.action.action_id
        assert outcome.observed_outcome_refs == []

    asyncio.run(scenario())


def test_model_action_planning_audit_is_frozen_with_planned_action(repository):
    from game_test_agent.planning import DurableActionPlanner
    from game_test_agent.runtime import InvestigationKernel

    async def scenario() -> None:
        investigation, _ = repository.create_investigation(
            InvestigationRequest(
                tenant_id="studio-a",
                project_id="game-a",
                objective=Objective(objective_id="audit-plan", description="audit planning"),
            ),
            "planner-model-audit",
        )
        registry = CapabilityRegistry(repository)
        registry.register(
            CapabilityDescriptor(
                capability_id="test.run",
                capability_version="1",
                provider_id="lab-a",
                provider_revision="catalog-1",
                tool_name="run",
                project_ids=["game-a"],
                input_schema={"type": "object", "additionalProperties": False},
                risk_level="bounded_execution",
                execution_owner="lab-a",
                resource_domain="pool-a",
            )
        )
        context = ContextBuilder(repository, registry).build(
            investigation.investigation_id, scopes=set()
        )
        kernel = InvestigationKernel(repository)
        prepared = kernel.prepare_round(
            context, plan_revision=1, model_name="test", prompt_version="test"
        )
        assert prepared.snapshot is not None
        decision = kernel.accept_decision(
            prepared.snapshot,
            NextStep(
                kind="experiment",
                objective_id="audit-plan",
                capability_id="test.run",
                provider_id="lab-a",
                expected_observation="run result",
                reason_summary="execute",
            ),
        )
        planner = ActionPlanner("test", enabled=False)
        planner.enabled = True

        async def caller(_profile, _request):
            return ModelProviderResponse(
                content={
                    "capability_id": "test.run",
                    "capability_version": "1",
                    "provider_id": "lab-a",
                    "parameters": {},
                    "expected_observation": "run result",
                },
                usage=ModelUsage(
                    input_tokens=100,
                    output_tokens=25,
                    provider_request_id="request-1",
                    provider_model_id="model-resolved",
                ),
            )

        planner.gateway = ModelGateway([planner.model_profile], caller)
        planned = await DurableActionPlanner(repository, planner).plan(context, decision)

        assert planned.model_profile_id == "action-planner"
        assert planned.model_profile_version == "1"
        assert planned.raw_output_ref is not None
        assert planned.attempts[0]["status"] == "valid"
        assert planned.usage["input_tokens"] == 100
        assert planned.usage["provider_model_id"] == "model-resolved"
        assert repository.model_usage(investigation.investigation_id) == {
            "model_calls": 1,
            "attempt_count": 1,
            "input_tokens": 100,
            "output_tokens": 25,
            "latency_ms": planned.usage["latency_ms"],
            "estimated_cost": 0.0,
            "cache_hits": 0,
        }
        assert repository.get_planned_action(investigation.investigation_id, 1) == planned

    asyncio.run(scenario())
