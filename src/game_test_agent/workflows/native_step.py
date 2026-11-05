"""Bounded native worker turns; continuation packets carry references, not history."""
from __future__ import annotations

import asyncio
import hashlib
import time
from contextlib import asynccontextmanager
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from game_test_agent.analysis.permissions import CodePermissionStore
from game_test_agent.analysis.code_operations import ACTIVE_ANALYSES
from game_test_agent.workflows.native_context import NativeMemory
from game_test_agent.workflows.native_questions import input_fingerprint


class ContinuationPacket(BaseModel):
    model_config = ConfigDict(extra="forbid")
    investigation_id: str
    revision: int
    next_turn: int
    deadline_epoch: float
    input_fingerprint: str
    plan_revision: int | None = None
    checkpoint_ref: str | None = None
    checkpoint_version: int | None = None
    tool_reads: int
    model_tokens: int
    pending_permission_ids: list[str] = Field(default_factory=list, max_length=16)
    pending_permission_count: int = 0
    pending_analysis_ids: list[str] = Field(default_factory=list, max_length=16)
    pending_analysis_count: int = 0
    pending_cleanup_resource_count: int = 0


class WorkerStepResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: Literal["continue", "waiting", "completed", "failed", "canceled"]
    status: str
    continuation: ContinuationPacket
    wait_reason: Literal["permission", "owner", "cancellation", "analysis", "analysis_capacity", "mcp_operation"] | None = None
    retry_after_seconds: float = Field(default=0, ge=0, le=60)

    @property
    def done(self):
        return self.state in {"completed", "failed", "canceled"}

    def activity_result(self):
        return {"done": self.done, "status": self.status, "worker_step": self.model_dump(mode="json")}


def pending_permissions(controller, investigation_id):
    if controller.code_execution is None:
        return []
    return [row for row in CodePermissionStore(controller.repository).list(investigation_id)
            if row["status"] == "pending"]


def pending_analyses(controller, investigation_id):
    if controller.code_execution is None:
        return []
    return [job for job in controller.repository.list_analysis_jobs(investigation_id)
            if job.spec.environment.runner_id == controller.code_execution.runner.runner_id
            and job.spec.created_by.startswith("agent:") and job.state in ACTIVE_ANALYSES]


def waiting_analysis_capacity(controller, investigation_id, inputs, turn):
    if controller.execution_limits.available("analyses"):
        return False
    events = controller.repository.list_recent_events_by_type(
        investigation_id, "mcp.native.analysis_capacity_wait", limit=1)
    from game_test_agent.storage.feedback import FeedbackStore
    generation = FeedbackStore(controller.repository).generation(investigation_id)
    from game_test_agent.memory.recall import RecallService
    history_generation = RecallService(controller.repository).generation(investigation_id)
    return bool(events and events[0]["data"]["turn"] == turn
                and events[0]["data"].get("feedback_generation", 0) == generation
                and events[0]["data"].get("history_generation", 0) == history_generation
                and events[0]["data"]["input_fingerprint"] == input_fingerprint(inputs))


def observe_step(controller, investigation_id, *, waiting=None):
    repo = controller.repository
    view = repo.get_investigation(investigation_id)
    inputs = repo.list_user_inputs(investigation_id)
    usage = repo.budget_usage(investigation_id)
    checkpoints = repo.list_recent_events_by_type(investigation_id, "mcp.native.checkpoint", limit=1)
    checkpoint = checkpoints[0]["data"] if checkpoints else {}
    ref = checkpoint.get("artifact_ref")
    if ref and not controller.artifacts.validate_scope(ref, view.request.tenant_id, view.request.project_id):
        raise PermissionError("worker checkpoint belongs to another project")
    plans = repo.list_recent_events_by_type(investigation_id, "mcp.native.plan_updated", limit=1)
    pending = pending_permissions(controller, investigation_id)
    analyses = pending_analyses(controller, investigation_id)
    status = str(view.status)
    if status in {"completed", "failed", "canceled"}:
        state, waiting = status, None
    elif status == "canceling":
        state, waiting = "waiting", "cancellation"
    else:
        if not waiting:
            if getattr(controller, "native_operations", None) and controller.native_operations.pending(investigation_id):
                waiting = "mcp_operation"
            elif analyses:
                waiting = "analysis" if any(j.operation_ref for j in analyses) else "analysis_capacity"
            elif pending:
                waiting = "permission"
            elif waiting_analysis_capacity(controller, investigation_id, inputs, int(usage.get("native_model_turns", 0))):
                waiting = "analysis_capacity"
        state = "waiting" if waiting else "continue"
    return WorkerStepResult(state=state, status=status, wait_reason=waiting,
        retry_after_seconds=1 if waiting else 0,
        continuation=ContinuationPacket(investigation_id=investigation_id, revision=view.revision,
            next_turn=int(usage.get("native_model_turns", 0)) + 1,
            deadline_epoch=view.created_at.timestamp() + view.request.budget.max_wall_seconds,
            input_fingerprint=input_fingerprint(inputs), plan_revision=plans[0]["data"]["revision"] if plans else None,
            checkpoint_ref=ref, checkpoint_version=checkpoint.get("version"),
            tool_reads=int(usage.get("tool_reads", 0)),
            model_tokens=repo.native_model_usage(investigation_id)["tokens"],
            pending_permission_ids=[row["approval_id"] for row in pending[:16]],
            pending_permission_count=len(pending), pending_analysis_count=len(analyses),
            pending_analysis_ids=[job.spec.job_id for job in analyses[:16]],
            pending_cleanup_resource_count=len(repo.pending_cleanup_resources(investigation_id))))


async def execute_worker_step(controller, investigation_id, *, principal_id, scopes, step_id=None):
    """At most one model turn; all execution state is reloaded from the ledger."""
    if not {"runs:read", "evidence:read"}.issubset(scopes):
        raise PermissionError("native worker requires runs:read and evidence:read")
    if step_id is not None and (not isinstance(step_id, str) or not 1 <= len(step_id) <= 200):
        raise ValueError("step_id must contain 1–200 characters")
    repo = controller.repository
    if repo.investigation_execution_mode(investigation_id, "native_mcp") != "native_mcp":
        raise RuntimeError("native worker requires its native execution owner")
    lock = controller._step_locks.setdefault(investigation_id, asyncio.Lock())
    if lock.locked():
        return observe_step(controller, investigation_id, waiting="owner")
    async with _step_ownership(controller, investigation_id, lock):
        current = observe_step(controller, investigation_id)
        if current.done or current.wait_reason == "cancellation":
            return current
        journal_key = "mcp.native.step_result." + hashlib.sha256(step_id.encode()).hexdigest() if step_id else None
        if journal_key:
            saved = repo.list_recent_events_by_type(investigation_id, journal_key, limit=1)
            if saved:
                return WorkerStepResult.model_validate(saved[0]["data"]["result"])
            started = repo.list_recent_events_by_type(investigation_id, journal_key + ".started", limit=1)
            if started and current.continuation.next_turn > started[0]["data"]["next_turn"]:
                # A process can die after reserving/committing the model turn,
                # before writing its step acknowledgement. Do not run the next
                # turn as a retry of the same request; the next step reloads
                # checkpoints and completed call journals through run().
                _record_step(repo, investigation_id, step_id, journal_key, current)
                return current
            if not started:
                repo.record_investigation_event(investigation_id, journal_key + ".started",
                    {"next_turn": current.continuation.next_turn})
        pending = pending_permissions(controller, investigation_id)
        if (current.wait_reason == "analysis_capacity" and not pending_analyses(controller, investigation_id)
                and time.time() < current.continuation.deadline_epoch):
            _record_step(repo, investigation_id, step_id, journal_key, current)
            return current
        if pending_analyses(controller, investigation_id):
            # Poll/collect persistent work before loading schemas or invoking the
            # model. The external job keeps its analysis permit; polling uses a
            # short control slot and releases the investigation when still busy.
            async with controller.execution_limits.slot("tools",
                    deadline=current.continuation.deadline_epoch if time.time() < current.continuation.deadline_epoch else None):
                feedback = await controller.code_execution.resume_investigation(investigation_id,
                    wait_for_permission=False, wait_for_completion=False)
            view = repo.get_investigation(investigation_id)
            memory = NativeMemory.restore(repo, controller.artifacts, view)
            if memory is not None:
                memory.sync_inputs(repo, investigation_id)
                for payload in feedback:
                    controller._remember_code_result(memory, payload)
                memory.save(repo, controller.artifacts, view)
            if pending_analyses(controller, investigation_id):
                if time.time() >= current.continuation.deadline_epoch:
                    # End the investigation on time without inventing proof
                    # that an unresponsive external operation stopped.
                    if view.status == "accepted":
                        repo.transition(investigation_id, "discovering")
                    controller._finish(view, None, "wall_clock_budget_exhausted", memory=memory)
                result = observe_step(controller, investigation_id)
                _record_step(repo, investigation_id, step_id, journal_key, result)
                return result
        # A new follow-up must supersede a waiting action before any code resumes.
        store = CodePermissionStore(repo)
        superseded = any(store.input_superseded(record) for record in pending)
        if not (current.wait_reason == "permission" and not superseded
                and time.time() < current.continuation.deadline_epoch):
            await controller.run(investigation_id, principal_id=principal_id, scopes=scopes,
                                 max_model_turns=1, yield_permissions=True)
        result = observe_step(controller, investigation_id)
        _record_step(repo, investigation_id, step_id, journal_key, result)
        return result


def _record_step(repo, investigation_id, step_id, journal_key, result):
    payload = {"step_id": step_id, "result": result.model_dump(mode="json")}
    with repo.transaction() as connection:
        repo._insert_event(connection, investigation_id, "mcp.native.worker_step", payload)
        if journal_key:
            repo._insert_event(connection, investigation_id, journal_key, payload)


@asynccontextmanager
async def _step_ownership(controller, investigation_id, lock):
    try:
        async with lock:
            yield
    finally:
        if not lock.locked() and controller._step_locks.get(investigation_id) is lock:
            controller._step_locks.pop(investigation_id, None)
