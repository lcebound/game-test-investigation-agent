from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from game_test_agent.workflows.waiting import poll_delay

try:
    from temporalio import workflow
    from temporalio.common import RetryPolicy
    from temporalio.exceptions import ActivityError
    from temporalio.workflow import ParentClosePolicy, ActivityCancellationType
except ImportError:  # pragma: no cover - optional production dependency
    workflow = None


@dataclass
class WorkflowState:
    investigation_id: str
    canceled: bool = False
    input_revision: int = 0
    evidence_revision: int = 0
    processed_signal_ids: set[str] = field(default_factory=set)


@dataclass
class WorkflowRunInput:
    investigation_id: str
    next_plan_revision: int = 1
    deadline_epoch: float | None = None
    input_revision: int = 0
    evidence_revision: int = 0
    processed_signal_ids: list[str] = field(default_factory=list)
    continued: bool = False


if workflow is not None:

    @workflow.defn(name="game-test-cleanup-v1")
    class CleanupWorkflow:
        @workflow.run
        async def run(self, investigation_id: str) -> list[str]:
            outstanding: list[str] = []
            for attempt in range(144):
                outstanding = await workflow.execute_activity(
                    "reconcile_cleanup",
                    args=[investigation_id],
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=RetryPolicy(
                        maximum_attempts=3,
                        initial_interval=timedelta(seconds=5),
                        maximum_interval=timedelta(minutes=1),
                    ),
                )
                if not outstanding:
                    return []
                if attempt < 143:
                    await workflow.sleep(
                        timedelta(minutes=10),
                        summary="wait before external cleanup reconciliation",
                    )
            return outstanding

    @workflow.defn(name="game-test-investigation-v1")
    class InvestigationWorkflow:
        """Durable Plan–Act–Observe–Evaluate loop with bounded activities."""

        CONTINUE_AFTER_ROUNDS = 5

        def __init__(self) -> None:
            # Signals may be delivered before the run method executes. Initialize
            # eagerly and let run attach the investigation identity without
            # replacing signal-derived state.
            self.state = WorkflowState(investigation_id="")

        @workflow.signal
        async def cancel(self, command_id: str) -> None:
            if command_id not in self.state.processed_signal_ids:
                self.state.processed_signal_ids.add(command_id)
                self.state.canceled = True

        @workflow.signal(name="input")
        async def receive_input(self, command: dict) -> None:
            command_id = str(command["command_id"])
            if command_id not in self.state.processed_signal_ids:
                self.state.processed_signal_ids.add(command_id)
                self.state.input_revision += 1

        @workflow.signal(name="evidence_updated")
        async def evidence_updated(self, command: dict) -> None:
            command_id = str(command["command_id"])
            if command_id not in self.state.processed_signal_ids:
                self.state.processed_signal_ids.add(command_id)
                self.state.evidence_revision += 1

        @workflow.query(name="state")
        def state_snapshot(self) -> dict:
            return {
                "investigation_id": self.state.investigation_id,
                "canceled": self.state.canceled,
                "input_revision": self.state.input_revision,
                "evidence_revision": self.state.evidence_revision,
                "processed_signal_count": len(self.state.processed_signal_ids),
            }

        def _restore_input(self, value: str | dict | WorkflowRunInput) -> WorkflowRunInput:
            if isinstance(value, str):
                restored = WorkflowRunInput(investigation_id=value)
            elif isinstance(value, dict):
                restored = WorkflowRunInput(**value)
            else:
                restored = value
            # Signals can arrive before run on both the first and continued run.
            early_input = self.state.input_revision
            early_evidence = self.state.evidence_revision
            early_canceled = self.state.canceled
            early_ids = self.state.processed_signal_ids
            self.state = WorkflowState(
                investigation_id=restored.investigation_id,
                canceled=early_canceled,
                input_revision=restored.input_revision + early_input,
                evidence_revision=restored.evidence_revision + early_evidence,
                processed_signal_ids=set(restored.processed_signal_ids) | early_ids,
            )
            return restored

        async def _activity(
            self,
            name: str,
            args: list,
            *,
            timeout: timedelta = timedelta(minutes=2),
            attempts: int = 3,
        ):
            return await workflow.execute_activity(
                name,
                args=args,
                start_to_close_timeout=timeout,
                retry_policy=RetryPolicy(
                    maximum_attempts=attempts,
                    initial_interval=timedelta(seconds=1),
                    maximum_interval=timedelta(seconds=15),
                    backoff_coefficient=2,
                ),
            )

        async def _set_status(self, investigation_id: str, status: str) -> None:
            await self._activity(
                "project_status", [investigation_id, status], timeout=timedelta(seconds=30)
            )

        async def _execute_code(self, investigation_id: str, plan_revision: int) -> bool:
            execution = workflow.start_activity(
                "execute_readonly_decision", args=[investigation_id, plan_revision],
                start_to_close_timeout=timedelta(minutes=12),
                heartbeat_timeout=timedelta(seconds=15),
                retry_policy=RetryPolicy(maximum_attempts=3),
                cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
            )
            cancellation = asyncio.create_task(workflow.wait_condition(lambda: self.state.canceled))
            try:
                await asyncio.wait({execution, cancellation}, return_when=asyncio.FIRST_COMPLETED)
                if self.state.canceled:
                    execution.cancel()
                    try:
                        await execution
                    except (asyncio.CancelledError, ActivityError):
                        pass
                    return False
                await execution
                return True
            finally:
                cancellation.cancel()

        async def _cancel(self, investigation_id: str) -> str:
            outstanding = await self._activity(
                "cancel_investigation",
                [investigation_id],
                timeout=timedelta(minutes=2),
                attempts=5,
            )
            if outstanding:
                await workflow.start_child_workflow(
                    CleanupWorkflow.run,
                    investigation_id,
                    id=f"cleanup/{investigation_id}",
                    parent_close_policy=ParentClosePolicy.ABANDON,
                    execution_timeout=timedelta(hours=25),
                )
            return "canceled"

        async def _run_native(self, investigation_id: str, control: dict, *, defer_capacity=False,
                              worker_steps=False) -> str:
            """Only orchestration lives here; Local and Temporal execute the same native rounds."""
            timeout = timedelta(seconds=int(control["budget"]["max_wall_seconds"]) + 60)
            rounds, deferrals = 0, 0
            while rounds < int(control["budget"]["max_rounds"]) + 1:
                if self.state.canceled:
                    return await self._cancel(investigation_id)
                execution = workflow.start_activity(
                    "native_investigation_step", args=[investigation_id, True] if worker_steps else [investigation_id],
                    start_to_close_timeout=timeout, heartbeat_timeout=timedelta(seconds=15),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                    cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
                )
                canceled = asyncio.create_task(workflow.wait_condition(lambda: self.state.canceled))
                try:
                    wait = getattr(workflow, "wait", asyncio.wait) if defer_capacity else asyncio.wait
                    await wait({execution, canceled}, return_when=asyncio.FIRST_COMPLETED)
                    if self.state.canceled:
                        execution.cancel()
                        try:
                            await execution
                        except (asyncio.CancelledError, ActivityError):
                            pass
                        return await self._cancel(investigation_id)
                    result = await execution
                finally:
                    canceled.cancel()
                if result["done"]:
                    if (worker_steps and result.get("worker_step", {}).get("continuation", {})
                            .get("pending_cleanup_resource_count", 0)
                            and workflow.patched("native-code-cleanup-v1")):
                        await workflow.start_child_workflow(
                            CleanupWorkflow.run, investigation_id, id=f"cleanup/{investigation_id}",
                            parent_close_policy=ParentClosePolicy.ABANDON,
                            execution_timeout=timedelta(hours=25))
                    return result["status"]
                if worker_steps and result.get("worker_step", {}).get("state") == "waiting":
                    deferrals += 1
                    if deferrals >= 100:
                        workflow.continue_as_new(WorkflowRunInput(
                            investigation_id=investigation_id, continued=True,
                            input_revision=self.state.input_revision, evidence_revision=self.state.evidence_revision,
                            processed_signal_ids=sorted(self.state.processed_signal_ids)))
                    before_input, before_evidence = self.state.input_revision, self.state.evidence_revision
                    try:
                        await workflow.wait_condition(lambda: self.state.canceled
                            or self.state.input_revision != before_input or self.state.evidence_revision != before_evidence,
                            timeout=timedelta(seconds=max(.25, min(60, result["worker_step"].get("retry_after_seconds", 1)))))
                    except TimeoutError:
                        pass
                    continue  # Waiting does not spend the model-round bound.
                if defer_capacity and result.get("capacity_deferred"):
                    deferrals += 1
                    if deferrals >= 100:
                        workflow.continue_as_new(WorkflowRunInput(
                            investigation_id=investigation_id, continued=True,
                            input_revision=self.state.input_revision,
                            evidence_revision=self.state.evidence_revision,
                            processed_signal_ids=sorted(self.state.processed_signal_ids)))
                    try:
                        await workflow.wait_condition(lambda: self.state.canceled,
                                                      timeout=timedelta(seconds=1))
                    except TimeoutError:
                        pass
                    continue
                rounds += 1
            raise RuntimeError("native investigation exceeded its durable round bound")

        @workflow.run
        async def run(self, workflow_input: str | dict | WorkflowRunInput) -> str:
            restored = self._restore_input(workflow_input)
            investigation_id = restored.investigation_id
            control = await self._activity(
                "load_control_state", [investigation_id], timeout=timedelta(seconds=30)
            )
            budget = control["budget"]
            if workflow.patched("native-mcp-rounds-v1") and control.get("execution_mode") == "native_mcp":
                return await self._run_native(investigation_id, control,
                    defer_capacity=workflow.patched("native-capacity-wait-v1"),
                    worker_steps=workflow.patched("native-worker-step-v1"))
            trusted_execution_v2 = workflow.patched("trusted-execution-routing-v2")
            objective_assessment_v2 = workflow.patched("objective-assessment-v2")
            scoped_poll_budget = workflow.patched("round-scoped-poll-budget-v1")
            read_analysis_loop = workflow.patched("read-analysis-loop-v1")
            paced_polling = workflow.patched("deadline-paced-status-polls-v1")
            remaining_reads = int(budget["max_tool_reads"]) - int(control.get("tool_reads", 0))
            reserve_reads = min(8, int(budget["max_tool_reads"]) // 4) if paced_polling else 0
            deadline = (
                datetime.fromtimestamp(restored.deadline_epoch, timezone.utc)
                if restored.deadline_epoch is not None
                else workflow.now() + timedelta(seconds=int(budget["max_wall_seconds"]))
            )
            if paced_polling and restored.deadline_epoch is None and control.get("created_at"):
                deadline = datetime.fromisoformat(control["created_at"]) + timedelta(
                    seconds=int(budget["max_wall_seconds"])
                )
            if not restored.continued:
                await self._set_status(investigation_id, "discovering")
                await self._activity(
                    "discover_capabilities", [investigation_id], timeout=timedelta(minutes=2)
                )
                await self._set_status(investigation_id, "planning")

            stop_reason = "reasoning_round_budget_exhausted"
            for plan_revision in range(restored.next_plan_revision, int(budget["max_rounds"]) + 1):
                if self.state.canceled:
                    return await self._cancel(investigation_id)
                if workflow.now() >= deadline:
                    stop_reason = "wall_clock_budget_exhausted"
                    break
                if objective_assessment_v2:
                    try:
                        assessment = await self._activity(
                            "evaluate_objective",
                            [investigation_id],
                            timeout=timedelta(seconds=30),
                            attempts=1,
                        )
                    except ActivityError:
                        stop_reason = "objective_spec_invalid"
                        break
                    if assessment and assessment["verdict"] in {"passed", "failed"}:
                        stop_reason = f"objective_{assessment['verdict']}"
                        break
                try:
                    next_step = await self._activity(
                        "reason_next_step",
                        [investigation_id, plan_revision],
                        timeout=timedelta(minutes=3),
                        attempts=2,
                    )
                except ActivityError:
                    stop_reason = "reasoning_activity_failed"
                    break
                if next_step["kind"] == "report":
                    stop_reason = next_step.get("reason_summary") or "no_valuable_action"
                    break
                if next_step["kind"] == "request_input":
                    await self._set_status(investigation_id, "waiting_input")
                    input_revision = self.state.input_revision
                    remaining = max((deadline - workflow.now()).total_seconds(), 0)
                    try:
                        await workflow.wait_condition(
                            lambda: self.state.canceled
                            or self.state.input_revision > input_revision,
                            timeout=remaining,
                            timeout_summary="wait for investigation input",
                        )
                    except TimeoutError:
                        await self._set_status(investigation_id, "planning")
                        stop_reason = "input_deadline_exhausted"
                        break
                    if self.state.canceled:
                        return await self._cancel(investigation_id)
                    await self._set_status(investigation_id, "planning")
                    continue

                if trusted_execution_v2 and next_step["kind"] in {
                    "retrieve_evidence", "search_evidence", "analyze", "execute_code"
                }:
                    await self._set_status(investigation_id, "analyzing")
                    if read_analysis_loop:
                        try:
                            if next_step["kind"] == "execute_code":
                                if not await self._execute_code(investigation_id, plan_revision):
                                    return await self._cancel(investigation_id)
                            else:
                                await self._activity(
                                    "execute_readonly_decision", [investigation_id, plan_revision],
                                    timeout=timedelta(minutes=2), attempts=1,
                                )
                        except ActivityError:
                            await self._set_status(investigation_id, "evaluating")
                            stop_reason = "readonly_operation_failed"
                            break
                    await self._set_status(investigation_id, "evaluating")
                    if plan_revision < int(budget["max_rounds"]):
                        await self._set_status(investigation_id, "planning")
                    continue

                await self._set_status(investigation_id, "acting")
                if trusted_execution_v2 and next_step["kind"] == "query_run":
                    action_id = next_step["target_action_id"]
                    poll_interval = 0.1
                else:
                    try:
                        submitted = await self._activity(
                            "plan_and_submit_action",
                            [investigation_id, plan_revision, next_step],
                            timeout=timedelta(minutes=3),
                            attempts=5,
                        )
                    except ActivityError:
                        await self._set_status(investigation_id, "analyzing")
                        await self._set_status(investigation_id, "evaluating")
                        stop_reason = "dispatch_activity_failed"
                        break
                    if not submitted["ok"]:
                        await self._set_status(investigation_id, "analyzing")
                        await self._set_status(investigation_id, "evaluating")
                        stop_reason = submitted["stop_reason"]
                        if paced_polling and submitted.get("repairable") and plan_revision < int(budget["max_rounds"]):
                            await self._set_status(investigation_id, "planning")
                            continue
                        break
                    action_id = submitted["action_id"]
                    poll_interval = float(
                        submitted["operation"].get("suggested_poll_interval_seconds", 5)
                    )

                await self._set_status(investigation_id, "waiting_external")
                result = None
                poll_number = 0
                while workflow.now() < deadline:
                    if self.state.canceled:
                        return await self._cancel(investigation_id)
                    evidence_revision = self.state.evidence_revision
                    delay = min(max(poll_interval, 0.1), 60)
                    if paced_polling:
                        remaining = max((deadline - workflow.now()).total_seconds(), 0)
                        delay = min(delay, remaining) if poll_number < 2 else poll_delay(
                            remaining, remaining_reads - reserve_reads, poll_interval
                        )
                    try:
                        await workflow.wait_condition(
                            lambda: self.state.canceled
                            or self.state.evidence_revision > evidence_revision,
                            timeout=delay,
                            timeout_summary="wait for provider operation or evidence update",
                        )
                    except TimeoutError:
                        pass
                    poll_number += 1
                    try:
                        observation = await self._activity(
                            "query_action",
                            (
                                [action_id, poll_number, plan_revision, reserve_reads]
                                if paced_polling
                                else [action_id, poll_number, plan_revision]
                                if scoped_poll_budget
                                else [action_id, poll_number]
                            ),
                            timeout=timedelta(minutes=1),
                            attempts=5,
                        )
                    except ActivityError:
                        stop_reason = "operation_query_failed"
                        break
                    if observation["kind"] == "error":
                        stop_reason = observation["stop_reason"]
                        break
                    remaining_reads = int(observation.get("remaining_reads", remaining_reads - 1))
                    if observation["kind"] == "run_result":
                        candidate = observation["value"]
                        if candidate["execution_status"] in {
                            "succeeded",
                            "failed",
                            "canceled",
                            "unknown",
                        }:
                            result = candidate
                            break
                if result is None and workflow.now() >= deadline:
                    stop_reason = "wall_clock_budget_exhausted"

                await self._set_status(investigation_id, "analyzing")
                evidence_count = 0
                if result and (trusted_execution_v2 or result["execution_status"] == "succeeded"):
                    try:
                        ingestion_args = (
                            [investigation_id, action_id, result]
                            if trusted_execution_v2
                            else [investigation_id, result]
                        )
                        evidence_count = await self._activity(
                            "ingest_run_evidence",
                            ingestion_args,
                            timeout=timedelta(minutes=5),
                            attempts=3,
                        )
                        stop_reason = (
                            "completed_with_available_evidence"
                            if evidence_count
                            else "completed_without_evidence"
                        )
                    except ActivityError:
                        stop_reason = "evidence_ingestion_failed"
                        result = None
                if result and result["execution_status"] != "succeeded":
                    stop_reason = f"external_{result['execution_status']}"
                await self._set_status(investigation_id, "evaluating")
                if objective_assessment_v2:
                    try:
                        assessment = await self._activity(
                            "evaluate_objective",
                            [investigation_id],
                            timeout=timedelta(seconds=30),
                            attempts=1,
                        )
                    except ActivityError:
                        stop_reason = "objective_spec_invalid"
                        break
                    if assessment and assessment["verdict"] in {"passed", "failed"}:
                        stop_reason = f"objective_{assessment['verdict']}"
                        break
                if not result or (
                    result["execution_status"] != "succeeded" and evidence_count == 0
                ):
                    break
                if plan_revision < int(budget["max_rounds"]):
                    await self._set_status(investigation_id, "planning")
                    completed_in_run = plan_revision - restored.next_plan_revision + 1
                    if completed_in_run >= self.CONTINUE_AFTER_ROUNDS and workflow.patched(
                        "bounded-history-continue-as-new-v1"
                    ):
                        workflow.continue_as_new(
                            WorkflowRunInput(
                                investigation_id=investigation_id,
                                next_plan_revision=plan_revision + 1,
                                deadline_epoch=deadline.timestamp(),
                                input_revision=self.state.input_revision,
                                evidence_revision=self.state.evidence_revision,
                                processed_signal_ids=sorted(self.state.processed_signal_ids),
                                continued=True,
                            )
                        )

            if self.state.canceled:
                return await self._cancel(investigation_id)
            await self._set_status(investigation_id, "reporting")
            await self._activity(
                "publish_inconclusive_report",
                [investigation_id, stop_reason],
                timeout=timedelta(minutes=2),
                attempts=5,
            )
            return "completed"
