from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from game_test_agent.analysis import AnalysisSandboxService
from game_test_agent.analysis.code_execution import build_analysis_sandbox, build_code_execution
from game_test_agent.agents import Investigator
from game_test_agent.activities import ActionService, ReasoningService
from game_test_agent.capabilities import CapabilityRegistry
from game_test_agent.config import Settings
from game_test_agent.audit import ReadOnlyReplayService
from game_test_agent.context import ContextAccess, ContextBroker
from game_test_agent.contracts.models import (
    BuildRef,
    AnalysisJobRecord,
    AnalysisJobSpec,
    AnalysisJobView,
    AnalysisResultSubmission,
    CaseMemoryDraft,
    CaseMemoryRecord,
    CaseMemoryTransition,
    ContextAccessScope,
    ContextManifest,
    ContextSummary,
    DerivedFactCandidate,
    DecisionOutcomeRecord,
    EvidenceExcerpt,
    EvidenceSearchFilters,
    EvidenceSearchHit,
    EventPage,
    ExternalRunSummary,
    HistoricalCaseReference,
    InputCommand,
    InvestigationDetail,
    InvestigationRequest,
    InvestigationStatus,
    InvestigationView,
    ObjectiveIR,
    PlanCandidate,
    PlanStatusSummary,
    ProjectPack,
    ReopenRequest,
    SandboxOperationRef,
    SandboxOperationView,
)
from game_test_agent.memory import CaseMemoryService, ProjectPackRegistry
from game_test_agent.reports import ReportService
from game_test_agent.storage import (
    ConflictError,
    NotFoundError,
    Repository,
    create_artifact_store,
    create_repository,
)
from game_test_agent.evidence import ContextBuilder, EvidenceManifestIngestor
from game_test_agent.mcp_gateway import ProviderManager, load_provider_configs
from game_test_agent.planning import ActionPlanner, ObjectiveCompiler
from game_test_agent.policies import PolicyEngine
from game_test_agent.provenance import ProvenanceIndex
from game_test_agent.workflows import (
    DeferredWorkflowBackend,
    InvestigationController,
    LocalWorkflowBackend,
    WorkflowBackend,
)
from game_test_agent.api.auth import Principal, build_principal_dependency
from game_test_agent.api.workspace import install_workspace
from game_test_agent.events.local_bridge import LocalEventBridge, dispatch_local_commands
from game_test_agent.events.router_recovery import RouterRecovery
from game_test_agent.observability import RequestContextMiddleware
from game_test_agent.runtime.execution import ExecutionLimits
from game_test_agent.observability.tracing import InvestigationTracer


@dataclass(slots=True)
class Container:
    settings: Settings
    repository: Repository
    registry: CapabilityRegistry
    reports: ReportService
    workflows: WorkflowBackend
    context_access: ContextAccess
    case_memory: CaseMemoryService
    analysis_sandbox: AnalysisSandboxService | None
    execution_limits: ExecutionLimits | None = None


class CancelRequest(BaseModel):
    command_id: str


class DerivedFactProposalRequest(BaseModel):
    output_evidence_id: str
    statement: str
    data: dict[str, Any] = Field(default_factory=dict)


class DerivedFactVerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accepted: bool
    reason: str = Field(min_length=1, max_length=4_000)


class ObjectiveCompilationResponse(BaseModel):
    objective_ir: ObjectiveIR
    candidates: list[PlanCandidate]
    selected_candidate_id: str | None = None


class ProvenanceTraceResponse(BaseModel):
    target_id: str
    source_ids: list[str]


class AuditIntegrityResponse(BaseModel):
    investigation_id: str
    complete: bool
    issues: list[str]


async def require_principal(request: Request) -> Principal:
    return await request.app.state.principal_authenticator(request)


def create_app(
    settings: Settings | None = None,
    repository: Repository | None = None,
    *,
    principal_authenticator: Any | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.validate()
    repository = repository or create_repository(settings)
    registry = CapabilityRegistry(repository)
    execution_limits = ExecutionLimits.from_settings(settings)
    tracer = InvestigationTracer(repository)
    artifact_store = create_artifact_store(settings)
    reports = ReportService(repository, artifact_store)
    context_access = ContextAccess(repository, artifact_store)
    context_broker = ContextBroker(
        ContextBuilder(
            repository,
            registry,
            window_limit_tokens=settings.model_context_window_tokens,
            reserved_output_tokens=settings.model_max_output_tokens,
        ),
        registry,
        context_access,
        ProjectPackRegistry(repository),
    )
    provenance = ProvenanceIndex(repository)
    replay = ReadOnlyReplayService(repository)
    objective_compiler = ObjectiveCompiler()
    case_memory = CaseMemoryService(repository)
    analysis_sandbox = build_analysis_sandbox(settings, repository, artifact_store)
    code_execution = build_code_execution(settings, repository, artifact_store, analysis_sandbox)
    context_broker.builder.code_execution = code_execution
    workflows: WorkflowBackend
    providers: ProviderManager | None = None
    provider_configs = load_provider_configs(settings.mcp_providers_json)
    if provider_configs:
        providers = ProviderManager(registry, provider_configs, native_tools=settings.mcp_native_tools)
    if settings.workflow_backend == "temporal":
        workflows = DeferredWorkflowBackend()
    else:
        investigator = Investigator(
            settings.model_name,
            settings.enable_model,
            model_api=settings.model_api,
            output_mode=settings.model_output_mode,
            timeout_seconds=settings.model_timeout_seconds,
            max_output_tokens=settings.model_max_output_tokens,
            reasoning_effort=settings.model_reasoning_effort,
            extra_body=settings.model_extra_body,
        )
        controller = None
        if providers is not None:
            controller = InvestigationController(
                repository,
                registry,
                ReasoningService(repository, investigator, execution_limits=execution_limits),
                ActionPlanner(
                    settings.model_name,
                    settings.enable_model,
                    model_api=settings.model_api,
                    output_mode=settings.model_output_mode,
                    timeout_seconds=settings.model_timeout_seconds,
                    max_output_tokens=settings.model_max_output_tokens,
                    reasoning_effort=settings.model_reasoning_effort,
                    extra_body=settings.model_extra_body,
                    execution_limits=execution_limits,
                    tracer=tracer,
                ),
                ActionService(
                    repository,
                    PolicyEngine(repository, registry),
                    providers.dispatcher,
                ),
                EvidenceManifestIngestor(repository, artifact_store),
                reports,
                provider_lookup=providers.provider,
                discover=providers.discover_all,
                context_window_tokens=settings.model_context_window_tokens,
                context_reserved_output_tokens=settings.model_max_output_tokens,
                code_execution=code_execution,
            )
        if providers is not None and settings.mcp_native_tools:
            from game_test_agent.workflows.native_mcp import NativeMcpController
            controller = NativeMcpController(settings, repository, providers, artifact_store, reports,
                                             code_execution=code_execution, execution_limits=execution_limits)
        workflows = LocalWorkflowBackend(
            repository,
            registry,
            investigator,
            reports,
            controller=controller,
            context_window_tokens=settings.model_context_window_tokens,
            context_reserved_output_tokens=settings.model_max_output_tokens,
            execution_limits=execution_limits,
        )
    container = Container(
        settings,
        repository,
        registry,
        reports,
        workflows,
        context_access,
        case_memory,
        analysis_sandbox,
        execution_limits,
    )
    principal_authenticator = principal_authenticator or build_principal_dependency(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.container = container
        bridge = (
            LocalEventBridge(settings, repository, workflows)
            if settings.enable_local_event_bridge
            else None
        )
        app.state.event_bridge = bridge
        app.state.event_bridge_status = "connecting" if bridge else "disabled"
        async def connect_bridge():
            while True:
                try:
                    await bridge.start()
                    app.state.event_bridge_status = "ready"
                    return
                except Exception as error:
                    app.state.event_bridge_status = "unavailable"
                    logging.getLogger(__name__).warning("event bridge unavailable (%s)", type(error).__name__)
                    await asyncio.sleep(5)

        bridge_task = None
        command_task = None
        try:
            app.state.router_recovery.start()
            if isinstance(workflows, LocalWorkflowBackend):
                workflows.recover()
                command_task = asyncio.create_task(
                    dispatch_local_commands(settings, repository, workflows), name="local-command-dispatch")
            if bridge is not None:
                bridge_task = asyncio.create_task(connect_bridge(), name="connect-local-event-bridge")
            yield
        finally:
            await app.state.router_recovery.close()
            if command_task is not None:
                command_task.cancel()
                await asyncio.gather(command_task, return_exceptions=True)
            if bridge_task is not None:
                bridge_task.cancel()
                await asyncio.gather(bridge_task, return_exceptions=True)
            if bridge is not None:
                await bridge.close()
            await workflows.close()
            if providers is not None:
                await providers.close()
            repository.close()

    app = FastAPI(
        title="Game Test Agent",
        version="0.1.0",
        description="Evidence-driven game test investigation control plane",
        lifespan=lifespan,
    )
    app.state.container = container
    app.state.principal_authenticator = principal_authenticator
    app.add_middleware(RequestContextMiddleware)
    install_workspace(app, container, providers, require_principal)
    app.state.router_recovery = RouterRecovery(app.state.routing_service,
        enabled=settings.enable_semantic_router and settings.enable_router_recovery,
        poll_seconds=settings.dispatcher_poll_seconds)

    @app.exception_handler(NotFoundError)
    async def not_found_handler(_request, error: NotFoundError):
        return JSONResponse(status_code=404, content={"detail": str(error)})

    @app.exception_handler(ConflictError)
    async def conflict_handler(_request, error: ConflictError):
        return JSONResponse(status_code=409, content={"detail": str(error)})

    def check_scope(view: InvestigationView, principal: Principal) -> None:
        if principal.tenant_id != view.request.tenant_id:
            raise HTTPException(status_code=403, detail="tenant scope mismatch")
        if principal.project_id != view.request.project_id:
            raise HTTPException(status_code=403, detail="project scope mismatch")

    def require_scope(principal: Principal, scope: str) -> None:
        if scope not in principal.scopes:
            raise HTTPException(status_code=403, detail=f"{scope} scope required")

    def require_analysis_sandbox() -> AnalysisSandboxService:
        if analysis_sandbox is None:
            raise HTTPException(status_code=503, detail="analysis sandbox is not configured")
        return analysis_sandbox

    def invoke_analysis(operation):
        try:
            return operation()
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    def analysis_view(record: AnalysisJobRecord) -> AnalysisJobView:
        operation = record.operation_ref
        return AnalysisJobView(
            spec=record.spec,
            spec_hash=record.spec_hash,
            state=record.state,
            operation_ref=(
                SandboxOperationView(
                    job_id=operation.job_id,
                    runner_id=operation.runner_id,
                    operation_id=operation.operation_id,
                    execution_status=operation.execution_status,
                )
                if operation
                else None
            ),
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def ready() -> dict[str, object]:
        if not repository.ping():
            raise HTTPException(status_code=503, detail="database unavailable")
        return {
            "status": "ready",
            "database": "ok",
            "schema_version": repository.schema_version(),
            "workflow_backend": settings.workflow_backend,
        }

    @app.get("/internal/metrics")
    async def internal_metrics(
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> dict[str, object]:
        if "agent:ops" not in principal.scopes:
            raise HTTPException(status_code=403, detail="agent:ops scope required")
        return {**repository.operational_metrics(principal.tenant_id, principal.project_id),
                "execution_capacity": execution_limits.snapshot((principal.tenant_id, principal.project_id))}

    @app.get("/v1/investigations/{investigation_id}/trace")
    async def investigation_trace(investigation_id: str, principal: Annotated[Principal, Depends(require_principal)]):
        check_scope(repository.get_investigation(investigation_id), principal)
        rows = repository.list_recent_events_by_types(investigation_id, ["trace.span"], limit=5000)
        return {"investigation_id": investigation_id, "spans": [r["data"] for r in rows],
                "complete": len(rows) < 5000, "limit": 5000}

    @app.get("/v1/investigations/{investigation_id}/trajectory")
    async def investigation_trajectory(investigation_id: str, principal: Annotated[Principal, Depends(require_principal)]):
        view = repository.get_investigation(investigation_id)
        check_scope(view, principal)
        from game_test_agent.audit.trajectory import export_trajectory
        return export_trajectory(repository, view)

    @app.post(
        "/v1/investigations",
        response_model=InvestigationView,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_investigation(
        request: InvestigationRequest,
        response: Response,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1, max_length=256)
        ],
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> InvestigationView:
        if (request.tenant_id, request.project_id) != (principal.tenant_id, principal.project_id):
            raise HTTPException(status_code=403, detail="request scope does not match principal")
        request = request.model_copy(update={"budget": settings.default_budget().model_copy(
            update=request.budget.model_dump(exclude_unset=True)
        )})
        try:
            view, created = repository.create_investigation(
                request, idempotency_key, principal_id=principal.principal_id, scopes=set(principal.scopes))
        except PermissionError as error:
            raise HTTPException(status_code=403, detail="historical reference scope or permission mismatch") from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        if not created:
            response.headers["Idempotent-Replay"] = "true"
        workflows.start(view.investigation_id)
        repository.record_audit(
            principal_id=principal.principal_id,
            tenant_id=principal.tenant_id,
            project_id=principal.project_id,
            action="investigation.create",
            target_id=view.investigation_id,
            metadata={"created": created},
        )
        return view

    @app.get("/v1/investigations/{investigation_id}", response_model=InvestigationDetail)
    async def get_investigation(
        investigation_id: str,
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> InvestigationDetail:
        view = repository.get_investigation(investigation_id)
        check_scope(view, principal)
        plan = repository.get_latest_plan(investigation_id)
        plan_summary = None
        if plan:
            statuses = [step.status for step in plan.steps]
            plan_summary = PlanStatusSummary(
                revision=plan.revision,
                total_steps=len(plan.steps),
                pending_steps=statuses.count("pending") + statuses.count("ready"),
                running_steps=statuses.count("running"),
                completed_steps=statuses.count("completed") + statuses.count("skipped"),
                failed_steps=statuses.count("failed"),
            )
        actions = repository.list_actions(investigation_id)
        external_runs = [
            ExternalRunSummary(
                action_id=item["action"]["action_id"],
                action_state=item["state"],
                operation_id=(item["operation_ref"] or {}).get("operation_id"),
                run_ref=(item["operation_ref"] or {}).get("run_ref"),
            )
            for item in actions
            if item["operation_ref"]
        ]
        gaps = sorted(
            {
                gap
                for hypothesis in repository.list_hypotheses(investigation_id)
                for gap in hypothesis.missing_evidence
            }
        )
        if not repository.list_current_evidence(investigation_id):
            gaps.append("no_evidence_ingested")
        return InvestigationDetail(
            **view.model_dump(),
            plan_summary=plan_summary,
            budget_usage=repository.budget_usage(investigation_id),
            model_usage=repository.model_usage(investigation_id),
            evidence_gaps=gaps,
            external_runs=external_runs,
            pending_cleanup_action_ids=repository.pending_cleanup_actions(investigation_id),
            pending_cleanup_resources=repository.pending_cleanup_resources(investigation_id),
        )

    @app.post("/v1/investigations/{investigation_id}/inputs", status_code=202)
    async def add_input(
        investigation_id: str,
        command: InputCommand,
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> dict[str, object]:
        view = repository.get_investigation(investigation_id)
        check_scope(view, principal)
        if command.expected_revision != view.revision:
            raise HTTPException(status_code=409, detail=f"expected revision {view.revision}")
        if view.status in {
            InvestigationStatus.COMPLETED,
            InvestigationStatus.CANCELED,
            InvestigationStatus.FAILED,
        }:
            raise HTTPException(status_code=409, detail="terminal investigation must be reopened")
        created = repository.enqueue_command(investigation_id, command.command_id, "input", command)
        if settings.workflow_backend == "local":
            repository.mark_command_sent(command.command_id)
            if created:
                workflows.start(investigation_id)
        repository.record_audit(
            principal_id=principal.principal_id,
            tenant_id=principal.tenant_id,
            project_id=principal.project_id,
            action="investigation.input",
            target_id=investigation_id,
            metadata={"command_id": command.command_id, "duplicate": not created},
        )
        return {"accepted": True, "duplicate": not created, "command_id": command.command_id}

    @app.post("/v1/investigations/{investigation_id}/cancel", status_code=202)
    async def cancel_investigation(
        investigation_id: str,
        command: CancelRequest,
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> dict[str, object]:
        view = repository.get_investigation(investigation_id)
        check_scope(view, principal)
        if view.status in {
            InvestigationStatus.COMPLETED,
            InvestigationStatus.CANCELED,
            InvestigationStatus.FAILED,
        }:
            repository.record_audit(
                principal_id=principal.principal_id,
                tenant_id=principal.tenant_id,
                project_id=principal.project_id,
                action="investigation.cancel",
                target_id=investigation_id,
                metadata={
                    "command_id": command.command_id,
                    "already_terminal": True,
                    "status": view.status,
                },
            )
            return {
                "accepted": True,
                "duplicate": False,
                "command_id": command.command_id,
                "already_terminal": True,
            }
        created = repository.enqueue_command(
            investigation_id, command.command_id, "cancel", command
        )
        if created:
            delivered = workflows.request_cancel(investigation_id)
            if delivered:
                repository.mark_command_sent(command.command_id)
        repository.record_audit(
            principal_id=principal.principal_id,
            tenant_id=principal.tenant_id,
            project_id=principal.project_id,
            action="investigation.cancel",
            target_id=investigation_id,
            metadata={"command_id": command.command_id, "duplicate": not created},
        )
        return {"accepted": True, "duplicate": not created, "command_id": command.command_id}

    @app.post(
        "/v1/investigations/{investigation_id}/reopen",
        response_model=InvestigationView,
        status_code=202,
    )
    async def reopen_investigation(
        investigation_id: str,
        request: ReopenRequest,
        response: Response,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1, max_length=256)
        ],
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> InvestigationView:
        parent = repository.get_investigation(investigation_id)
        check_scope(parent, principal)
        if parent.status not in {
            InvestigationStatus.COMPLETED,
            InvestigationStatus.CANCELED,
            InvestigationStatus.FAILED,
        }:
            raise HTTPException(
                status_code=409, detail="only terminal investigations can be reopened"
            )
        try:
            view, created = repository.reopen_investigation(
                investigation_id, request, idempotency_key,
                principal_id=principal.principal_id, scopes=set(principal.scopes))
        except PermissionError as error:
            raise HTTPException(status_code=403, detail="historical reference scope or permission mismatch") from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        if not created:
            response.headers["Idempotent-Replay"] = "true"
        workflows.start(view.investigation_id)
        repository.record_audit(
            principal_id=principal.principal_id,
            tenant_id=principal.tenant_id,
            project_id=principal.project_id,
            action="investigation.reopen",
            target_id=view.investigation_id,
            metadata={"parent_id": investigation_id, "created": created},
        )
        return view

    @app.get("/v1/investigations/{investigation_id}/events", response_model=EventPage)
    async def get_events(
        investigation_id: str,
        principal: Annotated[Principal, Depends(require_principal)],
        after: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> EventPage:
        check_scope(repository.get_investigation(investigation_id), principal)
        events = repository.list_events(investigation_id, after, limit)
        next_cursor = events[-1]["cursor"] if events else after
        return EventPage(events=events, next_cursor=next_cursor)

    @app.get(
        "/v1/investigations/{investigation_id}/context-manifest",
        response_model=ContextManifest,
    )
    async def get_context_manifest(
        investigation_id: str,
        principal: Annotated[Principal, Depends(require_principal)],
        step_id: Annotated[str | None, Query(max_length=256)] = None,
        intent: Annotated[str | None, Query(max_length=1000)] = None,
    ) -> ContextManifest:
        view = repository.get_investigation(investigation_id)
        check_scope(view, principal)
        context = context_broker.assemble(
            investigation_id,
            scopes=set(principal.scopes),
            step_id=step_id,
            intent=intent or view.request.objective.description,
        )
        if context.context_manifest is None:
            raise HTTPException(status_code=500, detail="context manifest was not generated")
        return context.context_manifest

    @app.get(
        "/v1/investigations/{investigation_id}/objective-compilation",
        response_model=ObjectiveCompilationResponse,
    )
    async def get_objective_compilation(
        investigation_id: str,
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> ObjectiveCompilationResponse:
        view = repository.get_investigation(investigation_id)
        check_scope(view, principal)
        current = repository.get_latest_plan(investigation_id)
        compiled = objective_compiler.compile(
            investigation_id,
            view.request,
            [
                item
                for item in registry.discover(view.request.project_id, scopes=set(principal.scopes))
                if item.provider_id
                in view.request.constraints.get("provider_allowlist", [item.provider_id])
            ],
            revision=current.revision if current is not None else 1,
        )
        return ObjectiveCompilationResponse(
            objective_ir=compiled.objective_ir,
            candidates=compiled.candidates,
            selected_candidate_id=(
                compiled.selected.candidate_id if compiled.selected is not None else None
            ),
        )

    @app.get(
        "/v1/investigations/{investigation_id}/provenance/{target_id}",
        response_model=ProvenanceTraceResponse,
    )
    async def get_provenance_trace(
        investigation_id: str,
        target_id: str,
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> ProvenanceTraceResponse:
        check_scope(repository.get_investigation(investigation_id), principal)
        return ProvenanceTraceResponse(
            target_id=target_id,
            source_ids=provenance.sources(investigation_id, target_id),
        )

    @app.get(
        "/v1/investigations/{investigation_id}/audit/integrity",
        response_model=AuditIntegrityResponse,
    )
    async def get_audit_integrity(
        investigation_id: str,
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> AuditIntegrityResponse:
        check_scope(repository.get_investigation(investigation_id), principal)
        report = replay.integrity(investigation_id)
        return AuditIntegrityResponse(
            investigation_id=report.investigation_id,
            complete=report.complete,
            issues=list(report.issues),
        )

    @app.get(
        "/v1/investigations/{investigation_id}/audit/outcomes",
        response_model=list[DecisionOutcomeRecord],
    )
    async def get_decision_outcomes(
        investigation_id: str,
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> list[DecisionOutcomeRecord]:
        check_scope(repository.get_investigation(investigation_id), principal)
        return repository.list_decision_outcomes(investigation_id)

    @app.post(
        "/v1/investigations/{investigation_id}/evidence/search",
        response_model=list[EvidenceSearchHit],
    )
    async def search_evidence(
        investigation_id: str,
        filters: EvidenceSearchFilters,
        principal: Annotated[Principal, Depends(require_principal)],
        query: Annotated[str | None, Query(max_length=1000)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> list[EvidenceSearchHit]:
        check_scope(repository.get_investigation(investigation_id), principal)
        return context_access.search_evidence(
            investigation_id,
            ContextAccessScope(
                tenant_id=principal.tenant_id,
                project_id=principal.project_id,
            ),
            filters=filters,
            query=query,
            limit=limit,
        )

    @app.get(
        "/v1/investigations/{investigation_id}/context-summary",
        response_model=ContextSummary,
    )
    async def get_context_summary(
        investigation_id: str,
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> ContextSummary:
        check_scope(repository.get_investigation(investigation_id), principal)
        summary = context_access.get_summary(
            investigation_id,
            ContextAccessScope(
                tenant_id=principal.tenant_id,
                project_id=principal.project_id,
            ),
        )
        if summary is None:
            raise HTTPException(status_code=404, detail="current context summary is unavailable")
        return summary

    @app.get(
        "/v1/investigations/{investigation_id}/evidence/{evidence_id}",
        response_model=EvidenceExcerpt,
    )
    async def read_evidence(
        investigation_id: str,
        evidence_id: str,
        principal: Annotated[Principal, Depends(require_principal)],
        byte_offset: Annotated[int, Query(ge=0)] = 0,
        max_bytes: Annotated[int, Query(ge=1, le=1024 * 1024)] = 64 * 1024,
    ) -> EvidenceExcerpt:
        check_scope(repository.get_investigation(investigation_id), principal)
        try:
            return context_access.read_evidence(
                investigation_id,
                evidence_id,
                ContextAccessScope(
                    tenant_id=principal.tenant_id,
                    project_id=principal.project_id,
                ),
                byte_offset=byte_offset,
                max_bytes=max_bytes,
            )
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except OSError as error:
            raise HTTPException(
                status_code=409, detail="artifact integrity check failed"
            ) from error

    @app.get("/v1/investigations/{investigation_id}/reports/{revision}")
    async def get_report(
        investigation_id: str,
        revision: int,
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> dict[str, object]:
        check_scope(repository.get_investigation(investigation_id), principal)
        report, markdown, digest = repository.get_report(investigation_id, revision)
        return {"report": report.model_dump(mode="json"), "markdown": markdown, "sha256": digest}

    @app.get("/v1/capabilities")
    async def list_capabilities(
        project_id: str,
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> list[dict[str, object]]:
        if project_id != principal.project_id:
            raise HTTPException(status_code=403, detail="project scope mismatch")
        return [
            item.model_dump(mode="json", by_alias=True)
            for item in registry.discover(project_id, scopes=set(principal.scopes))
        ]

    @app.post("/v1/project-packs", response_model=ProjectPack, status_code=status.HTTP_201_CREATED)
    async def register_project_pack(
        pack: ProjectPack,
        response: Response,
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> ProjectPack:
        require_scope(principal, "agent:project-pack:write")
        if (pack.tenant_id, pack.project_id) != (principal.tenant_id, principal.project_id):
            raise HTTPException(status_code=403, detail="project pack scope mismatch")
        existing = None
        try:
            existing = repository.get_project_pack(pack.tenant_id, pack.project_id, pack.version)
        except NotFoundError:
            pass
        repository.register_project_pack(pack)
        if existing is not None:
            response.status_code = status.HTTP_200_OK
            response.headers["Idempotent-Replay"] = "true"
        repository.record_audit(
            principal_id=principal.principal_id,
            tenant_id=principal.tenant_id,
            project_id=principal.project_id,
            action="project_pack.register",
            target_id=f"{pack.pack_id}@{pack.version}",
            metadata={"created": existing is None},
        )
        return pack

    @app.get("/v1/project-packs", response_model=list[ProjectPack])
    async def list_project_packs(
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> list[ProjectPack]:
        return repository.list_project_packs(principal.tenant_id, principal.project_id)

    @app.get("/v1/project-packs/{version}", response_model=ProjectPack)
    async def get_project_pack(
        version: str,
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> ProjectPack:
        return repository.get_project_pack(principal.tenant_id, principal.project_id, version)

    @app.post(
        "/v1/investigations/{investigation_id}/memory-candidates",
        response_model=CaseMemoryRecord,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_memory_candidate(
        investigation_id: str,
        draft: CaseMemoryDraft,
        response: Response,
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> CaseMemoryRecord:
        require_scope(principal, "agent:memory:write")
        check_scope(repository.get_investigation(investigation_id), principal)
        record, created = case_memory.create_candidate(
            investigation_id,
            draft,
            created_by=principal.principal_id,
        )
        if not created:
            response.status_code = status.HTTP_200_OK
            response.headers["Idempotent-Replay"] = "true"
        repository.record_audit(
            principal_id=principal.principal_id,
            tenant_id=principal.tenant_id,
            project_id=principal.project_id,
            action="case_memory.candidate.create",
            target_id=record.case_id,
            metadata={"source_investigation_id": investigation_id, "created": created},
        )
        return record

    @app.post(
        "/v1/memory/cases/{case_id}/transitions",
        response_model=CaseMemoryRecord,
    )
    async def transition_memory_case(
        case_id: str,
        transition: CaseMemoryTransition,
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> CaseMemoryRecord:
        require_scope(principal, "agent:memory:review")
        current = repository.get_case_memory(case_id)
        if (current.tenant_id, current.project_id) != (
            principal.tenant_id,
            principal.project_id,
        ):
            raise HTTPException(status_code=403, detail="case memory scope mismatch")
        record = case_memory.transition(case_id, transition, actor=principal.principal_id)
        repository.record_audit(
            principal_id=principal.principal_id,
            tenant_id=principal.tenant_id,
            project_id=principal.project_id,
            action="case_memory.transition",
            target_id=record.case_id,
            metadata={"revision": record.revision, "status": record.status},
        )
        return record

    @app.get("/v1/memory/cases/{case_id}", response_model=CaseMemoryRecord)
    async def get_memory_case(
        case_id: str,
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> CaseMemoryRecord:
        require_scope(principal, "agent:memory:review")
        record = repository.get_case_memory(case_id)
        if (record.tenant_id, record.project_id) != (
            principal.tenant_id,
            principal.project_id,
        ):
            raise HTTPException(status_code=403, detail="case memory scope mismatch")
        return record

    @app.get("/v1/memory/cases", response_model=list[HistoricalCaseReference])
    async def search_memory_cases(
        principal: Annotated[Principal, Depends(require_principal)],
        query: Annotated[str | None, Query(max_length=2000)] = None,
        engine_family: Annotated[str | None, Query(max_length=128)] = None,
        engine_version: Annotated[str | None, Query(max_length=256)] = None,
        build_id: Annotated[str | None, Query(max_length=256)] = None,
        build_revision: Annotated[str | None, Query(max_length=256)] = None,
        environment_fingerprint: Annotated[str | None, Query(max_length=512)] = None,
        limit: Annotated[int, Query(ge=1, le=50)] = 5,
    ) -> list[HistoricalCaseReference]:
        if build_revision and not build_id:
            raise HTTPException(status_code=422, detail="build_revision requires build_id")
        return case_memory.search(
            principal.tenant_id,
            principal.project_id,
            query=query,
            engine_family=engine_family,
            engine_version=engine_version,
            build_ref=(
                BuildRef(id=build_id, revision=build_revision) if build_id is not None else None
            ),
            environment_fingerprint=environment_fingerprint,
            limit=limit,
        )

    @app.post(
        "/v1/analysis/jobs",
        response_model=AnalysisJobView,
        status_code=status.HTTP_201_CREATED,
    )
    async def prepare_analysis_job(
        spec: AnalysisJobSpec,
        response: Response,
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> AnalysisJobView:
        require_scope(principal, "agent:analysis:submit")
        check_scope(repository.get_investigation(spec.investigation_id), principal)
        if (spec.tenant_id, spec.project_id) != (
            principal.tenant_id,
            principal.project_id,
        ):
            raise HTTPException(status_code=403, detail="analysis job scope mismatch")
        authorized_spec = spec.model_copy(update={"created_by": principal.principal_id})
        record, created = invoke_analysis(
            lambda: require_analysis_sandbox().prepare(authorized_spec)
        )
        if not created:
            response.status_code = status.HTTP_200_OK
            response.headers["Idempotent-Replay"] = "true"
        repository.record_audit(
            principal_id=principal.principal_id,
            tenant_id=principal.tenant_id,
            project_id=principal.project_id,
            action="analysis.prepare",
            target_id=record.spec.job_id,
            metadata={"created": created, "spec_hash": record.spec_hash},
        )
        return analysis_view(record)

    @app.get("/v1/analysis/jobs/{job_id}", response_model=AnalysisJobView)
    async def get_analysis_job(
        job_id: str,
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> AnalysisJobView:
        record = repository.get_analysis_job(job_id)
        if (record.spec.tenant_id, record.spec.project_id) != (
            principal.tenant_id,
            principal.project_id,
        ):
            raise HTTPException(status_code=403, detail="analysis job scope mismatch")
        return analysis_view(record)

    @app.post(
        "/v1/analysis/jobs/{job_id}/operations",
        response_model=AnalysisJobView,
    )
    async def bind_analysis_operation(
        job_id: str,
        operation: SandboxOperationRef,
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> AnalysisJobView:
        require_scope(principal, "agent:analysis:runner")
        current = repository.get_analysis_job(job_id)
        if (current.spec.tenant_id, current.spec.project_id) != (
            principal.tenant_id,
            principal.project_id,
        ):
            raise HTTPException(status_code=403, detail="analysis job scope mismatch")
        updated = invoke_analysis(
            lambda: require_analysis_sandbox().bind_operation(job_id, operation)
        )
        repository.record_audit(
            principal_id=principal.principal_id,
            tenant_id=principal.tenant_id,
            project_id=principal.project_id,
            action="analysis.operation.update",
            target_id=job_id,
            metadata={
                "operation_id": operation.operation_id,
                "execution_status": operation.execution_status,
            },
        )
        return analysis_view(updated)

    @app.post("/v1/analysis/jobs/{job_id}/results")
    async def record_analysis_result(
        job_id: str,
        submission: AnalysisResultSubmission,
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> dict[str, Any]:
        require_scope(principal, "agent:analysis:runner")
        current = repository.get_analysis_job(job_id)
        if (current.spec.tenant_id, current.spec.project_id) != (
            principal.tenant_id,
            principal.project_id,
        ):
            raise HTTPException(status_code=403, detail="analysis job scope mismatch")
        if submission.operation.job_id != job_id:
            raise HTTPException(status_code=422, detail="analysis result job mismatch")
        snapshot, evidence = invoke_analysis(
            lambda: require_analysis_sandbox().record_result(
                submission.operation,
                submission.result,
            )
        )
        repository.record_audit(
            principal_id=principal.principal_id,
            tenant_id=principal.tenant_id,
            project_id=principal.project_id,
            action="analysis.result.record",
            target_id=job_id,
            metadata={
                "result_revision": submission.result.result_revision,
                "execution_status": submission.result.execution_status,
                "output_count": len(evidence),
            },
        )
        return {
            "snapshot": snapshot.model_dump(mode="json"),
            "evidence": [item.model_dump(mode="json") for item in evidence],
        }

    @app.post(
        "/v1/analysis/jobs/{job_id}/fact-candidates",
        response_model=DerivedFactCandidate,
        status_code=status.HTTP_201_CREATED,
    )
    async def propose_analysis_fact(
        job_id: str,
        request: DerivedFactProposalRequest,
        response: Response,
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> DerivedFactCandidate:
        require_scope(principal, "agent:analysis:verify")
        current = repository.get_analysis_job(job_id)
        if (current.spec.tenant_id, current.spec.project_id) != (
            principal.tenant_id,
            principal.project_id,
        ):
            raise HTTPException(status_code=403, detail="analysis job scope mismatch")
        candidate, created = invoke_analysis(
            lambda: require_analysis_sandbox().propose_fact(
                job_id,
                output_evidence_id=request.output_evidence_id,
                statement=request.statement,
                data=request.data,
            )
        )
        if not created:
            response.status_code = status.HTTP_200_OK
            response.headers["Idempotent-Replay"] = "true"
        repository.record_audit(
            principal_id=principal.principal_id,
            tenant_id=principal.tenant_id,
            project_id=principal.project_id,
            action="analysis.fact_candidate.create",
            target_id=candidate.candidate_id,
            metadata={"job_id": job_id, "created": created},
        )
        return candidate

    @app.post(
        "/v1/analysis/fact-candidates/{candidate_id}/verification",
        response_model=DerivedFactCandidate,
    )
    async def verify_analysis_fact(
        candidate_id: str,
        request: DerivedFactVerificationRequest,
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> DerivedFactCandidate:
        require_scope(principal, "agent:analysis:verify")
        candidate = repository.get_derived_fact_candidate(candidate_id)
        check_scope(repository.get_investigation(candidate.investigation_id), principal)
        verified = invoke_analysis(
            lambda: require_analysis_sandbox().verify_fact(
                candidate_id,
                accepted=request.accepted,
                verifier=principal.principal_id,
                reason=request.reason,
            )
        )
        repository.record_audit(
            principal_id=principal.principal_id,
            tenant_id=principal.tenant_id,
            project_id=principal.project_id,
            action="analysis.fact_candidate.verify",
            target_id=candidate_id,
            metadata={"status": verified.status, "verifier": principal.principal_id},
        )
        return verified

    @app.post("/v1/analysis/jobs/{job_id}/cleanup", status_code=204)
    async def confirm_analysis_cleanup(
        job_id: str,
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> Response:
        require_scope(principal, "agent:analysis:runner")
        current = repository.get_analysis_job(job_id)
        if (current.spec.tenant_id, current.spec.project_id) != (
            principal.tenant_id,
            principal.project_id,
        ):
            raise HTTPException(status_code=403, detail="analysis job scope mismatch")
        invoke_analysis(lambda: require_analysis_sandbox().confirm_cleanup(job_id))
        repository.record_audit(
            principal_id=principal.principal_id,
            tenant_id=principal.tenant_id,
            project_id=principal.project_id,
            action="analysis.cleanup.confirm",
            target_id=job_id,
            metadata={},
        )
        return Response(status_code=204)

    return app
