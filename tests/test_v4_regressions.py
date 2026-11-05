from __future__ import annotations

import asyncio
import hashlib

import pytest

from game_test_agent.activities import ActionService, ReasoningService
from game_test_agent.adapters import ReferenceProviderAdapter
from game_test_agent.capabilities import CapabilityRegistry
from game_test_agent.context import ContextBroker
from game_test_agent.context.budget import fit_final_context
from game_test_agent.contracts.models import (
    EvidenceManifestItem,
    InvestigationRequest,
    NextStep,
    PlanDelta,
    ProjectPack,
    TestObjective as Objective,
)
from game_test_agent.evidence import ContextBuilder, EvidenceManifestIngestor
from game_test_agent.memory import ProjectPackRegistry
from game_test_agent.mcp_gateway import ActionDispatcher
from game_test_agent.planning import ActionPlanner, DurableActionPlanner
from game_test_agent.policies import PolicyEngine
from game_test_agent.provenance.projections import ProjectionCoordinator, ProjectionLagError
from game_test_agent.reports import ReportService
from game_test_agent.runtime import InvestigationKernel
from game_test_agent.storage import LocalArtifactStore, canonical_json
from game_test_agent.workflows import InvestigationController
from test_next_generation_architecture import capability


class EvidenceProvider(ReferenceProviderAdapter):
    provider_id = "provider-a"
    content = b"frame_ms\n16\n17\n"

    async def submit(self, action):
        operation = await super().submit(action)
        result = self._results[operation.operation_id]
        self._results[operation.operation_id] = result.model_copy(
            update={
                "evidence_manifest": [
                    EvidenceManifestItem(
                        evidence_id="metrics",
                        artifact_ref="metrics",
                        kind="frame_metrics",
                        mime_type="text/csv",
                        sha256=hashlib.sha256(self.content).hexdigest(),
                        locator={"kind": "sample_window", "value": "all"},
                        quality="simulated",
                        producer=self.provider_id,
                        producer_version="1",
                    )
                ],
            }
        )
        return operation

    async def fetch_artifact(self, _ref):
        return self.content, "text/csv"


class RepeatInvestigator:
    model_name = "offline-scripted"

    def __init__(self, repeats):
        self.repeats = repeats

    async def reason_next_step(self, context):
        count = len(context["working_state"]["completed_actions"])
        return NextStep(
            kind="experiment" if count < self.repeats else "report",
            objective_id="repeat",
            capability_id="test.run",
            provider_id="provider-a",
            evidence_ids=[item["evidence_id"] for item in context["evidence"]],
            expected_observation="independent repeat",
            reason_summary="sufficient" if count >= self.repeats else "need independent evidence",
        )


@pytest.mark.parametrize("repeats,recover", [(1, False), (2, False), (3, False), (2, True)])
@pytest.mark.parametrize("budget_limit", [20, 2])
@pytest.mark.parametrize("pending", [False, True])
def test_compiled_plan_executes_independent_repeats_and_stops(
    repository, settings, repeats, recover, monkeypatch, budget_limit, pending
):
    async def scenario():
        investigation, _ = repository.create_investigation(
            InvestigationRequest(
                tenant_id="studio-a",
                project_id="game-a",
                objective=Objective(objective_id="repeat", description="independent observations"),
                constraints={"action_parameters": {}},
                budget={"max_device_minutes": budget_limit},
            ),
            "repeat",
        )
        registry = CapabilityRegistry(repository)
        registry.register(capability("test.run"))
        provider = EvidenceProvider()
        dispatcher = ActionDispatcher()
        dispatcher.register(provider)
        controller = InvestigationController(
            repository,
            registry,
            ReasoningService(repository, RepeatInvestigator(repeats)),
            ActionPlanner("disabled"),
            ActionService(repository, PolicyEngine(repository, registry), dispatcher),
            EvidenceManifestIngestor(repository, LocalArtifactStore(settings.artifact_root)),
            ReportService(repository),
            provider_lookup=lambda _: provider,
        )
        if recover:
            original_save = repository.save_planned_action
            interrupted = False

            def crash_before_action_save(planned):
                nonlocal interrupted
                if planned.plan_revision == 2 and not interrupted:
                    interrupted = True
                    raise OSError("worker lost after plan extension")
                return original_save(planned)

            monkeypatch.setattr(repository, "save_planned_action", crash_before_action_save)
            original_plan = controller.durable_action_planner.plan

            async def recover_planning(context, decision, *, ordinal):
                try:
                    return await original_plan(context, decision, ordinal=ordinal)
                except OSError:
                    return await DurableActionPlanner(repository, ActionPlanner("disabled")).plan(
                        context, decision, ordinal=ordinal
                    )

            monkeypatch.setattr(controller.durable_action_planner, "plan", recover_planning)
        if pending:

            async def wait_pending(*args):
                return None

            monkeypatch.setattr(controller, "_wait_for_result", wait_pending)
        report = await controller.run(
            investigation.investigation_id, principal_id="test", scopes=set()
        )
        assert report is not None
        if pending:
            assert report.stop_reason == "external_result_pending"
            assert "目标尚未完成" in report.summary
            assert len(provider._actions) == 1
            return
        if budget_limit == 2 and repeats > 1:
            assert report.stop_reason == "plan_budget_rejected:device_minutes:required=4:limit=2"
            assert "目标尚未完成" in report.summary
            assert len(provider._actions) == 1
            assert repository.get_latest_plan(investigation.investigation_id).revision == 1
            return
        assert "planning_failed" not in report.stop_reason
        actions = repository.list_actions(investigation.investigation_id)
        assert len(actions) == repeats
        assert len(provider._actions) == repeats
        assert len({item["action"]["plan_step_id"] for item in actions}) == repeats
        assert len(repository.list_evidence(investigation.investigation_id)) == repeats
        context = controller.context_builder.build(investigation.investigation_id, scopes=set())
        experiment_steps = [step for step in context.plan.steps if step.action_type == "experiment"]
        assert len(experiment_steps) == repeats
        assert all(step.status == "completed" for step in experiment_steps)
        if recover:
            assert interrupted
            assert context.plan.revision == 2

    asyncio.run(scenario())


def test_same_round_delta_is_used_for_action_without_mutating_snapshot(repository):
    investigation, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective=Objective(objective_id="delta", description="new plan"),
            constraints={"action_parameters": {}},
        ),
        "delta-execute",
    )
    registry = CapabilityRegistry(repository)
    descriptor = capability("test.run")
    registry.register(descriptor)
    kernel = InvestigationKernel(repository)
    compilation = kernel.objective_compiler.compile(
        investigation.investigation_id, investigation.request, [descriptor]
    )
    context = ContextBuilder(repository, registry).build(
        investigation.investigation_id, scopes=set()
    )
    snapshot = kernel.prepare_round(
        context, plan_revision=1, model_name="offline", prompt_version="1"
    ).snapshot
    before = canonical_json(snapshot)
    decision = kernel.accept_decision(
        snapshot,
        NextStep(
            kind="experiment",
            objective_id="delta",
            capability_id="test.run",
            provider_id="provider-a",
            expected_observation="new observation",
            reason_summary="create plan",
            plan_delta=PlanDelta(
                base_revision=0, added_steps=compilation.selected.plan.steps, reason="initial plan"
            ),
        ),
    )
    planner = DurableActionPlanner(repository, ActionPlanner("disabled"))
    planned = asyncio.run(planner.plan(snapshot.context, decision))
    assert planned.action.plan_node_revision == 1
    assert planned.action.plan_step_id == "collect-evidence"
    assert canonical_json(snapshot) == before
    assert asyncio.run(planner.plan(snapshot.context, decision)) == planned


def test_runtime_preserves_node_history_and_delta_cannot_replace_executed_node(repository):
    from game_test_agent.contracts.models import ActionProposal, PlanStepReplacement
    from game_test_agent.planning import ActionFactory
    from game_test_agent.planning.deltas import InvestigationDeltaService

    investigation, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective=Objective(objective_id="history", description="preserve executed history"),
        ),
        "history",
    )
    registry = CapabilityRegistry(repository)
    registry.register(capability("test.run"))
    kernel = InvestigationKernel(repository)
    kernel.compile_objective(
        investigation.investigation_id, registry.discover("game-a", scopes=set())
    )
    context = ContextBuilder(repository, registry).build(
        investigation.investigation_id, scopes=set()
    )
    action = ActionFactory.create(
        context,
        ActionProposal(
            capability_id="test.run",
            capability_version="1",
            provider_id="provider-a",
            parameters={},
            expected_observation="result",
        ),
        plan_revision=1,
        ordinal=1,
    )
    PolicyEngine(repository, registry).reserve(action, principal_id="test", scopes=set())
    original = repository.get_latest_plan(investigation.investigation_id)
    replacement = original.steps[0].model_copy(update={"expected_evidence": ["new-kind"]})
    with pytest.raises(ValueError, match="executed plan steps"):
        InvestigationDeltaService(repository).apply(
            context,
            NextStep(
                kind="report",
                objective_id="history",
                expected_observation="report",
                reason_summary="replace",
                plan_delta=PlanDelta(
                    base_revision=1,
                    reason="replace",
                    replacements=[
                        PlanStepReplacement(
                            step_id=original.steps[0].step_id, replacement=replacement
                        ),
                    ],
                ),
            ),
        )
    repository.save_plan(original.model_copy(update={"revision": 2}), "catalog")
    projected = ContextBuilder(repository, registry).build(
        investigation.investigation_id, scopes=set()
    )
    assert projected.plan.steps[0].status == "running"


def test_projection_does_not_certify_event_arriving_during_build(repository, monkeypatch):
    investigation, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective=Objective(objective_id="projection", description="concurrent event"),
        ),
        "projection-race",
    )
    coordinator = ProjectionCoordinator(repository)
    original = coordinator.provenance.build
    calls = 0

    def concurrent_build(identifier):
        nonlocal calls
        graph = original(identifier)
        calls += 1
        if calls == 1:
            repository.record_investigation_event(identifier, "external.changed", {})
        return graph

    monkeypatch.setattr(coordinator.provenance, "build", concurrent_build)
    coordinator.refresh(investigation.investigation_id)
    assert calls == 2
    coordinator.assert_fresh(investigation.investigation_id)

    def always_changing(identifier):
        graph = original(identifier)
        repository.record_investigation_event(identifier, "external.changed", {})
        return graph

    monkeypatch.setattr(coordinator.provenance, "build", always_changing)
    with pytest.raises(ProjectionLagError):
        coordinator.refresh(investigation.investigation_id)
    with pytest.raises(ProjectionLagError):
        coordinator.assert_fresh(investigation.investigation_id)


def test_final_context_budget_includes_pack_and_serialized_manifest(repository):
    investigation, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective=Objective(objective_id="context", description="bounded"),
            constraints={"project_pack_version": "1"},
        ),
        "pack-budget",
    )
    registry = CapabilityRegistry(repository)
    packs = ProjectPackRegistry()
    packs.register(
        ProjectPack(
            pack_id="pack",
            version="1",
            tenant_id="studio-a",
            project_id="game-a",
            terminology={"huge": "term " * 10000},
            reviewed_by="reviewer",
        )
    )
    broker = ContextBroker(ContextBuilder(repository, registry), registry, project_packs=packs)
    context = broker.assemble(
        investigation.investigation_id, scopes=set(), window_limit_tokens=8192
    )
    assert context.project_pack is None
    assert context.context_window.omitted_records["project_pack"] == 1
    assert not any(item.object_type == "project_pack" for item in context.context_manifest.included)
    actual = (len(canonical_json(context).encode("utf-8")) + 1) // 2
    assert actual == context.context_window.estimated_input_tokens
    assert actual <= 8192 - context.context_window.reserved_output_tokens
    assert not context.context_window.over_budget
    context.context_window.window_limit_tokens = 100
    context.context_window.reserved_output_tokens = 0
    oversized = fit_final_context(context)
    assert oversized.context_window.over_budget
    assert oversized.working_state == context.working_state
    assert (
        InvestigationKernel(repository)
        .prepare_round(
            oversized,
            plan_revision=1,
            model_name="offline",
            prompt_version="1",
        )
        .stop_reason
        == "mandatory_context_exceeds_token_budget"
    )
