from __future__ import annotations

from datetime import datetime, timezone
import asyncio

from game_test_agent.contracts.models import (
    ActionPermit,
    ActionSpec,
    ActionState,
    OperationRef,
    RunResult,
    ResourceRequest,
)
from game_test_agent.mcp_gateway import ActionDispatcher, StandardToolError
from game_test_agent.policies import PolicyEngine
from game_test_agent.provenance.projections import ProjectionCoordinator
from game_test_agent.storage import ConflictError, Repository
from game_test_agent.scheduling import DurableResourceScheduler
from game_test_agent.observability.tracing import trace_operation


class ActionService:
    """Durable action boundary used by local and Temporal activities."""

    def __init__(
        self,
        repository: Repository,
        policy: PolicyEngine,
        dispatcher: ActionDispatcher,
        scheduler: DurableResourceScheduler | None = None,
    ) -> None:
        self.repository = repository
        self.policy = policy
        self.dispatcher = dispatcher
        self.scheduler = scheduler or DurableResourceScheduler(repository)
        self.projections = ProjectionCoordinator(repository)

    @trace_operation("action.submit", identity="object")
    async def submit(
        self, action: ActionSpec, *, principal_id: str, scopes: set[str]
    ) -> OperationRef:
        decision_record = self.repository.get_decision_record(
            action.investigation_id, action.plan_revision
        )
        required_cursor = (
            decision_record.envelope.source_cursor
            if decision_record is not None and decision_record.envelope is not None
            else self.repository.latest_event_cursor(action.investigation_id)
        )
        self.projections.refresh(action.investigation_id)
        self.projections.assert_fresh(action.investigation_id, required_cursor=required_cursor)
        decision = self.policy.reserve(action, principal_id=principal_id, scopes=scopes)
        action = decision.action
        _, state, stored = self.repository.get_action(action.action_id)
        if not decision.created and stored is not None:
            return OperationRef.model_validate(stored)
        if not decision.created and state in {
            ActionState.SUCCEEDED,
            ActionState.FAILED,
            ActionState.CANCELED,
        }:
            self._release_admission(action.action_id)
            raise ConflictError(f"terminal action {action.action_id} cannot be submitted again")
        if state == ActionState.UNKNOWN:
            found = await self.dispatcher.lookup(action)
            if found is not None:
                self.repository.transition_action(
                    action.action_id,
                    {ActionState.UNKNOWN},
                    ActionState.ACCEPTED,
                    operation_ref=found,
                )
                return found
            capability = self.policy.registry.resolve(
                action.project_id,
                action.provider_id,
                action.capability_id,
                action.capability_version,
                scopes=scopes,
            )
            if capability.idempotency != "required":
                raise StandardToolError(
                    "UNKNOWN_SIDE_EFFECT",
                    "provider could not reconcile the action and does not guarantee idempotency",
                    action_id=action.action_id,
                    side_effect_status="unknown",
                )
        permit = self._ensure_admission(action, decision.permit, scopes=scopes)
        if state == ActionState.UNKNOWN:
            self.repository.transition_action(
                action.action_id, {ActionState.UNKNOWN}, ActionState.DISPATCHING
            )
        if state == ActionState.RESERVED:
            self.repository.transition_action(
                action.action_id, {ActionState.RESERVED}, ActionState.DISPATCHING
            )
        try:
            operation = await self.dispatcher.dispatch(action, permit)
        except asyncio.CancelledError:
            self.repository.transition_action(
                action.action_id, {ActionState.DISPATCHING}, ActionState.UNKNOWN
            )
            self.repository.record_cleanup_required(action.investigation_id, action.action_id)
            raise
        except StandardToolError as error:
            target = (
                ActionState.UNKNOWN if error.side_effect_status == "unknown" else ActionState.FAILED
            )
            self.repository.transition_action(action.action_id, {ActionState.DISPATCHING}, target)
            if target == ActionState.FAILED and permit.admission_ticket_id:
                self.scheduler.release(permit.admission_ticket_id)
            raise
        except Exception as error:
            # An unclassified adapter exception cannot prove that no run was committed.
            self.repository.transition_action(
                action.action_id, {ActionState.DISPATCHING}, ActionState.UNKNOWN
            )
            self.repository.record_cleanup_required(action.investigation_id, action.action_id)
            raise StandardToolError(
                "PROVIDER_OUTCOME_UNKNOWN", "provider submission requires reconciliation",
                action_id=action.action_id, side_effect_status="unknown",
            ) from error
        target = ActionState.ACCEPTED if operation.accepted else ActionState.FAILED
        self.repository.transition_action(
            action.action_id,
            {ActionState.DISPATCHING},
            target,
            operation_ref=operation,
        )
        if target == ActionState.FAILED and permit.admission_ticket_id:
            self.scheduler.release(permit.admission_ticket_id)
        return operation

    def _ensure_admission(
        self, action: ActionSpec, permit: ActionPermit, *, scopes: set[str]
    ) -> ActionPermit:
        active_ticket = self.repository.get_admission_ticket(
            action.investigation_id, action.action_id, active_only=True
        )
        if active_ticket is not None and active_ticket.expires_at <= datetime.now(timezone.utc):
            active_ticket = None
        if permit.admission_ticket_id is None or active_ticket is None:
            capability = self.policy.registry.resolve(
                action.project_id,
                action.provider_id,
                action.capability_id,
                action.capability_version,
                scopes=scopes,
            )
            investigation = self.repository.get_investigation(action.investigation_id)
            priority = {"low": -25, "normal": 0, "high": 25, "critical": 50}[
                investigation.request.objective.priority
            ]
            ticket = self.scheduler.acquire(
                ResourceRequest(
                    project_id=action.project_id,
                    task_id=action.investigation_id,
                    node_id=action.action_id,
                    plan_step_id=action.plan_step_id,
                    plan_node_revision=action.plan_node_revision,
                    resource_domain=capability.resource_domain,
                    device_minutes=permit.reserved_device_minutes,
                    priority=priority,
                    exclusive=True,
                    ttl_seconds=min(86400, max(600, investigation.request.budget.max_wall_seconds + 60)),
                )
            )
            if ticket is None:
                raise StandardToolError(
                    "RESOURCE_BUSY",
                    "action is queued for resource admission",
                    retryable=True,
                    retry_after_ms=1000,
                    action_id=action.action_id,
                )
            permit = self.repository.bind_action_admission(action.action_id, ticket)
        if (
            permit.resource_domain is None
            or permit.fencing_token is None
            or not self.scheduler.accepts_fencing_token(
                permit.resource_domain, permit.fencing_token
            )
        ):
            raise StandardToolError(
                "STALE_ADMISSION",
                "action admission fencing token is no longer current",
                retryable=False,
                action_id=action.action_id,
            )
        return permit

    @trace_operation("action.reconcile", identity="action_id")
    async def reconcile(
        self, action_id: str, *, read_reservation_id: str | None = None
    ) -> OperationRef | RunResult | None:
        action, state, operation_data = self.repository.get_action(action_id)
        if read_reservation_id is not None:
            investigation = self.repository.get_investigation(action.investigation_id)
            self.repository.reserve_counter(
                action.investigation_id,
                "tool_reads",
                read_reservation_id,
                investigation.request.budget.max_tool_reads,
            )
        if state in {ActionState.SUCCEEDED, ActionState.FAILED, ActionState.CANCELED}:
            self._release_admission(action_id)
            snapshot = self.repository.get_latest_run_result(action_id)
            if snapshot and snapshot.result.execution_status in {"succeeded", "failed", "canceled"}:
                self.repository.resolve_cleanup(action_id)
            return snapshot.result if snapshot else None
        if state == ActionState.UNKNOWN:
            found = await self.dispatcher.lookup(action)
            if found is None:
                return None
            self.repository.transition_action(
                action_id, {ActionState.UNKNOWN}, ActionState.ACCEPTED, operation_ref=found
            )
            operation_data = found.model_dump(mode="json")
        if not operation_data:
            return None
        operation = OperationRef.model_validate(operation_data)
        if not operation.operation_id:
            return operation
        result = await self.dispatcher.query(action.provider_id, operation.operation_id)
        snapshot, _ = self.repository.record_run_result(action_id, result)
        current = self.repository.get_action(action_id)[1]
        if current in {ActionState.SUCCEEDED, ActionState.FAILED, ActionState.CANCELED}:
            self.repository.resolve_cleanup(action_id)
            permit = self.repository.get_action_permit(action_id)
            if permit.admission_ticket_id:
                self.scheduler.release(
                    permit.admission_ticket_id,
                    consumed_device_minutes=permit.reserved_device_minutes,
                )
        return snapshot.result

    @trace_operation("action.cancel", identity="action_id")
    async def cancel(self, action_id: str) -> bool:
        action, state, _ = self.repository.get_action(action_id)
        terminal = {ActionState.SUCCEEDED, ActionState.FAILED, ActionState.CANCELED}
        if state in terminal:
            return state == ActionState.CANCELED
        if state == ActionState.RESERVED:
            self.repository.transition_action(
                action_id, {ActionState.RESERVED}, ActionState.CANCELED
            )
            permit = self.repository.get_action_permit(action_id)
            if permit.admission_ticket_id:
                self.scheduler.cancel(permit.admission_ticket_id)
            else:
                self.scheduler.cancel_request(action.investigation_id, action.action_id)
            return True
        try:
            response = await self.dispatcher.cancel(action)
        except StandardToolError as error:
            if error.side_effect_status == "unknown" and state != ActionState.UNKNOWN:
                self.repository.transition_action(
                    action_id,
                    {state},
                    ActionState.UNKNOWN,
                )
            return False
        if not bool(response.get("accepted", False)):
            return False
        current = self.repository.get_action(action_id)[1]
        if current in terminal:
            return current == ActionState.CANCELED
        self.repository.transition_action(
            action_id,
            {
                ActionState.DISPATCHING,
                ActionState.ACCEPTED,
                ActionState.WAITING,
                ActionState.UNKNOWN,
            },
            ActionState.CANCELED,
        )
        permit = self.repository.get_action_permit(action_id)
        if permit.admission_ticket_id:
            self.scheduler.cancel(permit.admission_ticket_id)
        return True

    def _release_admission(self, action_id: str) -> None:
        permit = self.repository.get_action_permit(action_id)
        if permit.admission_ticket_id:
            self.scheduler.release(
                permit.admission_ticket_id,
                consumed_device_minutes=permit.reserved_device_minutes,
            )
