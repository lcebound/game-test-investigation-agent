from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from game_test_agent.analysis import AnalysisRecipeRunner, AnalysisRecipeService
from game_test_agent.audit import ReadOnlyReplayService
from game_test_agent.capabilities import CapabilityRegistry
from game_test_agent.context import ContextBroker
from game_test_agent.contracts.models import (
    AnalysisRecipe,
    AnalysisRecipeStep,
    CaseApplicability,
    CaseMemoryContent,
    CaseMemoryRecord,
    CaseMemoryStatus,
    CapabilityDescriptor,
    EvidenceLocator,
    EvidenceRecord,
    Fact,
    Hypothesis,
    InvestigationRequest,
    ModelProfile,
    ModelUsage,
    NextStep,
    ProjectPack,
    ResourceRequest,
    TestObjective as Objective,
)
from game_test_agent.evidence import ContextBuilder, EvidenceService
from game_test_agent.models import ModelCallError, ModelGateway, ModelProviderResponse
from game_test_agent.memory import ProjectPackRegistry
from game_test_agent.planning import ObjectiveCompiler
from game_test_agent.provenance import ProvenanceIndex
from game_test_agent.provenance.projections import ProjectionCoordinator, ProjectionLagError
from game_test_agent.reports import ReportBuilder, ReportService
from game_test_agent.runtime import InvestigationKernel
from game_test_agent.scheduling import DurableResourceScheduler, ResourceScheduler
from game_test_agent.storage import LocalArtifactStore


def capability(capability_id: str, operation: str = "experiment") -> CapabilityDescriptor:
    return CapabilityDescriptor(
        capability_id=capability_id,
        capability_version="1",
        provider_id="provider-a",
        provider_revision="catalog-1",
        tool_name=capability_id,
        project_ids=["game-a"],
        input_schema={"type": "object"},
        risk_level="read_only" if operation != "experiment" else "bounded_execution",
        execution_owner="provider-a",
        resource_domain="pool-a",
        operation_kind=operation,
        creates_run=operation == "experiment",
        requires_existing_run=operation == "query_run",
        evidence_types=["frame_time"],
        estimated_cost={"device_minutes": 2},
    )


def test_model_gateway_retries_format_and_applies_whitelisted_normalization():
    calls = 0

    async def caller(_profile, _request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelProviderResponse(content="not-json")
        return ModelProviderResponse(
            content={
                "kind": "report",
                "objective_id": "o-1",
                "expected_observation": None,
                "reason_summary": "bounded report",
            }
        )

    gateway = ModelGateway(
        [
            ModelProfile(
                profile_id="reasoner",
                profile_version="1",
                provider="test",
                model_name="test-model",
                max_attempts=2,
            )
        ],
        caller,
    )
    result = asyncio.run(gateway.invoke("reasoner", {}, NextStep))

    assert result.output.kind == "report"
    assert [item.status for item in result.attempts] == ["format_error", "valid"]
    assert result.normalization_log == ["filled_null_report_expected_observation"]
    assert result.raw_output_ref.startswith("sha256:")


def test_model_gateway_stops_retrying_immediately_when_attempt_budget_is_exhausted():
    calls = 0

    async def caller(_profile, _request):
        nonlocal calls
        calls += 1
        return ModelProviderResponse(
            content="not-json",
            usage=ModelUsage(input_tokens=8, output_tokens=5, estimated_cost=0.25),
        )

    gateway = ModelGateway(
        [
            ModelProfile(
                profile_id="bounded",
                profile_version="1",
                provider="test",
                model_name="test-model",
                max_attempts=3,
                max_total_tokens=10,
                max_estimated_cost=1,
            )
        ],
        caller,
    )
    try:
        asyncio.run(gateway.invoke("bounded", {}, NextStep))
    except ModelCallError as error:
        assert "token budget" in str(error)
        assert [item.status for item in error.attempts] == ["budget_error"]
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("over-budget model call was accepted")
    assert calls == 1


def test_objective_compiler_builds_valid_ranked_plan():
    request = InvestigationRequest(
        tenant_id="studio-a",
        project_id="game-a",
        objective=Objective(
            objective_id="memory",
            description="分析场景切换后的内存增长",
            acceptance_criteria=["峰值增长不超过项目规范"],
        ),
    )
    compiled = ObjectiveCompiler().compile(
        "inv-compiler",
        request,
        [capability("test.run")],
    )

    assert compiled.objective_ir.goal_type == "diagnosis"
    assert compiled.objective_ir.field_origins["criteria"].source == "user"
    assert compiled.selected is not None
    assert compiled.selected.validation_errors == []
    assert [item.step_id for item in compiled.selected.plan.steps] == [
        "collect-evidence",
        "evaluate-objective",
        "publish-report",
    ]
    collect, evaluate, report = compiled.selected.plan.steps
    assert collect.outputs[0].output_type == "evidence[]"
    assert collect.completion_conditions
    assert collect.resource_requirements[0].resource_domain == "pool-a"
    assert collect.failure_handling.strategy == "replan"
    assert evaluate.input_bindings[0].source_step_id == collect.step_id
    assert report.input_bindings[0].expected_type == "criterion_assessment"


def test_context_manifest_and_progressive_capability_loading(repository):
    investigation, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective=Objective(objective_id="context", description="bounded context"),
            constraints={"project_pack_version": "2026.09"},
        ),
        "next-context",
    )
    registry = CapabilityRegistry(repository)
    registry.register(capability("test.run"))
    registry.register(capability("run.lookup", "query_run"))
    packs = ProjectPackRegistry()
    packs.register(
        ProjectPack(
            pack_id="game-a-default",
            version="2026.09",
            tenant_id="studio-a",
            project_id="game-a",
            terminology={"frame": "rendered frame"},
            reviewed_by="qa-lead",
        )
    )
    broker = ContextBroker(
        ContextBuilder(repository, registry),
        registry,
        project_packs=packs,
    )

    context = broker.assemble(
        investigation.investigation_id,
        scopes=set(),
        step_id="collect",
        capability_ids={"test.run"},
    )

    assert [item.capability_id for item in context.capabilities] == ["test.run"]
    assert context.context_manifest is not None
    assert context.context_manifest.step_id == "collect"
    assert context.context_manifest.loaded_capability_ids == ["test.run"]
    assert context.project_pack is not None
    assert context.project_pack.version == "2026.09"
    assert {item.capability_id for item in context.context_manifest.capability_summaries} == {
        "run.lookup",
        "test.run",
    }


def test_provenance_index_traces_and_invalidates_only_downstream(repository):
    investigation, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective=Objective(objective_id="prov", description="trace provenance"),
        ),
        "next-provenance",
    )
    now = datetime.now(timezone.utc)
    base = EvidenceRecord(
        evidence_id="ev-base",
        tenant_id="studio-a",
        project_id="game-a",
        artifact_ref="artifact://studio-a/game-a/base",
        kind="trace",
        mime_type="application/json",
        sha256="1" * 64,
        producer="test",
        producer_version="1",
        collected_at=now,
        locator=EvidenceLocator(kind="sample_window", value="all"),
        quality="verified",
        source_type="user_upload",
        source_evidence_id="upload-base",
    )
    derived = base.model_copy(
        update={
            "evidence_id": "ev-derived",
            "artifact_ref": "artifact://studio-a/game-a/derived",
            "sha256": "2" * 64,
            "source_evidence_id": "upload-derived",
            "derived_from": ["ev-base"],
            "parser_version": "parser-2",
        }
    )
    service = EvidenceService(repository)
    service.ingest(investigation.investigation_id, base)
    service.ingest(investigation.investigation_id, derived)
    repository.add_fact(
        Fact(
            fact_id="fact-growth",
            investigation_id=investigation.investigation_id,
            statement="growth observed",
            evidence_ids=["ev-derived"],
            extractor="metric",
            extractor_version="2",
        )
    )
    index = ProvenanceIndex(repository)
    assert index.sources(investigation.investigation_id, "fact-growth") == [
        "ev-derived",
        "ev-base",
    ]
    invalidation = index.invalidate(investigation.investigation_id, "ev-base")
    assert invalidation.affected_node_ids == ["ev-derived", "fact-growth"]
    assert invalidation.recompute_order == ["ev-derived", "fact-growth"]
    assert invalidation.cycles == []

    first_report = ReportBuilder(repository).build(investigation.investigation_id, "first revision")
    ReportService(repository).publish(first_report)
    claim_node = f"claim:{investigation.investigation_id}:1:claim-fact-growth"
    summary_node = f"summary:report:{investigation.investigation_id}:1"
    graph = index.build(investigation.investigation_id)
    assert graph.nodes[claim_node].node_type == "claim"
    assert graph.nodes[summary_node].node_type == "summary"
    assert set(index.sources(investigation.investigation_id, claim_node)) == {
        "ev-base",
        "ev-derived",
        "fact-growth",
    }

    corrected = base.model_copy(
        update={
            "evidence_id": "ev-base-v2",
            "artifact_ref": "artifact://studio-a/game-a/base-v2",
            "sha256": "9" * 64,
            "source_revision": 2,
        }
    )
    service.ingest(investigation.investigation_id, corrected)
    stale_graph = index.build(investigation.investigation_id)
    assert stale_graph.nodes[claim_node].stale
    assert stale_graph.nodes[summary_node].stale
    assert [
        item.evidence_id
        for item in repository.list_current_evidence(investigation.investigation_id)
    ] == ["ev-base-v2"]

    second_report = ReportBuilder(repository).build(
        investigation.investigation_id, "source corrected"
    )
    assert second_report.revision == 2
    ReportService(repository).publish(second_report)


def test_projection_checkpoints_gate_stale_read_models(repository):
    investigation, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective=Objective(objective_id="projection", description="projection freshness"),
        ),
        "next-projection-checkpoint",
    )
    coordinator = ProjectionCoordinator(repository)
    try:
        coordinator.assert_fresh(investigation.investigation_id)
    except ProjectionLagError as error:
        assert "objective" in str(error)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("missing projection checkpoints were accepted")

    checkpoints = coordinator.refresh(investigation.investigation_id)
    assert {item.projection_name for item in checkpoints} == {
        "objective",
        "provenance",
        "summary",
    }
    coordinator.assert_fresh(investigation.investigation_id)

    repository.record_investigation_event(
        investigation.investigation_id, "test.projection_changed", {"revision": 2}
    )
    try:
        coordinator.assert_fresh(investigation.investigation_id)
    except ProjectionLagError as error:
        assert "cursor" in str(error)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("lagging projection checkpoints were accepted")

    coordinator.refresh(investigation.investigation_id)
    coordinator.assert_fresh(investigation.investigation_id)


def test_new_source_revision_invalidates_old_facts_and_reasoning_context(repository):
    investigation, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective=Objective(objective_id="revision", description="use latest evidence"),
        ),
        "next-source-revision",
    )
    now = datetime.now(timezone.utc)
    original = EvidenceRecord(
        evidence_id="ev-revision-1",
        tenant_id="studio-a",
        project_id="game-a",
        artifact_ref="artifact://studio-a/game-a/revision-1",
        kind="metric",
        mime_type="application/json",
        sha256="3" * 64,
        producer="test",
        producer_version="1",
        collected_at=now,
        locator=EvidenceLocator(kind="sample_window", value="all"),
        quality="verified",
        source_type="user_upload",
        source_evidence_id="stable-source",
        source_revision=1,
    )
    evidence = EvidenceService(repository)
    evidence.ingest(investigation.investigation_id, original)
    repository.add_fact(
        Fact(
            fact_id="fact-old-revision",
            investigation_id=investigation.investigation_id,
            statement="old value",
            evidence_ids=[original.evidence_id],
            extractor="test",
            extractor_version="1",
        )
    )
    repository.save_hypothesis(
        Hypothesis(
            hypothesis_id="hyp-old-source",
            investigation_id=investigation.investigation_id,
            revision=1,
            statement="old source supports the hypothesis",
            scope="current-build",
            supporting_evidence_ids=[original.evidence_id],
            falsifiable_prediction="a corrected source retains the value",
            status="supported",
        )
    )
    memory = CaseMemoryRecord(
        case_id="case-stale-source",
        revision=1,
        tenant_id="studio-a",
        project_id="game-a",
        status=CaseMemoryStatus.ACTIVE,
        confidence="medium",
        content=CaseMemoryContent(
            title="old source conclusion",
            symptoms=["old value"],
            confirmed_outcome="old source appeared conclusive",
        ),
        applicability=CaseApplicability(),
        source_investigation_id=investigation.investigation_id,
        source_report_revision=1,
        source_report_hash="5" * 64,
        source_evidence_ids=[original.evidence_id],
        created_by="tester",
        reviewed_by="reviewer",
        state_reason="test active case",
        state_changed_by="reviewer",
    )
    repository.save_case_memory(memory, expected_previous_revision=0)
    replacement = original.model_copy(
        update={
            "evidence_id": "ev-revision-2",
            "artifact_ref": "artifact://studio-a/game-a/revision-2",
            "sha256": "4" * 64,
            "source_revision": 2,
        }
    )
    evidence.ingest(investigation.investigation_id, replacement)

    assert repository.superseded_evidence_ids(investigation.investigation_id) == {"ev-revision-1"}
    assert [
        item.evidence_id
        for item in repository.list_current_evidence(investigation.investigation_id)
    ] == ["ev-revision-2"]
    assert repository.list_current_facts(investigation.investigation_id) == []
    assert repository.get_case_memory(memory.case_id).status == CaseMemoryStatus.RETRACTED
    hypothesis = repository.list_hypotheses(investigation.investigation_id)[0]
    assert hypothesis.revision == 2
    assert hypothesis.status == "unresolved"
    assert hypothesis.supporting_evidence_ids == []
    context = ContextBuilder(repository, CapabilityRegistry(repository)).build(
        investigation.investigation_id,
        scopes=set(),
    )
    assert [item.evidence_id for item in context.evidence] == ["ev-revision-2"]
    assert context.facts == []
    assert "superseded_evidence_omitted_from_reasoning_context" in context.limitations
    graph = ProvenanceIndex(repository).build(investigation.investigation_id)
    assert graph.nodes["ev-revision-1"].stale is True


def test_kernel_persists_decision_envelope_and_read_only_replay(repository):
    investigation, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective=Objective(objective_id="audit", description="audit decision"),
        ),
        "next-audit",
    )
    context = ContextBuilder(repository, CapabilityRegistry(repository)).build(
        investigation.investigation_id,
        scopes=set(),
    )
    kernel = InvestigationKernel(repository)
    prepared = kernel.prepare_round(
        context,
        plan_revision=1,
        model_name="test-profile",
        prompt_version="prompt-v2",
    )
    assert prepared.snapshot is not None
    record = kernel.accept_decision(
        prepared.snapshot,
        NextStep(
            kind="report",
            objective_id="audit",
            expected_observation="bounded report",
            reason_summary="no executable capability",
        ),
        model_profile_version="2",
        raw_output_ref="sha256:" + "a" * 64,
    )

    assert record.envelope is not None
    assert record.envelope.context_manifest is not None
    assert record.envelope.release_unit is not None
    assert record.envelope.release_unit.release_id.startswith("release-")
    assert record.envelope.release_unit.components["prompt"] == "prompt-v2"
    assert record.envelope.model_profile_version == "2"
    replay = ReadOnlyReplayService(repository)
    assert replay.state_replay(investigation.investigation_id).decisions == (record,)
    assert replay.integrity(investigation.investigation_id).complete


def test_resource_scheduler_is_fair_and_fences_old_holders():
    scheduler = ResourceScheduler(
        project_device_minute_limits={"a": 5, "b": 5},
        max_concurrent_per_project=1,
    )
    now = datetime.now(timezone.utc)
    scheduler.enqueue(
        ResourceRequest(
            project_id="a",
            task_id="task-a",
            node_id="node-a",
            resource_domain="device-a",
            device_minutes=2,
            ttl_seconds=10,
        )
    )
    scheduler.enqueue(
        ResourceRequest(
            project_id="b",
            task_id="task-b",
            node_id="node-b",
            resource_domain="device-b",
            device_minutes=1,
        )
    )
    first = scheduler.admit_next(now=now)
    assert first is not None and first.project_id == "a"
    scheduler.release(first.ticket_id, consumed_device_minutes=2)
    second = scheduler.admit_next(now=now)
    assert second is not None and second.project_id == "b"

    scheduler.enqueue(
        ResourceRequest(
            project_id="a",
            task_id="task-a2",
            node_id="node-a2",
            resource_domain="device-a",
            device_minutes=1,
            ttl_seconds=1,
        )
    )
    scheduler.release(second.ticket_id, consumed_device_minutes=1)
    renewed = scheduler.admit_next(now=now)
    assert renewed is not None
    assert renewed.fencing_token > first.fencing_token
    assert not scheduler.accepts_fencing_token("device-a", first.fencing_token)
    assert scheduler.expire(now=now + timedelta(seconds=2)) == [renewed.ticket_id]


def test_durable_resource_scheduler_recovers_and_renews_expired_ticket(repository):
    now = datetime.now(timezone.utc)
    first_scheduler = DurableResourceScheduler(
        repository,
        project_device_minute_limits={"game-a": 5},
    )
    request = ResourceRequest(
        project_id="game-a",
        task_id="inv-durable",
        node_id="action-durable",
        plan_step_id="collect-evidence",
        plan_node_revision=3,
        resource_domain="device-a",
        device_minutes=2,
        ttl_seconds=1,
    )
    first = first_scheduler.acquire(request, now=now)
    assert first is not None
    assert first.plan_step_id == "collect-evidence"
    assert first.plan_node_revision == 3

    restarted = DurableResourceScheduler(
        repository,
        project_device_minute_limits={"game-a": 5},
    )
    recovered = restarted.acquire(request, now=now + timedelta(milliseconds=500))
    assert recovered == first
    renewed = restarted.acquire(request, now=now + timedelta(seconds=2))

    assert renewed is not None
    assert renewed.ticket_id != first.ticket_id
    assert renewed.fencing_token == first.fencing_token + 1
    assert not restarted.accepts_fencing_token("device-a", first.fencing_token)
    assert restarted.release(renewed.ticket_id, consumed_device_minutes=2)
    assert not restarted.release(renewed.ticket_id, consumed_device_minutes=2)


def test_project_pack_registry_persists_immutable_versions(repository):
    registry = ProjectPackRegistry(repository)
    pack = ProjectPack(
        pack_id="game-a-default",
        version="2026.09",
        tenant_id="studio-a",
        project_id="game-a",
        terminology={"frame": "rendered frame"},
        reviewed_by="qa-lead",
    )
    digest = registry.register(pack)

    restarted = ProjectPackRegistry(repository)
    assert restarted.resolve("studio-a", "game-a", "2026.09") == pack
    assert restarted.register(pack) == digest

    changed = pack.model_copy(update={"terminology": {"frame": "changed"}})
    try:
        restarted.register(changed)
    except ValueError as error:
        assert "immutable" in str(error)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("mutable project pack version was accepted")


def test_analysis_recipe_runs_only_registered_validated_dag(repository, settings):
    runner = AnalysisRecipeRunner(
        {
            ("sum", "1"): lambda material, _params: sum(material["inputs"]["values"]),
            ("wrap", "1"): lambda material, _params: {
                "total": material["dependencies"]["aggregate"]
            },
        },
        {"non-negative": lambda value: value["total"] >= 0},
    )
    recipe = AnalysisRecipe(
        recipe_id="aggregate-errors",
        version="1",
        input_types=["number_list"],
        steps=[
            AnalysisRecipeStep(
                step_id="aggregate",
                operation="aggregate",
                component="sum",
                component_version="1",
                output_type="number",
            ),
            AnalysisRecipeStep(
                step_id="result",
                operation="verify",
                dependencies=["aggregate"],
                component="wrap",
                component_version="1",
                output_type="summary",
            ),
        ],
        output_schema={
            "type": "object",
            "properties": {"total": {"type": "number"}},
            "required": ["total"],
            "additionalProperties": False,
        },
        verifier="non-negative",
    )

    assert runner.validate(recipe) == []
    assert runner.run(recipe, {"values": [1, 2, 3]}) == {"total": 6}

    investigation, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective=Objective(objective_id="recipe", description="analyze bounded data"),
        ),
        "next-recipe-service",
    )
    artifacts = LocalArtifactStore(settings.artifact_root)
    stored = artifacts.put_bytes("studio-a", "game-a", b"[1,2,3]")
    source = EvidenceRecord(
        evidence_id="ev-recipe-input",
        tenant_id="studio-a",
        project_id="game-a",
        artifact_ref=stored.ref,
        kind="numbers",
        mime_type="application/json",
        sha256=stored.sha256,
        producer="test",
        producer_version="1",
        collected_at=datetime.now(timezone.utc),
        locator=EvidenceLocator(kind="sample_window", value="all"),
        quality="verified",
        source_type="user_upload",
        source_evidence_id="recipe-input",
    )
    EvidenceService(repository).ingest(investigation.investigation_id, source)
    service_runner = AnalysisRecipeRunner(
        {
            ("count", "1"): lambda material, _params: {
                "total": len(material["inputs"]["evidence"]["ev-recipe-input"])
            }
        },
        {"non-negative": lambda value: value["total"] >= 0},
    )
    service_recipe = recipe.model_copy(
        update={
            "steps": [
                AnalysisRecipeStep(
                    step_id="result",
                    operation="aggregate",
                    component="count",
                    component_version="1",
                    output_type="summary",
                )
            ]
        }
    )
    execution = AnalysisRecipeService(repository, artifacts, service_runner).execute(
        investigation.investigation_id,
        service_recipe,
        [source.evidence_id],
    )
    assert execution.output == {"total": 3}
    assert execution.evidence.derived_from == [source.evidence_id]
    assert ProvenanceIndex(repository).sources(
        investigation.investigation_id,
        execution.evidence.evidence_id,
    ) == [source.evidence_id]
