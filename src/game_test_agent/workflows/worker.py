from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker

from game_test_agent.activities import ActionService, ReasoningService
from game_test_agent.agents import Investigator
from game_test_agent.capabilities import CapabilityRegistry
from game_test_agent.config import Settings
from game_test_agent.context import ContextBroker
from game_test_agent.context.tool_loop import InvestigationToolLoop
from game_test_agent.analysis.code_execution import build_code_execution
from game_test_agent.contracts.models import (
    InvestigationStatus,
    NextStep,
    OperationRef,
    Resolution,
    RunResult,
)
from game_test_agent.evidence import ContextBuilder, EvidenceManifestIngestor
from game_test_agent.memory import ProjectPackRegistry
from game_test_agent.mcp_gateway import (
    ProviderManager,
    StandardToolError,
    load_provider_configs,
)
from game_test_agent.planning import (
    ActionPlanner, CandidateDesignService, CandidateSelectionService, DurableActionPlanner,
)
from game_test_agent.planning.service import PlanBudgetExceeded, PlanCorrectionRequired
from game_test_agent.observability import configure_logging
from game_test_agent.policies import PolicyEngine
from game_test_agent.reports import ReportBuilder, ReportService
from game_test_agent.runtime import InvestigationKernel
from game_test_agent.storage import (
    ConflictError,
    NotFoundError,
    Repository,
    create_artifact_store,
    create_repository,
)
from game_test_agent.workflows.temporal import CleanupWorkflow, InvestigationWorkflow
from game_test_agent.workflows.native_mcp import NativeMcpController
from game_test_agent.workflows.native_step import observe_step
from game_test_agent.runtime.execution import CapacityExceeded, ExecutionLimits
from game_test_agent.runtime.worker_ownership import WorkerOwnership, WorkerOwnershipLost, owned_activity
from game_test_agent.observability.tracing import InvestigationTracer
from game_test_agent.runtime.lifecycle import finalize_failure


class CoreActivities:
    def __init__(self, repository: Repository, settings: Settings) -> None:
        self.repository = repository
        self.ownership = WorkerOwnership(repository)
        self.settings = settings
        self.registry = CapabilityRegistry(repository)
        self.providers = ProviderManager(
            self.registry, load_provider_configs(settings.mcp_providers_json),
        )
        self.execution_limits = ExecutionLimits.from_settings(settings)
        self.tracer = InvestigationTracer(repository)
        self.investigator = Investigator(
            settings.model_name,
            settings.enable_model,
            model_api=settings.model_api,
            output_mode=settings.model_output_mode,
            timeout_seconds=settings.model_timeout_seconds,
            max_output_tokens=settings.model_max_output_tokens,
            reasoning_effort=settings.model_reasoning_effort,
            extra_body=settings.model_extra_body,
        )
        self.reasoning = ReasoningService(repository, self.investigator, execution_limits=self.execution_limits)
        self.action_planner = ActionPlanner(
            settings.model_name,
            settings.enable_model,
            model_api=settings.model_api,
            output_mode=settings.model_output_mode,
            timeout_seconds=settings.model_timeout_seconds,
            max_output_tokens=settings.model_max_output_tokens,
            reasoning_effort=settings.model_reasoning_effort,
            extra_body=settings.model_extra_body,
            execution_limits=self.execution_limits,
            tracer=self.tracer,
        )
        self.durable_action_planner = DurableActionPlanner(repository, self.action_planner)
        self.candidate_selection = CandidateSelectionService(repository)
        self.candidate_design = CandidateDesignService(repository, self.investigator, execution_limits=self.execution_limits)
        self.actions = ActionService(
            repository, PolicyEngine(repository, self.registry), self.providers.dispatcher
        )
        self.evidence = EvidenceManifestIngestor(repository, create_artifact_store(settings))
        self.code_execution = build_code_execution(settings, repository, self.evidence.artifacts)
        self.tool_loop = InvestigationToolLoop(repository, self.evidence.artifacts)
        self.reports = ReportService(repository, self.evidence.artifacts)
        self.report_builder = ReportBuilder(repository)
        self.context = ContextBuilder(
            repository,
            self.registry,
            window_limit_tokens=settings.model_context_window_tokens,
            reserved_output_tokens=settings.model_max_output_tokens,
            code_execution=self.code_execution,
        )
        self.context_broker = ContextBroker(
            self.context,
            self.registry,
            project_packs=ProjectPackRegistry(repository),
        )
        self.kernel = InvestigationKernel(repository)
        self.native_providers = ProviderManager(self.registry, load_provider_configs(settings.mcp_providers_json),
            native_tools=True) if settings.mcp_native_tools else None
        self.native = NativeMcpController(settings, repository, self.native_providers,
            self.evidence.artifacts, self.reports, code_execution=self.code_execution,
            execution_limits=self.execution_limits) if settings.mcp_native_tools else None

    @activity.defn(name="project_status")
    @owned_activity
    async def project_status(self, investigation_id: str, status: str) -> None:
        self.repository.transition(investigation_id, InvestigationStatus(status))

    @activity.defn(name="load_control_state")
    @owned_activity
    async def load_control_state(self, investigation_id: str) -> dict[str, Any]:
        view = self.repository.get_investigation(investigation_id)
        authorization = self.repository.get_authorization(investigation_id)
        return {
            "execution_mode": self.repository.investigation_execution_mode(investigation_id,
                "native_mcp" if self.settings.mcp_native_tools else "typed"),
            "budget": view.request.budget.model_dump(mode="json"),
            "created_at": view.created_at.isoformat(),
            "tool_reads": self.repository.budget_usage(investigation_id).get("tool_reads", 0),
            "principal_id": authorization["principal_id"],
            "scopes": authorization["scopes"],
            "policy_profile": authorization["policy_profile"],
        }

    @activity.defn(name="native_investigation_step")
    @owned_activity
    async def native_investigation_step(self, investigation_id: str, worker_steps: bool = False) -> dict[str, Any]:
        if self.native is None:
            raise ValueError("native investigation requires a worker configured for native MCP")
        view = self.repository.get_investigation(investigation_id)
        if view.status in {"completed", "failed", "canceled", "canceling"}:
            if view.status == "canceling":
                await self.cancel_investigation(investigation_id)
                if worker_steps:
                    return observe_step(self.native, investigation_id).activity_result()
                return {"done": True, "status": "canceled"}
            if worker_steps:
                return observe_step(self.native, investigation_id).activity_result()
            return {"done": True, "status": view.status}
        authorization = self.repository.get_authorization(investigation_id)
        deadline = view.created_at.timestamp() + view.request.budget.max_wall_seconds

        async def heartbeat():
            while True:
                activity.heartbeat({"investigation_id": investigation_id})
                await asyncio.sleep(5)

        pulse = asyncio.create_task(heartbeat()) if activity.in_activity() else None
        try:
            async with self.execution_limits.slot("investigations", deadline=deadline, scope=(view.request.tenant_id, view.request.project_id)):
                with self.tracer.span(investigation_id, "investigation.round", backend="temporal", mode="native_mcp"):
                    if worker_steps:
                        result = await self.native.run_step(investigation_id,
                            principal_id=authorization["principal_id"], scopes=set(authorization["scopes"]),
                            step_id=(activity.info().workflow_run_id + ":" + activity.info().activity_id)
                                if activity.in_activity() else None)
                    else:
                        await self.native.run(investigation_id, principal_id=authorization["principal_id"],
                                              scopes=set(authorization["scopes"]), max_model_turns=1)
                        result = None
        except WorkerOwnershipLost:
            raise
        except CapacityExceeded:
            return {"done": False, "status": view.status, "capacity_deferred": True}
        except Exception as error:
            if isinstance(error, TimeoutError) and datetime.now(timezone.utc).timestamp() >= deadline:
                if worker_steps:
                    result = await self.native.run_step(investigation_id, principal_id=authorization["principal_id"],
                                                       scopes=set(authorization["scopes"]))
                else:
                    await self.native.run(investigation_id, principal_id=authorization["principal_id"],
                                          scopes=set(authorization["scopes"]), max_model_turns=1)
                    result = None
            else:
                await finalize_failure(self.repository, self.reports, investigation_id, error,
                                       code_execution=self.code_execution)
                result = None
        finally:
            if pulse:
                pulse.cancel()
                await asyncio.gather(pulse, return_exceptions=True)
        if result is not None:
            return result.activity_result()
        if worker_steps:
            return observe_step(self.native, investigation_id).activity_result()
        status = self.repository.get_investigation(investigation_id).status
        return {"done": status in {"completed", "failed", "canceled", "canceling"}, "status": status}

    @activity.defn(name="discover_capabilities")
    @owned_activity
    async def discover_capabilities(self, investigation_id: str) -> list[dict[str, Any]]:
        self.repository.get_investigation(investigation_id)
        results = await self.providers.discover_all()
        return [
            {
                "provider_id": item.provider_id,
                "available": item.available,
                "capability_count": item.capability_count,
                "error_code": item.error_code,
            }
            for item in results
        ]

    @activity.defn(name="reason_next_step")
    @owned_activity
    async def reason_next_step(self, investigation_id: str, plan_revision: int) -> dict[str, Any]:
        authorization = self.repository.get_authorization(investigation_id)
        investigation = self.repository.get_investigation(investigation_id)
        if self.repository.get_latest_plan(investigation_id) is None:
            try:
                self.kernel.compile_objective(
                    investigation_id,
                    self.registry.discover(
                        investigation.request.project_id,
                        scopes=set(authorization["scopes"]),
                    ),
                )
            except ValueError:
                pass
        context = self.context_broker.assemble(
            investigation_id,
            scopes=set(authorization["scopes"]),
            step_id=f"round-{plan_revision}",
            intent=investigation.request.objective.description,
        )
        preparation = self.kernel.prepare_round(
            context,
            plan_revision=plan_revision,
            model_name=self.investigator.model_name,
            prompt_version="investigator-v1",
        )
        if preparation.should_stop:
            return NextStep(
                kind="report",
                objective_id=context.investigation.request.objective.objective_id,
                expected_observation="共享调查内核已形成终止判定",
                reason_summary=preparation.stop_reason or "policy_stop",
            ).model_dump(mode="json")
        if preparation.snapshot is None:
            raise RuntimeError("kernel did not freeze a reasoning context")
        result = await self.reasoning.reason(
            investigation_id,
            plan_revision,
            f"next-step-{plan_revision}",
            preparation.snapshot.context.model_dump(mode="json", by_alias=True),
        )
        design = (
            await self.candidate_design.design(
                preparation.snapshot.context, plan_revision=plan_revision
            ) if result.output.kind not in {"report", "request_input"} else None
        )
        selected, selection_record = self.candidate_selection.choose(
            preparation.snapshot.context, result.output, plan_revision=plan_revision,
            design=design,
        )
        record = self.kernel.accept_decision(
            preparation.snapshot,
            selected,
            model_profile_version=result.model_profile_version or "unversioned",
            raw_output_ref=result.raw_output_ref,
            normalization_log=result.normalization_log,
        )
        self.candidate_selection.record(selection_record)
        return record.decision.model_dump(mode="json")

    @activity.defn(name="execute_readonly_decision")
    @owned_activity
    async def execute_readonly_decision(
        self, investigation_id: str, plan_revision: int
    ) -> dict[str, Any]:
        record = self.repository.get_decision_record(investigation_id, plan_revision)
        if record is None:
            raise LookupError("validated read-only decision is missing")
        decision = record.decision
        if decision.kind == "execute_code" and decision.code_request is not None:
            if self.code_execution is None:
                raise ValueError("sandbox execution is not configured")
            async def heartbeat():
                while True:
                    activity.heartbeat({"decision_id": record.decision_id})
                    await asyncio.sleep(1)
            pulse = asyncio.create_task(heartbeat()) if activity.in_activity() else None
            try:
                return await self.code_execution.execute(
                    investigation_id, record.decision_id, decision.code_request
                )
            finally:
                if pulse is not None:
                    pulse.cancel()
                    await asyncio.gather(pulse, return_exceptions=True)
        if decision.kind == "retrieve_evidence" and decision.read_request is not None:
            excerpt = self.tool_loop.read(
                investigation_id, record.decision_id, decision.read_request
            )
            return {"kind": decision.kind, "evidence_id": excerpt.evidence_id}
        if decision.kind == "search_evidence" and decision.search_query:
            ids = self.tool_loop.search(
                investigation_id, record.decision_id, decision.search_query
            )
            return {"kind": decision.kind, "evidence_ids": ids}
        if decision.kind == "analyze" and decision.analysis_request is not None:
            evidence_id = self.tool_loop.analyze(
                investigation_id, record.decision_id, decision.analysis_request
            )
            self.candidate_selection.observe_analysis(
                investigation_id, plan_revision, evidence_id, self.evidence.artifacts
            )
            return {"kind": decision.kind, "evidence_id": evidence_id}
        raise ValueError("decision is not a supported read-only operation")

    @activity.defn(name="plan_and_submit_action")
    @owned_activity
    async def plan_and_submit_action(
        self,
        investigation_id: str,
        plan_revision: int,
        next_step_payload: dict[str, Any],
    ) -> dict[str, Any]:
        authorization = self.repository.get_authorization(investigation_id)
        scopes = set(authorization["scopes"])
        try:
            decision = self.repository.get_decision_record(investigation_id, plan_revision)
            supplied = NextStep.model_validate(next_step_payload)
            if decision is None:
                # Compatibility path for an in-flight workflow whose reasoning
                # Activity completed before DecisionRecord existed. The historical
                # decision payload is preserved and validated; it is never regenerated.
                recovery_context = self.context.build(investigation_id, scopes=scopes)
                preparation = self.kernel.prepare_round(
                    recovery_context,
                    plan_revision=plan_revision,
                    model_name=self.investigator.model_name,
                    prompt_version="investigator-v1",
                )
                if preparation.should_stop or preparation.snapshot is None:
                    raise ValueError(preparation.stop_reason or "legacy decision recovery failed")
                decision = self.kernel.accept_decision(preparation.snapshot, supplied)
            if supplied != decision.decision:
                raise ValueError("action request differs from the validated decision")
            snapshot = self.repository.get_context_snapshot(decision.context_snapshot_id)
            planned = await self.durable_action_planner.plan(snapshot.context, decision, ordinal=1)
            action = planned.action
            operation = await self.actions.submit(
                action,
                principal_id=authorization["principal_id"],
                scopes=scopes,
            )
        except StandardToolError as error:
            if error.retryable:
                raise
            return {"ok": False, "stop_reason": f"provider_{error.code.lower()}"}
        except PlanBudgetExceeded as error:
            return {"ok": False, "stop_reason": error.stop_reason}
        except PlanCorrectionRequired as error:
            previous = [e for e in self.repository.list_recent_events(investigation_id)
                        if e["event_type"] == "execution.rejected"]
            if len(previous) >= 2:
                return {"ok": False, "stop_reason": "plan_correction_budget_exhausted"}
            self.repository.record_investigation_event(
                investigation_id, "execution.rejected",
                {"code": error.code, "instruction": error.instruction},
            )
            return {"ok": False, "stop_reason": "plan_correction_required", "repairable": True}
        except (ConflictError, ValueError, LookupError) as error:
            return {
                "ok": False,
                "stop_reason": f"action_rejected:{type(error).__name__}",
            }
        return {
            "ok": True,
            "action_id": action.action_id,
            "operation": operation.model_dump(mode="json"),
        }

    @activity.defn(name="query_action")
    @owned_activity
    async def query_action(
        self, action_id: str, poll_number: int, query_round: int | None = None,
        reserve_reads: int = 0,
    ) -> dict[str, Any]:
        # Preserve reservation IDs for old workflow histories and activity retries.
        scope = f"round-{query_round}/" if query_round is not None else ""
        remaining = {}
        if reserve_reads:
            action = self.repository.get_action(action_id)[0]
            view = self.repository.get_investigation(action.investigation_id)
            available = view.request.budget.max_tool_reads - int(
                self.repository.budget_usage(action.investigation_id).get("tool_reads", 0)
            )
            if available <= reserve_reads:
                return {"kind": "error", "stop_reason": "status_read_budget_exhausted"}
            remaining = {"remaining_reads": available - 1}
        try:
            result = await self.actions.reconcile(
                action_id,
                read_reservation_id=f"operation-poll/{action_id}/{scope}{poll_number}",
            )
        except ConflictError:
            return {"kind": "error", "stop_reason": "tool_read_budget_exhausted"}
        except StandardToolError as error:
            if error.retryable:
                raise
            return {"kind": "error", "stop_reason": f"provider_{error.code.lower()}"}
        if isinstance(result, RunResult):
            return {"kind": "run_result", "value": result.model_dump(mode="json"), **remaining}
        if isinstance(result, OperationRef):
            return {"kind": "operation", "value": result.model_dump(mode="json"), **remaining}
        return {"kind": "pending", **remaining}

    @activity.defn(name="ingest_run_evidence")
    @owned_activity
    async def ingest_run_evidence(
        self,
        investigation_id: str,
        action_id: str | dict[str, Any],
        result_payload: dict[str, Any] | None = None,
    ) -> int:
        # The two-argument form remains readable for in-flight v1 Temporal histories.
        if result_payload is None:
            result_payload = action_id  # type: ignore[assignment]
            legacy_result = RunResult.model_validate(result_payload)
            investigation = self.repository.get_investigation(investigation_id)
            snapshot = self.repository.get_run_result_by_ref(
                investigation.request.tenant_id,
                legacy_result.run_ref,
                legacy_result.revision,
            )
            if snapshot is None:
                raise ValueError("run result snapshot is missing for legacy ingestion")
            action_id = snapshot.action_id
        result = RunResult.model_validate(result_payload)
        provider = self.providers.provider(result.run_ref.provider_id)
        try:
            records = await self.evidence.ingest(investigation_id, result, provider)
        except Exception as error:
            self.repository.mark_run_result_ingestion(
                str(action_id), result.revision, "failed", type(error).__name__
            )
            raise
        self.repository.mark_run_result_ingestion(str(action_id), result.revision, "ingested")
        action, _state, _operation = self.repository.get_action(str(action_id))
        self.candidate_selection.observe_run(
            investigation_id, action.plan_revision, result
        )
        return len(records)

    @activity.defn(name="evaluate_objective")
    @owned_activity
    async def evaluate_objective(self, investigation_id: str) -> dict[str, Any] | None:
        assessment = self.kernel.evaluate_objective(investigation_id)
        return assessment.model_dump(mode="json") if assessment else None

    @activity.defn(name="publish_inconclusive_report")
    @owned_activity
    async def publish_inconclusive_report(self, investigation_id: str, stop_reason: str) -> str:
        view = self.repository.get_investigation(investigation_id)
        if view.status == InvestigationStatus.COMPLETED:
            _, _, digest = self.repository.get_report(investigation_id, 1)
            return digest
        report = self.report_builder.build(investigation_id, stop_reason)
        digest = self.reports.publish(report)
        self.repository.transition(
            investigation_id,
            InvestigationStatus.COMPLETED,
            resolution=report.resolution,
            stop_reason=stop_reason,
        )
        return digest

    @activity.defn(name="cancel_investigation")
    @owned_activity
    async def cancel_investigation(self, investigation_id: str) -> list[str]:
        if self.native is not None:
            await self.native.native_operations.cancel_investigation(investigation_id)
        existing_view = self.repository.get_investigation(investigation_id)
        if existing_view.status == InvestigationStatus.CANCELED:
            return []
        outstanding: list[str] = []
        if self.code_execution is not None:
            await self.code_execution.cancel_investigation(investigation_id)
        for item in self.repository.list_actions(investigation_id):
            action_id = item["action"]["action_id"]
            if item["state"] in {"succeeded", "failed", "canceled"}:
                continue
            if not await self.actions.cancel(action_id):
                outstanding.append(action_id)
                self.repository.record_cleanup_required(investigation_id, action_id)
        view = self.repository.get_investigation(investigation_id)
        if view.status not in {
            InvestigationStatus.CANCELING,
            InvestigationStatus.CANCELED,
        }:
            self.repository.transition(investigation_id, InvestigationStatus.CANCELING)
        outstanding = list(dict.fromkeys(outstanding + [
            r["resource_id"] for r in self.repository.pending_cleanup_resources(investigation_id)
        ]))
        stop_reason = "cancel_cleanup_pending" if outstanding else "user_canceled"
        try:
            self.repository.get_report(investigation_id, 1)
        except NotFoundError:
            report = self.report_builder.build(
                investigation_id,
                stop_reason,
                status="canceled",
                resolution=Resolution.CANCELED,
                summary=(
                    "已停止继续调查，但仍有外部运行尚未确认停止。"
                    if outstanding
                    else "调查已按用户请求取消；报告保留取消前已经取得的证据。"
                ),
            )
            self.reports.publish(report)
        if (
            self.repository.get_investigation(investigation_id).status
            != InvestigationStatus.CANCELED
        ):
            self.repository.transition(
                investigation_id,
                InvestigationStatus.CANCELED,
                resolution=Resolution.CANCELED,
                stop_reason=stop_reason,
            )
        return outstanding

    @activity.defn(name="reconcile_cleanup")
    @owned_activity
    async def reconcile_cleanup(self, investigation_id: str) -> list[str]:
        if self.native is not None:
            await self.native.native_operations.cancel_investigation(investigation_id)
        if self.code_execution is not None:
            await self.code_execution.cancel_investigation(investigation_id)
        for action_id in self.repository.pending_cleanup_actions(investigation_id):
            try:
                resolved = await self.actions.cancel(action_id)
            except Exception as error:
                self.repository.note_cleanup_attempt(action_id, type(error).__name__)
                continue
            if resolved:
                self.repository.resolve_cleanup(action_id)
            else:
                self.repository.note_cleanup_attempt(action_id, "cancel_not_confirmed")
        return list(dict.fromkeys(self.repository.pending_cleanup_actions(investigation_id) + [
            r["resource_id"] for r in self.repository.pending_cleanup_resources(investigation_id)
        ]))


async def run_worker() -> None:
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    repository = create_repository(settings)
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    activities = CoreActivities(repository, settings)
    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[InvestigationWorkflow, CleanupWorkflow],
        activities=[
            activities.project_status,
            activities.load_control_state,
            activities.native_investigation_step,
            activities.discover_capabilities,
            activities.reason_next_step,
            activities.execute_readonly_decision,
            activities.plan_and_submit_action,
            activities.query_action,
            activities.ingest_run_evidence,
            activities.evaluate_objective,
            activities.publish_inconclusive_report,
            activities.cancel_investigation,
            activities.reconcile_cleanup,
        ],
    )
    try:
        await worker.run()
    finally:
        if activities.native is not None:
            await activities.native.close()
        if activities.native_providers is not None:
            await activities.native_providers.close()
        await activities.providers.close()
        repository.close()


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
