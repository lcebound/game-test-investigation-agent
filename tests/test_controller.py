from __future__ import annotations

import asyncio

from game_test_agent.activities import ActionService, ReasoningService
from game_test_agent.adapters import ReferenceProviderAdapter
from game_test_agent.agents import Investigator
from game_test_agent.capabilities import CapabilityRegistry
from game_test_agent.contracts.models import (
    ActionSpec,
    CapabilityDescriptor,
    InvestigationRequest,
    InvestigationStatus,
    TestObjective as Objective,
    NextStep,
)
from game_test_agent.evidence import ContextBuilder, EvidenceManifestIngestor
from game_test_agent.mcp_gateway import ActionDispatcher
from game_test_agent.planning import ActionPlanner
from game_test_agent.policies import PolicyEngine
from game_test_agent.reports import ReportService
from game_test_agent.storage import LocalArtifactStore
from game_test_agent.workflows import InvestigationController


class FailingInvestigator:
    model_name = "failing-model"

    async def reason_next_step(self, _context):
        raise TimeoutError("model call timed out")


class QueryExistingRunInvestigator:
    model_name = "query-existing-run"

    def __init__(self, action_id: str) -> None:
        self.action_id = action_id
        self.calls = 0

    async def reason_next_step(self, context):
        self.calls += 1
        objective_id = context["investigation"]["request"]["objective"]["objective_id"]
        if self.calls == 1:
            return NextStep(
                kind="query_run",
                objective_id=objective_id,
                target_action_id=self.action_id,
                query_purpose="确认已有运行的终态结果",
                expected_observation="读取已有运行结果",
                reason_summary="对账已有运行，不创建新运行。",
            )
        return NextStep(
            kind="report",
            objective_id=objective_id,
            expected_observation="报告已有结果",
            reason_summary="existing_run_reconciled",
        )


def test_controller_runs_bounded_action_loop_and_reports(repository, settings):
    async def scenario() -> None:
        investigation, _ = repository.create_investigation(
            InvestigationRequest(
                tenant_id="studio-a",
                project_id="game-a",
                objective=Objective(objective_id="smoke", description="运行冒烟测试"),
                constraints={"action_parameters": {"case": "smoke"}},
            ),
            "controller-1",
        )
        registry = CapabilityRegistry(repository)
        registry.register(
            CapabilityDescriptor.model_validate(
                {
                    "capability_id": "test.run",
                    "capability_version": "1",
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
        provider = ReferenceProviderAdapter()
        dispatcher = ActionDispatcher()
        dispatcher.register(provider)
        controller = InvestigationController(
            repository,
            registry,
            ReasoningService(repository, Investigator("disabled", enabled=False)),
            ActionPlanner("disabled", enabled=False),
            ActionService(repository, PolicyEngine(repository, registry), dispatcher),
            EvidenceManifestIngestor(repository, LocalArtifactStore(settings.artifact_root)),
            ReportService(repository),
            provider_lookup=lambda provider_id: provider,
        )
        report = await controller.run(
            investigation.investigation_id,
            principal_id="controller",
            scopes={"runs:submit"},
        )
        assert report is not None
        assert report.stop_reason == "已有实验未产生证据，不能在没有新依据时隐式重复运行。"
        assert (
            repository.get_investigation(investigation.investigation_id).status
            == InvestigationStatus.COMPLETED
        )
        assert len(repository.list_actions(investigation.investigation_id)) == 1
        planned = repository.get_planned_action(investigation.investigation_id, 1)
        assert planned is not None
        assert planned.action.provider_revision == "1"
        assert len(repository.pending_events()) == 1
        final_context = ContextBuilder(repository, registry).build(
            investigation.investigation_id, scopes={"runs:submit"}
        )
        completed = final_context.working_state.completed_actions[0]
        assert completed.execution_status == "succeeded"
        assert completed.test_verdict == "passed"
        assert completed.execution_source == "simulated"
        assert completed.result_ingestion_state == "ingested"
        outcomes = repository.list_decision_outcomes(investigation.investigation_id)
        assert any(
            ref.startswith("run://reference-simulated/game-a/")
            for outcome in outcomes
            for ref in outcome.observed_outcome_refs
        )
        final_outcome = outcomes[-1]
        assert final_outcome.termination_basis == report.stop_reason
        assert f"report://{investigation.investigation_id}/1" in (
            final_outcome.observed_outcome_refs
        )

    asyncio.run(scenario())


def test_controller_turns_model_failure_into_bounded_report(repository, settings):
    async def scenario() -> None:
        investigation, _ = repository.create_investigation(
            InvestigationRequest(
                tenant_id="studio-a",
                project_id="game-a",
                objective=Objective(objective_id="timeout", description="验证模型失败边界"),
            ),
            "controller-model-timeout",
        )
        registry = CapabilityRegistry(repository)
        registry.register(
            CapabilityDescriptor.model_validate(
                {
                    "capability_id": "test.run",
                    "capability_version": "1",
                    "provider_id": "reference-simulated",
                    "provider_revision": "1",
                    "tool_name": "gt_runs_submit",
                    "project_ids": ["game-a"],
                    "input_schema": {"type": "object"},
                    "risk_level": "bounded_execution",
                    "required_scopes": [],
                    "execution_owner": "reference-simulated",
                    "resource_domain": "reference",
                }
            )
        )
        provider = ReferenceProviderAdapter()
        dispatcher = ActionDispatcher()
        dispatcher.register(provider)
        controller = InvestigationController(
            repository,
            registry,
            ReasoningService(repository, FailingInvestigator()),
            ActionPlanner("disabled", enabled=False),
            ActionService(repository, PolicyEngine(repository, registry), dispatcher),
            EvidenceManifestIngestor(repository, LocalArtifactStore(settings.artifact_root)),
            ReportService(repository),
            provider_lookup=lambda provider_id: provider,
        )

        report = await controller.run(
            investigation.investigation_id,
            principal_id="controller",
            scopes=set(),
        )

        assert report is not None
        assert report.stop_reason == "reasoning_failed:TimeoutError"
        assert (
            repository.get_investigation(investigation.investigation_id).status
            == InvestigationStatus.COMPLETED
        )

    asyncio.run(scenario())


def test_query_run_does_not_submit_a_new_action(repository, settings):
    async def scenario() -> None:
        investigation, _ = repository.create_investigation(
            InvestigationRequest(
                tenant_id="studio-a",
                project_id="game-a",
                objective=Objective(objective_id="query", description="查询已有运行"),
            ),
            "controller-query",
        )
        registry = CapabilityRegistry(repository)
        descriptor = CapabilityDescriptor.model_validate(
            {
                "capability_id": "test.run",
                "capability_version": "1",
                "provider_id": "reference-simulated",
                "provider_revision": "1",
                "tool_name": "gt_runs_submit",
                "project_ids": ["game-a"],
                "input_schema": {"type": "object", "additionalProperties": False},
                "risk_level": "bounded_execution",
                "required_scopes": ["runs:submit"],
                "execution_owner": "reference-simulated",
                "resource_domain": "reference",
            }
        )
        registry.register(descriptor)
        provider = ReferenceProviderAdapter()
        dispatcher = ActionDispatcher()
        dispatcher.register(provider)
        actions = ActionService(repository, PolicyEngine(repository, registry), dispatcher)
        action = ActionSpec(
            tenant_id="studio-a",
            project_id="game-a",
            investigation_id=investigation.investigation_id,
            action_id="act-existing",
            idempotency_key="controller-query/act-existing",
            capability_id="test.run",
            capability_version="1",
            provider_id="reference-simulated",
            parameters={},
            expected_observation="已有运行",
            plan_revision=1,
        )
        await actions.submit(action, principal_id="controller", scopes={"runs:submit"})
        assert await actions.reconcile(action.action_id) is not None

        investigator = QueryExistingRunInvestigator(action.action_id)
        controller = InvestigationController(
            repository,
            registry,
            ReasoningService(repository, investigator),
            ActionPlanner("disabled", enabled=False),
            actions,
            EvidenceManifestIngestor(repository, LocalArtifactStore(settings.artifact_root)),
            ReportService(repository),
            provider_lookup=lambda _provider_id: provider,
        )
        report = await controller.run(
            investigation.investigation_id,
            principal_id="controller",
            scopes={"runs:submit"},
        )
        assert report is not None
        assert report.stop_reason == "existing_run_reconciled"
        assert len(repository.list_actions(investigation.investigation_id)) == 1

    asyncio.run(scenario())
