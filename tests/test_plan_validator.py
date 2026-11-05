from __future__ import annotations

from game_test_agent.contracts.models import (
    CapabilityDescriptor,
    PlanInputBinding,
    PlanOutputContract,
    PlanFailureHandling,
    PlanStep,
    TestPlan as Plan,
)
from game_test_agent.planning import PlanRuntimeProjector, PlanValidator


def test_plan_validator_rejects_cycles_and_unobservable_actions():
    plan = Plan(
        investigation_id="inv-1",
        revision=1,
        steps=[
            PlanStep(
                step_id="a",
                objective_id="o",
                dependencies=["b"],
                action_type="query",
                provider_id="missing",
                capability_id="read",
                capability_version="1",
            ),
            PlanStep(
                step_id="b",
                objective_id="o",
                dependencies=["a"],
                action_type="analyze",
            ),
        ],
    )
    result = PlanValidator().validate(plan, [], max_device_minutes=1)
    assert not result.accepted
    assert any("cycle" in error for error in result.errors)
    assert any("no observable result" in error for error in result.errors)


def test_skipped_steps_do_not_consume_future_plan_budget():
    plan = Plan(
        investigation_id="inv-1",
        revision=2,
        steps=[
            PlanStep(
                step_id="old-experiment",
                objective_id="o",
                action_type="analyze",
                estimated_device_minutes=100,
                status="skipped",
            ),
            PlanStep(step_id="report", objective_id="o", action_type="report"),
        ],
    )
    result = PlanValidator().validate(plan, [], max_device_minutes=1)
    assert result.accepted


def test_query_steps_cannot_bind_run_creating_capabilities():
    capability = CapabilityDescriptor(
        capability_id="test.run",
        capability_version="1",
        provider_id="portable-provider",
        provider_revision="catalog-1",
        tool_name="submit",
        project_ids=["game-a"],
        input_schema={"type": "object"},
        risk_level="bounded_execution",
        execution_owner="portable-provider",
        resource_domain="pool",
    )
    plan = Plan(
        investigation_id="inv-1",
        revision=1,
        steps=[
            PlanStep(
                step_id="wrong-query",
                objective_id="o",
                action_type="query",
                provider_id="portable-provider",
                capability_id="test.run",
                capability_version="1",
                expected_evidence=["run status"],
            )
        ],
    )
    result = PlanValidator().validate(plan, [capability], max_device_minutes=1)
    assert not result.accepted
    assert any("not a run query" in error for error in result.errors)


def test_plan_validator_checks_typed_dependency_inputs_and_step_cost_caps():
    plan = Plan(
        investigation_id="inv-contract",
        revision=1,
        steps=[
            PlanStep(
                step_id="collect",
                objective_id="o",
                action_type="analyze",
                outputs=[PlanOutputContract(output_name="samples", output_type="metric[]")],
                status="completed",
            ),
            PlanStep(
                step_id="evaluate",
                objective_id="o",
                dependencies=["collect"],
                action_type="analyze",
                input_bindings=[
                    PlanInputBinding(
                        input_name="samples",
                        source_step_id="collect",
                        source_output="samples",
                        expected_type="trace[]",
                    )
                ],
                estimated_device_minutes=2,
                cost_limit_device_minutes=1,
                status="ready",
            ),
        ],
    )

    result = PlanValidator().validate(plan, [], max_device_minutes=10)

    assert not result.accepted
    assert any("expects trace[], got metric[]" in error for error in result.errors)
    assert any("exceeds its device-minute cost limit" in error for error in result.errors)


def test_plan_runtime_projects_action_state_and_promotes_dependents():
    plan = Plan(
        investigation_id="inv-runtime",
        revision=4,
        steps=[
            PlanStep(
                step_id="collect",
                objective_id="o",
                action_type="experiment",
                capability_id="run",
                capability_version="1",
                provider_id="lab",
                expected_evidence=["result"],
                failure_handling=PlanFailureHandling(strategy="retry", max_attempts=2),
                status="ready",
            ),
            PlanStep(
                step_id="evaluate",
                objective_id="o",
                dependencies=["collect"],
                action_type="evaluate",
            ),
        ],
    )
    action = {
        "action": {"plan_step_id": "collect", "plan_node_revision": 4},
        "state": "succeeded",
    }

    projected = PlanRuntimeProjector().project(
        plan,
        actions=[action],
        assessment=None,
        has_user_input=False,
        report_published=False,
    )

    assert projected is not None
    assert [item.status for item in projected.steps] == ["completed", "ready"]
