from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Awaitable, Callable

from game_test_agent.activities import ActionService, ReasoningService
from game_test_agent.capabilities import CapabilityRegistry
from game_test_agent.context import ContextBroker
from game_test_agent.context.tool_loop import InvestigationToolLoop
from game_test_agent.contracts.models import (
    InvestigationReport,
    InvestigationStatus,
    ActionSpec,
    ActionState,
    ReadRequest,
    RunRef,
    RunResult,
)
from game_test_agent.evidence import ContextBuilder, EvidenceManifestIngestor
from game_test_agent.memory import ProjectPackRegistry
from game_test_agent.planning import (
    ActionPlanner,
    CandidateDesignService,
    CandidateSelectionService,
    DurableActionPlanner,
)
from game_test_agent.planning.service import PlanBudgetExceeded, PlanCorrectionRequired
from game_test_agent.mcp_gateway import StandardToolError
from game_test_agent.policies import StopContext, StopPolicy
from game_test_agent.reports import ReportBuilder, ReportService
from game_test_agent.runtime import InvestigationKernel
from game_test_agent.storage import ConflictError, Repository
from game_test_agent.workflows.waiting import (
    ExternalWaitStopped, investigation_deadline, poll_delay,
)


class InvestigationController:
    execution_mode = "typed"
    """Bounded local controller exercising the production service boundaries.

    Temporal uses the same activities but replaces in-process sleeps with durable timers.
    """

    def __init__(
        self,
        repository: Repository,
        registry: CapabilityRegistry,
        reasoning: ReasoningService,
        action_planner: ActionPlanner,
        actions: ActionService,
        evidence: EvidenceManifestIngestor,
        reports: ReportService,
        provider_lookup: Callable[[str], object],
        discover: Callable[[], Awaitable[object]] | None = None,
        stop_policy: StopPolicy | None = None,
        context_window_tokens: int = 24_000,
        context_reserved_output_tokens: int = 4_000,
        code_execution=None,
    ) -> None:
        self.repository = repository
        self.registry = registry
        self.reasoning = reasoning
        self.action_planner = action_planner
        self.durable_action_planner = DurableActionPlanner(repository, action_planner)
        self.candidate_selection = CandidateSelectionService(repository)
        self.candidate_design = CandidateDesignService(repository, reasoning.investigator, execution_limits=reasoning.gateway.execution_limits)
        self.actions = actions
        self.evidence = evidence
        self.tool_loop = InvestigationToolLoop(repository, evidence.artifacts)
        self.code_execution = code_execution
        self.reports = reports
        self.provider_lookup = provider_lookup
        self.discover = discover
        self.stop_policy = stop_policy or StopPolicy()
        self.context_builder = ContextBuilder(
            repository,
            registry,
            window_limit_tokens=context_window_tokens,
            reserved_output_tokens=context_reserved_output_tokens,
            code_execution=code_execution,
        )
        self.context_broker = ContextBroker(
            self.context_builder, registry, project_packs=ProjectPackRegistry(repository)
        )
        self.report_builder = ReportBuilder(repository)
        self.kernel = InvestigationKernel(repository, stop_policy=self.stop_policy)
        self._operation_updates: dict[str, asyncio.Event] = {}

    def notify_event(self, investigation_id: str) -> None:
        wake = self._operation_updates.get(investigation_id)
        if wake is not None:
            wake.set()

    async def run(
        self,
        investigation_id: str,
        *,
        principal_id: str,
        scopes: set[str],
    ) -> InvestigationReport | None:
        initial = self.repository.get_investigation(investigation_id)
        if initial.status in {
            InvestigationStatus.COMPLETED, InvestigationStatus.FAILED,
            InvestigationStatus.CANCELED, InvestigationStatus.CANCELING,
        }:
            return None
        started_at = initial.created_at
        if initial.status == InvestigationStatus.REPORTING:
            try:
                report = self.repository.get_latest_report(investigation_id)[0]
            except LookupError:
                report = self._build_report(investigation_id, "recovered_reporting")
                self.reports.publish(report)
            self.repository.transition(
                investigation_id, InvestigationStatus(report.status),
                resolution=report.resolution, stop_reason=report.stop_reason,
            )
            return report
        if initial.status == InvestigationStatus.ACCEPTED:
            self.repository.transition(investigation_id, InvestigationStatus.DISCOVERING)
            if self.discover is not None:
                await self.discover()
        elif initial.status != InvestigationStatus.WAITING_INPUT:
            recovered_stop = await self._recover_execution(investigation_id, principal_id, scopes)
            if recovered_stop == "waiting_input":
                return None
            if recovered_stop:
                self.repository.transition(investigation_id, InvestigationStatus.REPORTING)
                report = self._build_report(investigation_id, recovered_stop)
                self.reports.publish(report)
                self.repository.transition(
                    investigation_id, InvestigationStatus.COMPLETED,
                    resolution=report.resolution, stop_reason=recovered_stop,
                )
                return report
        if self.repository.get_investigation(investigation_id).status != InvestigationStatus.PLANNING:
            self.repository.transition(investigation_id, InvestigationStatus.PLANNING)
        if initial.request.source == "event":
            try:
                await self._inspect_source_run(investigation_id, scopes)
            except Exception as error:
                self.repository.record_investigation_event(
                    investigation_id, "source_run.unavailable", {"error_code": type(error).__name__}
                )
        if self.repository.get_latest_plan(investigation_id) is None:
            try:
                self.kernel.compile_objective(
                    investigation_id,
                    [
                        item
                        for item in self.registry.discover(
                            initial.request.project_id, scopes=scopes
                        )
                        if item.provider_id
                        in initial.request.constraints.get("provider_allowlist", [item.provider_id])
                    ],
                )
            except ValueError:
                # ObjectiveEvaluator preserves the existing bounded invalid-spec outcome.
                pass

        stop_reason = "max_rounds_reached"
        no_progress_rounds = 0
        first_round = 1 + max(
            (
                record.plan_revision
                for record in self.repository.list_decision_records(investigation_id)
            ),
            default=0,
        )
        for round_number in range(first_round, initial.request.budget.max_rounds + 1):
            context = self.context_broker.assemble(
                investigation_id,
                scopes=scopes,
                step_id=f"round-{round_number}",
                intent=initial.request.objective.description,
            )
            preparation = self.kernel.prepare_round(
                context,
                plan_revision=round_number,
                model_name=self.reasoning.investigator.model_name,
                prompt_version="investigator-v1",
                stop_context=StopContext(
                    started_at=started_at,
                    now=datetime.now(timezone.utc),
                    reasoning_rounds=round_number - 1,
                    real_runs=int(context.budget.get("real_runs", 0)),
                    tool_reads=int(context.budget.get("tool_reads", 0)),
                    device_minutes=float(context.budget.get("reserved_device_minutes", 0)),
                    consecutive_rounds_without_evidence=no_progress_rounds,
                    required_capability_available=bool(context.capabilities or context.evidence),
                ),
            )
            if preparation.should_stop:
                stop_reason = preparation.stop_reason or "policy_stop"
                break
            if preparation.snapshot is None:
                stop_reason = "kernel_context_unavailable"
                break
            try:
                step = await self.reasoning.reason(
                    investigation_id,
                    round_number,
                    f"next-step-{round_number}",
                    preparation.snapshot.context.model_dump(mode="json", by_alias=True),
                )
            except Exception as error:
                stop_reason = f"reasoning_failed:{type(error).__name__}"
                break
            try:
                design = (
                    await self.candidate_design.design(
                        preparation.snapshot.context, plan_revision=round_number
                    )
                    if step.output.kind not in {"report", "request_input"}
                    else None
                )
                selected, selection_record = self.candidate_selection.choose(
                    preparation.snapshot.context,
                    step.output,
                    plan_revision=round_number,
                    design=design,
                )
                decision_record = self.kernel.accept_decision(
                    preparation.snapshot,
                    selected,
                    model_profile_version=step.model_profile_version or "unversioned",
                    raw_output_ref=step.raw_output_ref,
                    normalization_log=step.normalization_log,
                )
                self.candidate_selection.record(selection_record)
                decision = decision_record.decision
            except ValueError as error:
                stop_reason = f"decision_invalid:{type(error).__name__}"
                break
            if decision.kind == "request_input":
                self.repository.transition(investigation_id, InvestigationStatus.WAITING_INPUT)
                return None
            if decision.kind == "report":
                stop_reason = decision.reason_summary or "no_valuable_action"
                break
            progress_before = self.kernel.progress_fingerprint(investigation_id)
            # Each round owns its failure state; do not reuse the default loop-limit reason.
            stop_reason = ""
            action_id: str | None = None
            result: RunResult | None = None
            tool_completed = False
            if decision.kind == "query_run":
                action_id = decision.target_action_id
                try:
                    result = await self._wait_for_result(action_id or "", 0.1)
                except ConflictError:
                    stop_reason = "tool_read_budget_exhausted"
                except ExternalWaitStopped as error:
                    stop_reason = error.stop_reason
                except Exception as error:
                    stop_reason = f"query_failed:{type(error).__name__}"
            elif decision.kind in {"retrieve_evidence", "search_evidence", "analyze", "execute_code"}:
                self.repository.transition(investigation_id, InvestigationStatus.ANALYZING)
                try:
                    if decision.kind == "retrieve_evidence":
                        self.tool_loop.read(
                            investigation_id, decision_record.decision_id, decision.read_request
                        )
                    elif decision.kind == "search_evidence":
                        self.tool_loop.search(
                            investigation_id,
                            decision_record.decision_id,
                            decision.search_query or "",
                        )
                    elif decision.kind == "execute_code":
                        await self.code_execution.execute(
                            investigation_id, decision_record.decision_id, decision.code_request
                        )
                    else:
                        evidence_id = self.tool_loop.analyze(
                            investigation_id,
                            decision_record.decision_id,
                            decision.analysis_request,
                        )
                        self.candidate_selection.observe_analysis(
                            investigation_id,
                            round_number,
                            evidence_id,
                            self.evidence.artifacts,
                        )
                    tool_completed = True
                except ConflictError:
                    stop_reason = "tool_read_budget_exhausted"
                except Exception as error:
                    stop_reason = f"tool_operation_failed:{type(error).__name__}"
            else:
                try:
                    planned = await self.durable_action_planner.plan(
                        preparation.snapshot.context, decision_record, ordinal=1
                    )
                    action = planned.action
                except PlanBudgetExceeded as error:
                    stop_reason = error.stop_reason
                    break
                except PlanCorrectionRequired as error:
                    previous = [e for e in self.repository.list_recent_events(investigation_id)
                                if e["event_type"] == "execution.rejected"]
                    if len(previous) >= 2:
                        stop_reason = "plan_correction_budget_exhausted"
                        break
                    self.repository.record_investigation_event(
                        investigation_id, "execution.rejected",
                        {"code": error.code, "instruction": error.instruction,
                         "decision_id": decision_record.decision_id},
                    )
                    continue
                except Exception as error:
                    stop_reason = f"action_planning_failed:{type(error).__name__}"
                    break
                self.repository.transition(investigation_id, InvestigationStatus.ACTING)
                action_id = action.action_id
                try:
                    operation = await self._submit_action(action, principal_id, scopes)
                    self.repository.transition(
                        investigation_id, InvestigationStatus.WAITING_EXTERNAL
                    )
                    result = await self._wait_for_result(
                        action.action_id, operation.suggested_poll_interval_seconds
                    )
                except ConflictError as error:
                    stop_reason = (
                        "tool_read_budget_exhausted"
                        if "tool_reads" in str(error)
                        else "duplicate_or_budget_rejected"
                    )
                except ExternalWaitStopped as error:
                    stop_reason = error.stop_reason
                except StandardToolError as error:
                    stop_reason = f"provider_{error.code.lower()}"
                except Exception as error:
                    stop_reason = f"action_failed:{type(error).__name__}"

            current = self.repository.get_investigation(investigation_id)
            if current.status in {InvestigationStatus.ACTING, InvestigationStatus.PLANNING}:
                self.repository.transition(investigation_id, InvestigationStatus.ANALYZING)
            elif current.status == InvestigationStatus.WAITING_EXTERNAL:
                self.repository.transition(investigation_id, InvestigationStatus.ANALYZING)

            if result is not None:
                try:
                    provider = self.provider_lookup(result.run_ref.provider_id)
                    await self.evidence.ingest(investigation_id, result, provider)  # type: ignore[arg-type]
                    self._read_failure_log(investigation_id, result, scopes)
                    if action_id:
                        self.repository.mark_run_result_ingestion(
                            action_id, result.revision, "ingested"
                        )
                    self.candidate_selection.observe_run(investigation_id, round_number, result)
                except Exception as error:
                    if action_id:
                        self.repository.mark_run_result_ingestion(
                            action_id, result.revision, "failed", type(error).__name__
                        )
                    stop_reason = f"evidence_ingestion_failed:{type(error).__name__}"
            elif not tool_completed:
                stop_reason = stop_reason or "external_result_pending"

            if result is not None and not result.evidence_manifest:
                stop_reason = (
                    "completed_without_evidence"
                    if result.execution_status == "succeeded"
                    else f"external_{result.execution_status}"
                )

            self.repository.transition(investigation_id, InvestigationStatus.EVALUATING)
            progress_after = self.kernel.progress_fingerprint(investigation_id)
            no_progress_rounds = no_progress_rounds + 1 if progress_after == progress_before else 0
            try:
                assessment = self.kernel.evaluate_objective(investigation_id)
            except ValueError:
                stop_reason = "objective_spec_invalid"
                break
            if assessment and assessment.is_complete:
                stop_reason = (
                    "objective_passed" if assessment.verdict == "passed" else "objective_failed"
                )
                break
            if decision.kind in {"retrieve_evidence", "search_evidence", "analyze", "execute_code"}:
                if not tool_completed:
                    break
            elif result is None or (
                result.execution_status != "succeeded" and progress_after == progress_before
            ):
                break
            if not stop_reason:
                stop_reason = "max_rounds_reached"
            if round_number < initial.request.budget.max_rounds:
                self.repository.transition(investigation_id, InvestigationStatus.PLANNING)

        current = self.repository.get_investigation(investigation_id)
        if current.status == InvestigationStatus.EVALUATING:
            self.repository.transition(investigation_id, InvestigationStatus.REPORTING)
        elif current.status == InvestigationStatus.PLANNING:
            self.repository.transition(investigation_id, InvestigationStatus.REPORTING)
        report = self._build_report(investigation_id, stop_reason)
        self.reports.publish(report)
        self.repository.transition(
            investigation_id,
            InvestigationStatus.COMPLETED,
            resolution=report.resolution,
            stop_reason=stop_reason,
        )
        return report

    def _resume_planning(self, investigation_id: str) -> None:
        status = self.repository.get_investigation(investigation_id).status
        if status in {InvestigationStatus.ACTING, InvestigationStatus.WAITING_EXTERNAL}:
            self.repository.transition(investigation_id, InvestigationStatus.ANALYZING)
            status = InvestigationStatus.ANALYZING
        if status == InvestigationStatus.ANALYZING:
            self.repository.transition(investigation_id, InvestigationStatus.EVALUATING)
            status = InvestigationStatus.EVALUATING
        if status in {InvestigationStatus.DISCOVERING, InvestigationStatus.EVALUATING}:
            self.repository.transition(investigation_id, InvestigationStatus.PLANNING)

    async def _submit_action(self, action: ActionSpec, principal_id: str, scopes: set[str]):
        view = self.repository.get_investigation(action.investigation_id)
        deadline = investigation_deadline(view.created_at, view.request.budget.max_wall_seconds)
        while True:
            remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                if self.repository.get_action(action.action_id)[1] == ActionState.RESERVED:
                    await self.actions.cancel(action.action_id)
                else:
                    self.repository.record_cleanup_required(action.investigation_id, action.action_id)
                raise ExternalWaitStopped("wall_clock_budget_exhausted")
            try:
                return await asyncio.wait_for(
                    self.actions.submit(action, principal_id=principal_id, scopes=scopes),
                    timeout=remaining,
                )
            except StandardToolError as error:
                if not error.retryable:
                    raise
                # Reuse the durable action and idempotency key, including after a lost response.
                await asyncio.sleep(min(remaining, max(0.1, min(
                    (error.retry_after_ms or 1000) / 1000, 5.0
                ))))
            except TimeoutError:
                self.repository.record_cleanup_required(action.investigation_id, action.action_id)
                raise ExternalWaitStopped("wall_clock_budget_exhausted") from None

    async def _recover_execution(
        self, investigation_id: str, principal_id: str, scopes: set[str]
    ) -> str | None:
        self._resume_planning(investigation_id)
        if self.discover is not None:
            await self.discover()
        records = self.repository.list_decision_records(investigation_id)
        items = self.repository.list_actions(investigation_id)
        if records:
            record = records[-1]
            if record.decision.kind == "request_input":
                self.repository.transition(investigation_id, InvestigationStatus.WAITING_INPUT)
                return "waiting_input"
            if record.decision.kind == "report":
                return record.decision.reason_summary or "recovered_report_decision"
            if record.decision.kind in {"retrieve_evidence", "search_evidence", "analyze", "execute_code"}:
                self.repository.transition(investigation_id, InvestigationStatus.ANALYZING)
                decision = record.decision
                if decision.kind == "retrieve_evidence":
                    self.tool_loop.read(investigation_id, record.decision_id, decision.read_request)
                elif decision.kind == "search_evidence":
                    self.tool_loop.search(investigation_id, record.decision_id, decision.search_query or "")
                elif decision.kind == "execute_code":
                    await self.code_execution.execute(
                        investigation_id, record.decision_id, decision.code_request
                    )
                else:
                    evidence_id = self.tool_loop.analyze(investigation_id, record.decision_id, decision.analysis_request)
                    self.candidate_selection.observe_analysis(
                        investigation_id, record.plan_revision, evidence_id, self.evidence.artifacts
                    )
                self._resume_planning(investigation_id)
            if record.decision.kind == "experiment" and not any(
                i["action"]["plan_revision"] == record.plan_revision for i in items
            ):
                snapshot = self.repository.get_context_snapshot(record.context_snapshot_id)
                try:
                    planned = await self.durable_action_planner.plan(snapshot.context, record)
                except PlanCorrectionRequired as error:
                    self.repository.record_investigation_event(
                        investigation_id, "execution.rejected",
                        {"code": error.code, "instruction": error.instruction,
                         "decision_id": record.decision_id},
                    )
                else:
                    self.repository.transition(investigation_id, InvestigationStatus.ACTING)
                    try:
                        await self._submit_action(planned.action, principal_id, scopes)
                    except ExternalWaitStopped as error:
                        self._resume_planning(investigation_id)
                        return error.stop_reason
                    self._resume_planning(investigation_id)
                    items = self.repository.list_actions(investigation_id)
        for item in items:
            action = ActionSpec.model_validate(item["action"])
            state = ActionState(item["state"])
            if state not in {ActionState.SUCCEEDED, ActionState.FAILED, ActionState.CANCELED}:
                self.repository.transition(investigation_id, InvestigationStatus.ACTING)
                if state == ActionState.DISPATCHING:
                    self.repository.transition_action(
                        action.action_id, {state}, ActionState.UNKNOWN
                    )
                try:
                    if state in {ActionState.RESERVED, ActionState.DISPATCHING, ActionState.UNKNOWN}:
                        await self._submit_action(action, principal_id, scopes)
                    self.repository.transition(investigation_id, InvestigationStatus.WAITING_EXTERNAL)
                    await self._wait_for_result(action.action_id, 2)
                except ExternalWaitStopped as error:
                    self._resume_planning(investigation_id)
                    return error.stop_reason
                self._resume_planning(investigation_id)
            snapshot = self.repository.get_latest_run_result(action.action_id)
            if state in {ActionState.SUCCEEDED, ActionState.FAILED, ActionState.CANCELED}:
                await self.actions.reconcile(action.action_id)
            if snapshot and snapshot.ingestion_state != "ingested":
                provider = self.provider_lookup(action.provider_id)
                await self.evidence.ingest(investigation_id, snapshot.result, provider)
                self._read_failure_log(investigation_id, snapshot.result, scopes)
                self.repository.mark_run_result_ingestion(action.action_id, snapshot.result.revision, "ingested")
                self.candidate_selection.observe_run(investigation_id, action.plan_revision, snapshot.result)
        self.repository.record_investigation_event(
            investigation_id, "workflow.recovered", {"action_count": len(items)}
        )
        return None

    async def _inspect_source_run(self, investigation_id: str, scopes: set[str]) -> None:
        investigation = self.repository.get_investigation(investigation_id)
        raw_ref = investigation.request.constraints.get("source_run_ref")
        if not isinstance(raw_ref, dict):
            return
        if not {"runs:read", "evidence:read"}.issubset(scopes):
            raise PermissionError("source run inspection requires read scopes")
        ref = RunRef.model_validate(raw_ref)
        if ref.project_id != investigation.request.project_id:
            raise PermissionError("source run is outside investigation project")
        if ref.provider_id not in investigation.request.constraints.get(
            "provider_allowlist", [ref.provider_id]
        ):
            raise PermissionError("source run provider is not allowed")
        if any(
            item.run_ref == ref for item in self.repository.list_current_evidence(investigation_id)
        ):
            return
        provider = self.provider_lookup(ref.provider_id)
        reader = getattr(provider, "query_source_run", None)
        if reader is None or not getattr(provider, "source_run_supported", True):
            return
        self.repository.reserve_counter(
            investigation_id,
            "tool_reads",
            f"source-run:{ref.provider_id}:{ref.run_id}",
            investigation.request.budget.max_tool_reads,
        )
        result = await reader(ref)
        if not isinstance(result, RunResult) or result.run_ref != ref:
            raise ValueError("source result identity mismatch")
        records = await self.evidence.ingest(investigation_id, result, provider)
        self._read_failure_log(investigation_id, result, scopes)
        self.repository.record_investigation_event(
            investigation_id,
            "source_run.inspected",
            {
                "run_ref": ref.model_dump(mode="json"),
                "result_revision": result.revision,
                "execution_status": result.execution_status,
                "test_verdict": result.test_verdict,
                "evidence_count": len(records),
            },
        )

    def _read_failure_log(self, investigation_id: str, result: RunResult, scopes: set[str]) -> None:
        if "evidence:read" not in scopes or (
            result.execution_status != "failed" and result.test_verdict != "failed"
        ):
            return
        view = self.repository.get_investigation(investigation_id)
        if self.repository.budget_usage(investigation_id).get("tool_reads", 0) >= view.request.budget.max_tool_reads:
            return
        events = self.repository.list_recent_events(investigation_id)
        read_hashes = {e["data"]["excerpt"]["source_sha256"] for e in events
                       if e["event_type"] == "evidence.excerpt_read"}
        for evidence in self.repository.list_current_evidence(investigation_id):
            if (evidence.run_ref == result.run_ref and evidence.kind == "log"
                    and evidence.retrieval_status == "ready"
                    and evidence.mime_type.startswith("text/") and evidence.sha256 not in read_hashes):
                self.tool_loop.read(
                    investigation_id, f"failure-log:{evidence.evidence_id}",
                    ReadRequest(evidence_id=evidence.evidence_id,
                                purpose="Read the failure log before deciding to repeat execution",
                                max_bytes=4096),
                )
                break

    async def _wait_for_result(self, action_id: str, suggested_interval: float) -> RunResult | None:
        action = self.repository.get_action(action_id)[0]
        view = self.repository.get_investigation(action.investigation_id)
        deadline = investigation_deadline(view.created_at, view.request.budget.max_wall_seconds)
        reserve = min(8, view.request.budget.max_tool_reads // 4)
        poll_session = uuid.uuid4().hex
        wake = self._operation_updates.setdefault(view.investigation_id, asyncio.Event())
        attempt = 0
        while True:
            remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                self.repository.record_cleanup_required(view.investigation_id, action_id)
                raise ExternalWaitStopped("wall_clock_budget_exhausted")
            usage = self.repository.budget_usage(view.investigation_id)
            available = view.request.budget.max_tool_reads - int(usage.get("tool_reads", 0))
            if available <= reserve:
                self.repository.record_cleanup_required(view.investigation_id, action_id)
                raise ExternalWaitStopped("status_read_budget_exhausted")
            attempt += 1
            wake.clear()
            try:
                reconciled = await asyncio.wait_for(
                    self.actions.reconcile(
                        action_id,
                        read_reservation_id=f"operation-poll/{action_id}/{poll_session}/{attempt}",
                    ), timeout=remaining,
                )
            except StandardToolError as error:
                if not error.retryable:
                    raise
                reconciled = None
            except TimeoutError:
                self.repository.record_cleanup_required(view.investigation_id, action_id)
                raise ExternalWaitStopped("wall_clock_budget_exhausted") from None
            if isinstance(reconciled, RunResult):
                if reconciled.execution_status in {
                    "succeeded",
                    "failed",
                    "canceled",
                    "unknown",
                }:
                    return reconciled
            remaining = max((deadline - datetime.now(timezone.utc)).total_seconds(), 0)
            delay = (
                min(max(suggested_interval, 0.1), 5.0, remaining)
                if attempt <= 2
                else poll_delay(remaining, available - reserve, suggested_interval)
            )
            try:
                await asyncio.wait_for(wake.wait(), timeout=delay)
            except TimeoutError:
                pass

    def _build_report(self, investigation_id: str, stop_reason: str) -> InvestigationReport:
        return self.report_builder.build(investigation_id, stop_reason)
