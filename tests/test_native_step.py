"""Offline worker turn parity, durable waiting and queue fairness."""
import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from game_test_agent.analysis.code_execution import CodeExecutionService
from game_test_agent.api.auth import Principal
from game_test_agent.contracts.models import CodeExecutionRequest, InvestigationRequest, SandboxEnvironment
from game_test_agent.reports import ReportService
from game_test_agent.runtime.execution import ExecutionLimits
from game_test_agent.workflows.local import LocalWorkflowBackend
from game_test_agent.workflows.native_context import NativeMemory
from game_test_agent.workflows.native_step import observe_step
from game_test_agent.workflows.worker import CoreActivities
from native_plan_helpers import initial_plan_call
from test_native_enhancements import collect
from test_native_recall import add_input
from test_native_reliability import call, final, response, setup


SCOPES = {"runs:read", "evidence:read"}


def request_view(repository, key):
    return repository.create_investigation(InvestigationRequest(tenant_id="studio-a", project_id="game-a",
        objective={"objective_id": "diagnose", "description": "Worker boundary regression"},
        budget={"max_rounds": 6}), key, principal_id="tester", scopes=SCOPES)[0]


def code_service(controller):
    policy = SimpleNamespace(max_program_bytes=32768, max_jobs_per_investigation=4,
        max_cpu_seconds=30, max_memory_mb=512, max_process_count=16,
        max_disk_mb=64, max_wall_seconds=60, max_output_bytes=262144)
    sandbox = SimpleNamespace(repository=controller.repository, artifacts=controller.artifacts,
        policy=policy, prepare=Mock(side_effect=RuntimeError("approved job preparation reached")))
    runner = SimpleNamespace(runtime_digest="sha256:" + "a" * 64,
        runner_id="offline-runner", isolation_kind="windows_psec", normalize_permissions=lambda p: p,
        submit=AsyncMock(side_effect=AssertionError("No real code execution in offline tests")),
        environment=SandboxEnvironment(runner_id="offline-runner", isolation_kind="windows_psec",
            runtime_digest="sha256:" + "a" * 64, dependency_lock_hash="b" * 64))
    return CodeExecutionService(sandbox, runner)


def code_request(ids):
    return CodeExecutionRequest(language="python", code="print('offline fixture')",
        purpose="Inspect admitted fixture", input_evidence_ids=ids,
        permissions={"network": True, "reason": "Read an explicitly approved fixture"})


def decide(service, proposal, decision):
    return service.permissions.decide(proposal["approval_id"], proposal["request_hash"], decision,
        Principal("reviewer", "studio-a", "game-a", frozenset({"agent:analysis:approve"})))


async def until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(.001)
    await asyncio.wait_for(wait(), timeout=5)


def test_single_step_replays_lost_ack_and_resumes_with_new_controller(repository, settings):
    async def scenario():
        view, controller, provider = setup(repository, settings)
        turns = 0
        async def model(messages, tools):
            nonlocal turns
            turns += 1
            return response(initial_plan_call(), call()) if turns == 1 else final(messages)
        controller._model_call = model
        first = await controller.run_step(view.investigation_id, principal_id="tester", scopes=SCOPES, step_id="run-1:activity-1")
        assert first.state == "continue" and first.continuation.next_turn == 2
        assert first.continuation.checkpoint_ref and first.continuation.plan_revision == 1
        saved = NativeMemory.restore(repository, controller.artifacts, view)
        pending = set()
        for message in saved.messages:
            if message["role"] == "assistant":
                assert not pending
                pending = {item["id"] for item in message.get("tool_calls", [])}
            elif message["role"] == "tool":
                pending.remove(message["tool_call_id"])
        assert not pending
        restarted = type(controller)(controller.settings, repository, controller.providers,
                                     controller.artifacts, controller.reports)
        restarted._model_call = model
        replay = await restarted.run_step(view.investigation_id, principal_id="tester", scopes=SCOPES, step_id="run-1:activity-1")
        assert replay == first and turns == 1
        second = await restarted.run_step(view.investigation_id, principal_id="tester", scopes=SCOPES, step_id="run-1:activity-2")
        assert second.done and second.state == "completed" and turns == 2
        provider.call_native.assert_awaited_once()
        assert repository.budget_usage(view.investigation_id)["tool_reads"] == 1
    asyncio.run(scenario())


def test_step_packet_contains_references_and_validates_scope_before_discovery(repository, settings):
    async def scenario():
        view, controller, provider = setup(repository, settings)
        add_input(repository, view, "private", "PRIVATE_LONG_USER_REQUIREMENT" * 100)
        with pytest.raises(PermissionError):
            await controller.run_step(view.investigation_id, principal_id="unprivileged", scopes=set())
        provider.native_tools.assert_not_awaited()
        packet = observe_step(controller, view.investigation_id).model_dump_json()
        assert "PRIVATE_LONG_USER_REQUIREMENT" not in packet and "messages" not in packet and len(packet) < 2000
        assert repository.budget_usage(view.investigation_id).get("native_model_turns", 0) == 0
    asyncio.run(scenario())


def test_concurrent_step_owner_yields_without_second_model_call(repository, settings):
    async def scenario():
        view, controller, _ = setup(repository, settings)
        started, release = asyncio.Event(), asyncio.Event()
        async def model(*args):
            started.set()
            await release.wait()
            return response(initial_plan_call())
        controller._model_call = model
        running = asyncio.create_task(controller.run_step(view.investigation_id, principal_id="tester", scopes=SCOPES))
        await started.wait()
        yielded = await controller.run_step(view.investigation_id, principal_id="tester", scopes=SCOPES)
        assert yielded.state == "waiting" and yielded.wait_reason == "owner"
        release.set()
        await running
        assert repository.budget_usage(view.investigation_id)["native_model_turns"] == 1
        assert not controller._step_locks
    asyncio.run(scenario())


@pytest.mark.parametrize("waiting", [False, True])
def test_local_queue_yields_between_steps_and_during_wait(repository, settings, waiting):
    async def scenario():
        _, controller, _ = setup(repository, settings)
        a, b = request_view(repository, "fair-a"), request_view(repository, "fair-b")
        visits, counts = [], {}
        limits = ExecutionLimits(investigations=1)
        async def step(identity, **kwargs):
            assert limits.snapshot()["investigations"]["active"] == 1
            visits.append(identity)
            counts[identity] = counts.get(identity, 0) + 1
            if counts[identity] == 2:
                repository.transition(identity, "failed")
            result = observe_step(controller, identity)
            if waiting and identity == a.investigation_id and counts[identity] == 1:
                result = result.model_copy(update={"state": "waiting", "wait_reason": "permission", "retry_after_seconds": .05})
            return result
        controller.run_step = step
        backend = LocalWorkflowBackend(repository, None, SimpleNamespace(model_name="disabled"),
            ReportService(repository), controller=controller, execution_limits=limits)
        try:
            backend.start(a.investigation_id)
            backend.start(b.investigation_id)
            await until(lambda: all(counts.get(i, 0) == 2 for i in (a.investigation_id, b.investigation_id)))
            assert visits[:2] == [a.investigation_id, b.investigation_id]
            assert limits.snapshot()["investigations"]["active"] == 0
        finally:
            await backend.close()
    asyncio.run(scenario())


def test_nonblocking_code_permission_wait_and_denial_keep_budget_idempotent(repository, settings):
    async def scenario():
        view, controller, _, _, ids = await collect(repository, settings, [{"value": 7}])
        service = code_service(controller)
        request = code_request(ids)
        first = await service.execute(view.investigation_id, "permission-read", request, wait_for_permission=False)
        assert first["execution_status"] == "waiting_permission"
        usage = repository.budget_usage(view.investigation_id)["tool_reads"]
        repeated = await service.execute(view.investigation_id, "permission-read", request, wait_for_permission=False)
        assert repeated == first and repository.budget_usage(view.investigation_id)["tool_reads"] == usage
        proposal = service.permissions.get(first["approval_id"])
        decide(service, proposal, "deny")
        resumed = await service.resume_investigation(view.investigation_id, wait_for_permission=False)
        assert resumed[0]["execution_status"] == "not_executed"
        assert resumed[0]["failure_kind"] == "permission_denied"
        service.runner.submit.assert_not_awaited()
        assert not repository.list_analysis_jobs(view.investigation_id)
    asyncio.run(scenario())


def test_exact_approved_proposal_reaches_existing_job_admission(repository, settings):
    async def scenario():
        view, controller, _, _, ids = await collect(repository, settings, [{"value": 7}])
        service = code_service(controller)
        request = code_request(ids)
        pending = await service.execute(view.investigation_id, "approved-code", request, wait_for_permission=False)
        proposal = service.permissions.get(pending["approval_id"])
        decide(service, proposal, "approve")
        with pytest.raises(RuntimeError, match="job preparation"):
            await service.execute(view.investigation_id, "approved-code", request, wait_for_permission=False)
        spec = service.sandbox.prepare.call_args.args[0]
        assert spec.permission_approval_id == proposal["approval_id"]
        assert spec.permissions.network and spec.created_by == "agent:approved-code"
        service.runner.submit.assert_not_awaited()  # Stop before real execution.
    asyncio.run(scenario())


@pytest.mark.parametrize("decision", [None, "approve"])
def test_new_input_supersedes_pending_or_approved_code_without_execution(repository, settings, decision):
    async def scenario():
        view, controller, _, _, ids = await collect(repository, settings, [{"value": 7}])
        service = code_service(controller)
        request = code_request(ids)
        pending = await service.execute(view.investigation_id, "old-direction", request, wait_for_permission=False)
        if decision:
            decide(service, service.permissions.get(pending["approval_id"]), decision)
        add_input(repository, view, "new-direction", "Reassess the investigation before running code.")
        resumed = await service.resume_investigation(view.investigation_id, wait_for_permission=False)
        assert resumed[0]["failure_kind"] == "permission_superseded_input"
        service.sandbox.prepare.assert_not_called()
        service.runner.submit.assert_not_awaited()
    asyncio.run(scenario())


def test_actual_native_tool_wait_closes_group_and_resumes_after_denial(repository, settings):
    async def scenario():
        view, controller, provider = setup(repository, settings)
        controller.code_execution = service = code_service(controller)
        turns = 0
        async def model(messages, tools):
            nonlocal turns
            turns += 1
            if turns == 1:
                return response(initial_plan_call(), call())
            if turns == 2:
                eid = repository.list_current_evidence(view.investigation_id)[0].evidence_id
                return response(call("agent_execute_code", code_request([eid]).model_dump(mode="json"), "permission-code"))
            assert "permission_denied" in messages[2]["content"]
            note = next(n for n in json.loads(messages[2]["content"])["collected_evidence_search_hits"]
                        if "47 ms" in n.get("original_excerpt", ""))
            return response(content=json.dumps({"summary": "Limited observation; analysis not executed.",
                "citations": [{"evidence_id": note["evidence_id"], "quote": "47 ms"}],
                "limitations": ["Additional analysis was denied."]}))
        controller._model_call = model
        first = await controller.run_step(view.investigation_id, principal_id="tester", scopes=SCOPES)
        assert first.state == "continue"
        waiting = await controller.run_step(view.investigation_id, principal_id="tester", scopes=SCOPES)
        assert waiting.state == "waiting" and waiting.wait_reason == "permission" and turns == 2
        usage = repository.budget_usage(view.investigation_id)
        for _ in range(3):
            result = await controller.run_step(view.investigation_id, principal_id="tester", scopes=SCOPES)
            assert result.wait_reason == "permission"
        assert turns == 2 and repository.budget_usage(view.investigation_id) == usage
        assert all(item["active"] == 0 for item in controller.execution_limits.snapshot().values())
        restored = NativeMemory.restore(repository, controller.artifacts, view)
        assert json.loads(restored.messages[-1]["content"])["execution_status"] == "waiting_permission"
        decide(service, service.permissions.list(view.investigation_id)[0], "deny")
        finished = await controller.run_step(view.investigation_id, principal_id="tester", scopes=SCOPES)
        assert finished.state == "completed" and turns == 3
        provider.call_native.assert_awaited_once()
        service.runner.submit.assert_not_awaited()
    asyncio.run(scenario())


def test_waiting_permission_survives_local_shutdown_without_consuming_turns(repository, settings):
    async def scenario():
        _, controller, _ = setup(repository, settings)
        view = request_view(repository, "pending-restart")
        controller.code_execution = service = code_service(controller)
        proposal = service.permissions.propose(view.investigation_id, "pending", "unstarted-job",
            code_request(["future-input"]), service.runner.runtime_digest, {})
        controller._model_call = AsyncMock(side_effect=AssertionError("Waiting must not call the model"))
        limits = ExecutionLimits(investigations=1)
        backend = LocalWorkflowBackend(repository, None, SimpleNamespace(model_name="disabled"),
            ReportService(repository), controller=controller, execution_limits=limits)
        backend.start(view.investigation_id)
        await until(lambda: view.investigation_id in backend._pending)
        assert limits.snapshot()["investigations"]["active"] == 0
        await backend.close()
        assert service.permissions.get(proposal["approval_id"])["status"] == "pending"
        assert repository.budget_usage(view.investigation_id).get("native_model_turns", 0) == 0
    asyncio.run(scenario())


def test_temporal_waiting_steps_do_not_spend_round_bound(monkeypatch):
    from game_test_agent.workflows import temporal
    async def scenario():
        instance = temporal.InvestigationWorkflow()
        attempts = 0
        def start_activity(name, **kwargs):
            nonlocal attempts
            attempts += 1
            assert kwargs["args"] == ["inv", True]
            async def step():
                return {"done": attempts == 6, "status": "completed" if attempts == 6 else "planning",
                    "worker_step": {"state": "completed" if attempts == 6 else "waiting", "retry_after_seconds": .25}}
            return asyncio.create_task(step())
        async def wait_condition(predicate, **kwargs):
            if "timeout" in kwargs:
                raise TimeoutError()
            await asyncio.Future()
        monkeypatch.setattr(temporal.workflow, "start_activity", start_activity)
        monkeypatch.setattr(temporal.workflow, "wait_condition", wait_condition)
        monkeypatch.setattr(temporal.workflow, "wait", asyncio.wait, raising=False)
        assert await instance._run_native("inv", {"budget": {"max_rounds": 1, "max_wall_seconds": 60}},
                                           defer_capacity=True, worker_steps=True) == "completed"
        assert attempts == 6
    asyncio.run(scenario())


def test_temporal_activity_step_id_includes_workflow_run_identity(repository, settings, monkeypatch):
    from game_test_agent.workflows import worker
    async def scenario():
        view, controller, _ = setup(repository, settings)
        owner = CoreActivities(repository, replace(settings, mcp_native_tools=True))
        owner.native = controller
        result = observe_step(controller, view.investigation_id)
        owner.native.run_step = AsyncMock(return_value=result)
        monkeypatch.setattr(worker.activity, "in_activity", lambda: True)
        for run_id in ("first-run", "continued-run"):
            monkeypatch.setattr(worker.activity, "info", lambda: SimpleNamespace(workflow_run_id=run_id, activity_id="1"))
            await owner.native_investigation_step(view.investigation_id, True)
            assert owner.native.run_step.call_args.kwargs["step_id"] == run_id + ":1"
    asyncio.run(scenario())


def test_step_discards_stale_response_and_keeps_consumed_round(repository, settings):
    async def scenario():
        view, controller, provider = setup(repository, settings)
        turns = 0
        async def model(messages, tools):
            nonlocal turns
            turns += 1
            if turns == 1:
                add_input(repository, view, "correction", "Use the latest scope before any platform read.")
            return response(initial_plan_call(), call()) if turns <= 2 else final(messages)
        controller._model_call = model
        stale = await controller.run_step(view.investigation_id, principal_id="tester", scopes=SCOPES)
        assert stale.state == "continue" and stale.continuation.next_turn == 2
        provider.call_native.assert_not_awaited()
        await controller.run_step(view.investigation_id, principal_id="tester", scopes=SCOPES)
        done = await controller.run_step(view.investigation_id, principal_id="tester", scopes=SCOPES)
        assert done.done and turns == 3 and done.continuation.next_turn == 4
        provider.call_native.assert_awaited_once()
    asyncio.run(scenario())


def test_canceling_a_yielded_local_worker_cancels_pending_permission(repository, settings):
    async def scenario():
        _, controller, _ = setup(repository, settings)
        view = request_view(repository, "cancel-pending")
        controller.code_execution = service = code_service(controller)
        proposal = service.permissions.propose(view.investigation_id, "pending", "unstarted-job",
            code_request(["future-input"]), service.runner.runtime_digest, {})
        controller._model_call = AsyncMock(side_effect=AssertionError("No model call while waiting"))
        backend = LocalWorkflowBackend(repository, None, SimpleNamespace(model_name="disabled"),
            ReportService(repository), controller=controller, execution_limits=ExecutionLimits(investigations=1))
        try:
            backend.start(view.investigation_id)
            await until(lambda: view.investigation_id in backend._pending)
            assert backend.request_cancel(view.investigation_id)
            await backend._tasks[view.investigation_id]
            assert repository.get_investigation(view.investigation_id).status == "canceled"
            assert service.permissions.get(proposal["approval_id"])["status"] == "canceled"
            assert view.investigation_id not in backend._pending
        finally:
            await backend.close()
    asyncio.run(scenario())


def test_terminal_step_returns_without_discovery_or_model(repository, settings):
    async def scenario():
        view, controller, provider = setup(repository, settings)
        repository.transition(view.investigation_id, "failed")
        controller.run = AsyncMock(side_effect=AssertionError("Terminal investigations cannot execute"))
        result = await controller.run_step(view.investigation_id, principal_id="tester", scopes=SCOPES)
        assert result.state == "failed" and result.done
        provider.native_tools.assert_not_awaited()
        controller.run.assert_not_awaited()
    asyncio.run(scenario())


def test_restart_after_checkpoint_before_step_ack_does_not_advance_same_request(repository, settings):
    async def scenario():
        view, controller, provider = setup(repository, settings)
        controller._model_call = AsyncMock(return_value=response(initial_plan_call(), call()))
        original = controller.run
        async def interrupted(*args, **kwargs):
            await original(*args, **kwargs)
            raise asyncio.CancelledError()
        controller.run = interrupted
        with pytest.raises(asyncio.CancelledError):
            await controller.run_step(view.investigation_id, principal_id="tester", scopes=SCOPES, step_id="lost-ack")
        restarted = type(controller)(controller.settings, repository, controller.providers,
                                     controller.artifacts, controller.reports)
        restarted._model_call = AsyncMock(side_effect=AssertionError("Same step must not advance again"))
        recovered = await restarted.run_step(view.investigation_id, principal_id="tester", scopes=SCOPES, step_id="lost-ack")
        assert recovered.state == "continue" and recovered.continuation.next_turn == 2
        restarted._model_call.assert_not_awaited()
        provider.call_native.assert_awaited_once()
    asyncio.run(scenario())
