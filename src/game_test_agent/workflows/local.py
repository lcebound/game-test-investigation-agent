from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from game_test_agent.activities import ReasoningService
from game_test_agent.agents import Investigator
from game_test_agent.capabilities import CapabilityRegistry
from game_test_agent.context import ContextBroker
from game_test_agent.contracts.models import (
    InvestigationStatus,
    Resolution,
)
from game_test_agent.evidence import ContextBuilder
from game_test_agent.memory import ProjectPackRegistry
from game_test_agent.reports import ReportBuilder, ReportService
from game_test_agent.runtime import InvestigationKernel
from game_test_agent.storage import Repository
from game_test_agent.runtime.lifecycle import finalize_failure
from game_test_agent.runtime.execution import CapacityExceeded, ExecutionLimits
from game_test_agent.runtime.worker_ownership import (
    WorkerOwnership, WorkerOwnershipBusy, WorkerOwnershipLost, WorkerBackendMismatch, owned_local, validate_owned_write,
)
from game_test_agent.observability.tracing import InvestigationTracer

if TYPE_CHECKING:
    from game_test_agent.workflows.controller import InvestigationController


ALLOWED_TRANSITIONS: dict[InvestigationStatus, set[InvestigationStatus]] = {
    InvestigationStatus.ACCEPTED: {InvestigationStatus.DISCOVERING, InvestigationStatus.CANCELING},
    InvestigationStatus.DISCOVERING: {InvestigationStatus.PLANNING, InvestigationStatus.CANCELING},
    InvestigationStatus.PLANNING: {
        InvestigationStatus.ACTING,
        InvestigationStatus.ANALYZING,
        InvestigationStatus.WAITING_INPUT,
        InvestigationStatus.REPORTING,
        InvestigationStatus.CANCELING,
    },
    InvestigationStatus.ACTING: {
        InvestigationStatus.WAITING_EXTERNAL,
        InvestigationStatus.ANALYZING,
        InvestigationStatus.CANCELING,
    },
    InvestigationStatus.WAITING_EXTERNAL: {
        InvestigationStatus.ANALYZING,
        InvestigationStatus.CANCELING,
    },
    InvestigationStatus.WAITING_INPUT: {
        InvestigationStatus.PLANNING,
        InvestigationStatus.CANCELING,
    },
    InvestigationStatus.ANALYZING: {
        InvestigationStatus.EVALUATING,
        InvestigationStatus.CANCELING,
    },
    InvestigationStatus.EVALUATING: {
        InvestigationStatus.PLANNING,
        InvestigationStatus.REPORTING,
        InvestigationStatus.CANCELING,
    },
    InvestigationStatus.REPORTING: {InvestigationStatus.COMPLETED, InvestigationStatus.FAILED},
    InvestigationStatus.CANCELING: {InvestigationStatus.CANCELED},
}


class LocalWorkflowBackend:
    """Recoverable development backend implementing the same state contract as Temporal.

    Process-local capacity is combined with exclusive investigation ownership.
    A persisted backend binding prevents Local/Temporal from sharing one control flow.
    """

    def __init__(
        self,
        repository: Repository,
        registry: CapabilityRegistry,
        investigator: Investigator,
        reports: ReportService,
        controller: "InvestigationController | None" = None,
        context_window_tokens: int = 24_000,
        context_reserved_output_tokens: int = 4_000,
        execution_limits=None,
    ) -> None:
        self.repository = repository
        self.ownership = WorkerOwnership(repository)
        self.registry = registry
        self.investigator = investigator
        self.execution_limits = execution_limits or ExecutionLimits()
        self.tracer = InvestigationTracer(repository)
        self.reasoning = ReasoningService(repository, investigator, execution_limits=self.execution_limits)
        self.reports = reports
        self.report_builder = ReportBuilder(repository)
        self.context_builder = ContextBuilder(
            repository,
            registry,
            window_limit_tokens=context_window_tokens,
            reserved_output_tokens=context_reserved_output_tokens,
        )
        self.context_broker = ContextBroker(
            self.context_builder, registry, project_packs=ProjectPackRegistry(repository)
        )
        self.kernel = InvestigationKernel(repository)
        self.controller = controller
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._pending: dict[str, float] = {}
        self._queue_timer = None
        self._closed = False
        self._cleanup_task = None

    def start(self, investigation_id: str) -> None:
        if self._closed:
            return
        task = self._tasks.get(investigation_id)
        if task and not task.done():
            if self.controller is not None:
                self.controller.notify_event(investigation_id)
            self.repository.mark_workflow_started(investigation_id)
            return
        view = self.repository.get_investigation(investigation_id)
        if view.status in {InvestigationStatus.COMPLETED, InvestigationStatus.FAILED, InvestigationStatus.CANCELED}:
            self.repository.mark_workflow_started(investigation_id)
            return
        self._pending.setdefault(investigation_id, 0)
        self._drain_queue()

    def _drain_queue(self):
        if self._closed:
            return
        limit = self.execution_limits.snapshot()["investigations"]["limit"]
        if len(self._tasks) >= limit:
            return  # Completion callbacks wake the queue; no polling while all slots run.
        for investigation_id, ready_at in list(self._pending.items()):
            if len(self._tasks) >= limit:
                break
            if ready_at > time.monotonic():
                continue
            self._pending.pop(investigation_id)
            self._track(investigation_id, self._run_guarded(investigation_id))
        if self._pending and len(self._tasks) < limit and self._queue_timer is None:
            self._queue_timer = asyncio.get_running_loop().call_later(.25, self._retry_queue)

    def _retry_queue(self):
        self._queue_timer = None
        self._drain_queue()

    def _track(self, investigation_id: str, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks[investigation_id] = task

        def finished(done):
            if self._tasks.get(investigation_id) is done:
                self._tasks.pop(investigation_id, None)
                if self.controller is not None:
                    self.controller._operation_updates.pop(investigation_id, None)
                self._drain_queue()

        task.add_done_callback(finished)

    def recover(self) -> None:
        if self._cleanup_task is None and not self._closed:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        for investigation_id in self.repository.recoverable_local_investigations():
            if self.repository.get_investigation(investigation_id).status == InvestigationStatus.CANCELING:
                self._track(investigation_id, self._cancel_external(investigation_id, None))
            else:
                self.start(investigation_id)

    async def reconcile_terminal_cleanup(self):
        rows = self.repository._connection.execute("""SELECT c.investigation_id,MIN(c.updated_at) AS oldest
            FROM cleanup_operations c JOIN investigations i ON i.investigation_id=c.investigation_id
            WHERE c.state='pending' AND i.status IN ('completed','failed','canceled')
            AND NOT EXISTS (SELECT 1 FROM investigation_events e WHERE e.investigation_id=i.investigation_id
                AND e.event_type='investigation.backend_selected' AND json_extract(e.payload_json,'$.backend')='temporal')
            GROUP BY c.investigation_id ORDER BY oldest LIMIT 4""").fetchall()
        for row in rows:
            identity = row["investigation_id"]
            try:
                async with self.ownership.hold(identity, "local"):
                    # Rotate inaccessible resources and bound retry work independently
                    # of the terminal investigation's already-expired execution budget.
                    resources = self.repository.pending_cleanup_resources(identity)
                    for resource in resources:
                        self.repository.note_cleanup_attempt(resource["resource_id"], "local_cleanup_retry")
                    operations = getattr(self.controller, "native_operations", None)
                    if operations is not None:
                        await operations.cancel_investigation(identity)
                    executor = getattr(self.controller, "code_execution", None)
                    if executor is not None:
                        await asyncio.wait_for(executor.cancel_investigation(identity), 15)
                    actions = getattr(self.controller, "actions", None)
                    if actions is not None:
                        for action_id in self.repository.pending_cleanup_actions(identity)[:4]:
                            if await asyncio.wait_for(actions.cancel(action_id), 5):
                                self.repository.resolve_cleanup(action_id)
            except (WorkerOwnershipBusy, WorkerBackendMismatch, WorkerOwnershipLost):
                continue
            except Exception:
                logging.getLogger(__name__).warning("local terminal cleanup deferred")

    async def _cleanup_loop(self):
        while not self._closed:
            await self.reconcile_terminal_cleanup()
            await asyncio.sleep(5)

    async def _run_guarded(self, investigation_id: str) -> None:
        try:
            async with self.ownership.hold(investigation_id, "local"):
                status = self.repository.get_investigation(investigation_id).status
                if status in {InvestigationStatus.COMPLETED, InvestigationStatus.FAILED, InvestigationStatus.CANCELED}:
                    self.repository.mark_workflow_started(investigation_id)
                elif status == InvestigationStatus.CANCELING:
                    await self._cancel_external(investigation_id, None)
                else:
                    await self._run_owned_guarded(investigation_id)
        except WorkerOwnershipBusy:
            if not self._closed:
                self._pending[investigation_id] = time.monotonic() + .5
        except (WorkerOwnershipLost, WorkerBackendMismatch):
            logging.getLogger(__name__).warning("local worker ownership unavailable; investigation preserved")

    async def _run_owned_guarded(self, investigation_id: str) -> None:
        try:
            view = self.repository.get_investigation(investigation_id)
            deadline = view.created_at.timestamp() + view.request.budget.max_wall_seconds
            if deadline <= time.time():
                self.repository.mark_workflow_started(investigation_id)
                await self.run(investigation_id)
            else:
                async with self.execution_limits.slot("investigations", deadline=deadline, scope=(view.request.tenant_id, view.request.project_id)):
                    self.repository.mark_workflow_started(investigation_id)
                    with self.tracer.span(investigation_id, "investigation.run", backend="local"):
                        await self.run(investigation_id)
        except asyncio.CancelledError:
            raise
        except (WorkerOwnershipLost, WorkerBackendMismatch):
            raise
        except CapacityExceeded:
            # The investigation and unsent start already live in the durable ledger.
            # Keep it recoverable instead of turning transient pressure into failure.
            self._pending[investigation_id] = time.monotonic() + .25
            self._drain_queue()
        except TimeoutError:
            if time.time() >= deadline:
                await self.run(investigation_id)
            else:
                await finalize_failure(self.repository, self.reports, investigation_id, TimeoutError())
        except Exception as error:
            # Persist an observable failure without returning upstream exception text or credentials.
            logging.getLogger(__name__).warning("local workflow failed (%s)", type(error).__name__)
            await finalize_failure(self.repository, self.reports, investigation_id, error)
        finally:
            executor = getattr(self.controller, "code_execution", None)
            validate_owned_write(self.repository)
            view = self.repository.get_investigation(investigation_id)
            if executor is not None and view.status in {
                InvestigationStatus.COMPLETED, InvestigationStatus.FAILED,
                InvestigationStatus.CANCELING, InvestigationStatus.CANCELED,
            }:
                await executor.cancel_investigation(investigation_id)
            operations = getattr(self.controller, "native_operations", None)
            if operations is not None and view.status in {"completed", "failed", "canceling", "canceled"}:
                await operations.cancel_investigation(investigation_id)

    @owned_local
    async def run(self, investigation_id: str) -> None:
        if self.repository.get_investigation(investigation_id).status in {
            InvestigationStatus.COMPLETED, InvestigationStatus.FAILED, InvestigationStatus.CANCELED,
        }:
            return
        expected = getattr(self.controller, "execution_mode", "typed")
        if self.repository.investigation_execution_mode(investigation_id, expected) != expected:
            raise RuntimeError("investigation execution mode does not match this local owner")
        if self.controller is not None:
            authorization = self.repository.get_authorization(investigation_id)
            if expected == "native_mcp" and hasattr(self.controller, "run_step"):
                result = await self.controller.run_step(investigation_id,
                    principal_id=authorization["principal_id"], scopes=set(authorization["scopes"]))
                if not result.done and not self._closed:
                    # The completion callback returns this investigation to the
                    # queue tail, after other ready work, without holding a slot.
                    self._pending[investigation_id] = time.monotonic() + result.retry_after_seconds
                return
            await self.controller.run(
                investigation_id,
                principal_id=authorization["principal_id"],
                scopes=set(authorization["scopes"]),
            )
            return
        view = self.repository.get_investigation(investigation_id)
        if view.status != InvestigationStatus.ACCEPTED:
            # The provider configuration may have been removed between restarts.
            # Preserve unresolved external actions and finish with an explicit gap.
            if view.status not in {InvestigationStatus.COMPLETED, InvestigationStatus.FAILED,
                                   InvestigationStatus.CANCELED, InvestigationStatus.WAITING_INPUT}:
                for item in self.repository.list_actions(investigation_id):
                    if item["state"] not in {"succeeded", "failed", "canceled"}:
                        self.repository.record_cleanup_required(investigation_id, item["action"]["action_id"])
                for target in (InvestigationStatus.ANALYZING, InvestigationStatus.EVALUATING,
                               InvestigationStatus.REPORTING):
                    current = self.repository.get_investigation(investigation_id).status
                    if target in ALLOWED_TRANSITIONS.get(current, set()):
                        self._transition(investigation_id, target)
                report = self.report_builder.build(investigation_id, "required_capability_unavailable")
                self.reports.publish(report)
                self._transition(investigation_id, InvestigationStatus.COMPLETED,
                                 resolution=report.resolution, stop_reason=report.stop_reason)
            return
        self._transition(investigation_id, InvestigationStatus.DISCOVERING)
        authorization = self.repository.get_authorization(investigation_id)
        scopes = set(authorization["scopes"])
        capabilities = self.registry.discover(view.request.project_id, scopes=scopes)
        allowed_providers = view.request.constraints.get("provider_allowlist")
        if allowed_providers is not None:
            capabilities = [item for item in capabilities if item.provider_id in allowed_providers]
        self._transition(investigation_id, InvestigationStatus.PLANNING)
        if self.repository.get_latest_plan(investigation_id) is None:
            try:
                self.kernel.compile_objective(investigation_id, capabilities)
            except ValueError:
                pass
        context = self.context_broker.assemble(
            investigation_id,
            scopes=scopes,
            step_id="round-1",
            intent=view.request.objective.description,
        )
        preparation = self.kernel.prepare_round(
            context,
            plan_revision=1,
            model_name=self.investigator.model_name,
            prompt_version="investigator-v1",
        )
        next_step = None
        if not preparation.should_stop:
            if preparation.snapshot is None:
                raise RuntimeError("kernel did not freeze a reasoning context")
            result = await self.reasoning.reason(
                investigation_id,
                1,
                "next-step-1",
                preparation.snapshot.context.model_dump(mode="json", by_alias=True),
            )
            next_step = self.kernel.accept_decision(
                preparation.snapshot,
                result.output,
                model_profile_version=result.model_profile_version or "unversioned",
                raw_output_ref=result.raw_output_ref,
                normalization_log=result.normalization_log,
            ).decision
        # The development backend does not execute provider writes implicitly. It demonstrates
        # recovery and evidence-bound reporting; real actions are dispatched by provider activities.
        self._transition(investigation_id, InvestigationStatus.ANALYZING)
        self._transition(investigation_id, InvestigationStatus.EVALUATING)
        self._transition(investigation_id, InvestigationStatus.REPORTING)
        assessment = preparation.assessment
        stop_reason = preparation.stop_reason or (
            "provider_execution_requires_activity"
            if capabilities
            else "required_capability_unavailable"
        )
        report = self.report_builder.build(investigation_id, stop_reason)
        if assessment is None and next_step is not None:
            report = report.model_copy(update={"recommended_actions": [next_step.reason_summary]})
        self.reports.publish(report)
        self._transition(
            investigation_id,
            InvestigationStatus.COMPLETED,
            resolution=report.resolution,
            stop_reason=report.stop_reason,
        )

    def request_cancel(self, investigation_id: str) -> bool:
        view = self.repository.get_investigation(investigation_id)
        if view.status == InvestigationStatus.CANCELING:
            return True
        if view.status in {
            InvestigationStatus.COMPLETED,
            InvestigationStatus.CANCELED,
            InvestigationStatus.FAILED,
        }:
            return True
        self._pending.pop(investigation_id, None)
        task = self._tasks.get(investigation_id)
        if task and not task.done():
            task.cancel()
        self._transition(investigation_id, InvestigationStatus.CANCELING)
        if self.controller is None and (task is None or task.done()):
            try:
                with self.ownership.hold_now(investigation_id, "local"):
                    self._publish_cancellation(investigation_id)
                return True
            except WorkerOwnershipBusy:
                pass
            except (WorkerOwnershipLost, WorkerBackendMismatch):
                logging.getLogger(__name__).warning("local cancellation owner unavailable; cancellation preserved")
                return True
        self._track(investigation_id, self._cancel_external(investigation_id, task))
        return True

    async def _cancel_external(self, investigation_id: str, running_task) -> None:
        if running_task is not None:
            await asyncio.gather(running_task, return_exceptions=True)
        try:
            async with self.ownership.hold(investigation_id, "local"):
                if self.repository.get_investigation(investigation_id).status == InvestigationStatus.CANCELED:
                    return
                await self._cancel_owned(investigation_id)
        except WorkerOwnershipBusy:
            if not self._closed:
                self._pending[investigation_id] = time.monotonic() + .5
        except (WorkerOwnershipLost, WorkerBackendMismatch):
            logging.getLogger(__name__).warning("local cancellation owner unavailable; cancellation preserved")

    async def _cancel_owned(self, investigation_id: str) -> None:
        operations = getattr(self.controller, "native_operations", None)
        if operations is not None:
            await operations.cancel_investigation(investigation_id)
        code_execution = getattr(self.controller, "code_execution", None)
        if code_execution is not None:
            await code_execution.cancel_investigation(investigation_id)

        async def cancel_action(item):
            action_id = item["action"]["action_id"]
            try:
                accepted = await asyncio.wait_for(
                    self.controller.actions.cancel(action_id), timeout=15
                )
            except Exception:
                accepted = False
            if not accepted:
                self.repository.record_cleanup_required(investigation_id, action_id)

        await asyncio.gather(
            *(
                cancel_action(item)
                for item in self.repository.list_actions(investigation_id)
                if self.controller is not None
                if item["state"] not in {"succeeded", "failed", "canceled"}
            )
        )
        self._publish_cancellation(investigation_id)

    def _publish_cancellation(self, investigation_id: str) -> None:
        report = self.report_builder.build(
            investigation_id,
            "user_canceled",
            status="canceled",
            resolution=Resolution.CANCELED,
            summary="用户已停止继续调查；报告保留取消前已经取得的证据。",
        )
        self.reports.publish(report)
        self._transition(
            investigation_id,
            InvestigationStatus.CANCELED,
            resolution=Resolution.CANCELED,
            stop_reason="user_canceled",
        )
        # A canceled queued start is handled even though it never occupied a slot.
        self.repository.mark_workflow_started(investigation_id)

    async def close(self) -> None:
        self._closed = True
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            await asyncio.gather(self._cleanup_task, return_exceptions=True)
            self._cleanup_task = None
        self._pending.clear()
        if self._queue_timer is not None:
            self._queue_timer.cancel()
            self._queue_timer = None
        tasks = list(self._tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        close_controller = getattr(self.controller, 'close', None)
        if close_controller is not None:
            await close_controller()

    def _transition(
        self,
        investigation_id: str,
        target: InvestigationStatus,
        *,
        resolution: Resolution | None = None,
        stop_reason: str | None = None,
    ) -> None:
        current = self.repository.get_investigation(investigation_id).status
        if target not in ALLOWED_TRANSITIONS.get(current, set()):
            raise RuntimeError(f"invalid workflow transition: {current} -> {target}")
        self.repository.transition(
            investigation_id, target, resolution=resolution, stop_reason=stop_reason
        )
