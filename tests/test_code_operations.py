"""Offline persistent runner protocol and bounded control; never executes code."""
import asyncio
import json
import time
from dataclasses import replace
from datetime import datetime, timezone
from datetime import timedelta
from types import SimpleNamespace

import pytest

from game_test_agent.analysis.code_execution import CodeExecutionService
from game_test_agent.analysis.sandbox import AnalysisSandboxService, SandboxPolicy
from game_test_agent.contracts.models import (
    AnalysisJobResult, AnalysisOutputItem, CodeExecutionRequest, InvestigationRequest, IngestOutcome,
    SandboxEnvironment, SandboxOperationRef,
)
from game_test_agent.runtime.execution import CapacityExceeded, ExecutionLimits
from game_test_agent.storage import ConflictError
from game_test_agent.storage import canonical_json
from game_test_agent.analysis.windows_runner import WindowsSandboxRunner
from game_test_agent.workflows.local import LocalWorkflowBackend
from game_test_agent.workflows.native_context import NativeMemory
from game_test_agent.workflows.worker import CoreActivities
from native_plan_helpers import initial_plan_call
from test_native_enhancements import collect
from test_native_recall import add_input
from test_native_reliability import call, response, setup


class Runner:
    """Externally persistent state, shared by successive service instances."""
    runner_id = "offline-persistent"
    runtime_digest = "sha256:" + "a" * 64
    isolation_kind = "windows_psec"
    environment = SandboxEnvironment(runner_id=runner_id, isolation_kind=isolation_kind,
        runtime_digest=runtime_digest, dependency_lock_hash="b" * 64)

    def __init__(self, artifacts):
        self.artifacts = artifacts
        self.operations, self.specs, self.results = {}, {}, {}
        self.submitted, self.queries, self.cancel_requests, self.removed = [], [], [], []

    def authorize_inputs(self, spec, inputs):
        assert {item.evidence_id for item in inputs} == set(spec.input_evidence_ids)

    async def recover_operation(self, spec):
        return self.operations.get(spec.job_id)

    async def submit(self, spec):
        assert spec.job_id not in self.operations, "Accepted code must not be submitted twice"
        operation = SandboxOperationRef(job_id=spec.job_id, runner_id=self.runner_id,
            operation_id="operation-" + spec.job_id, fencing_token="fencing-" + spec.job_id,
            execution_status="running")
        self.operations[spec.job_id], self.specs[spec.job_id] = operation, spec
        self.submitted.append(spec.job_id)
        return operation

    async def query(self, operation):
        assert self.operations[operation.job_id] == operation
        self.queries.append(operation.job_id)
        return self.results.get(operation.job_id)

    async def request_cancel(self, operation):
        self.cancel_requests.append(operation.job_id)
        return True  # A request is deliberately not confirmation.

    async def cancel(self, operation):
        self.finish(operation.job_id, "canceled")
        return True

    async def remove(self, operation):
        assert operation.job_id in self.results, "Cannot remove an active operation"
        self.removed.append(operation.job_id)

    def finish(self, identity, status="succeeded"):
        spec = self.specs[identity]
        raw = b"47 ms from offline scripted fixture"
        stored = self.artifacts.put_bytes(spec.tenant_id, spec.project_id, raw)
        now = datetime.now(timezone.utc)
        self.results[identity] = AnalysisJobResult(job_id=identity, result_revision=1, execution_status=status,
            exit_code=0 if status == "succeeded" else 1, outputs=[AnalysisOutputItem(
                output_id="stdout", artifact_ref=stored.ref, sha256=stored.sha256,
                size_bytes=len(raw), kind="code_execution_output", mime_type="text/plain")],
            isolation_kind=self.isolation_kind, environment_runtime_digest=self.runtime_digest,
            dependency_lock_hash=self.environment.dependency_lock_hash,
            network_access=spec.environment.network_access,
            failure_kind=None if status == "succeeded" else "execution_canceled" if status == "canceled" else "program_error",
            started_at=now, finished_at=now)


def service_for(controller, runner=None, limits=None):
    runner = runner or Runner(controller.artifacts)
    sandbox = AnalysisSandboxService(controller.repository, controller.artifacts, SandboxPolicy(
        allowed_image_digests=set(), allowed_runtime_digests={runner.runtime_digest},
        allowed_runner_ids={runner.runner_id}, max_jobs_per_investigation=8))
    service = CodeExecutionService(sandbox, runner)
    service.execution_limits = limits or controller.execution_limits
    return service


def request(ids, code="print('scripted fixture')"):
    return CodeExecutionRequest(language="python", code=code, purpose="Inspect an offline fixture", input_evidence_ids=ids)


async def execute(service, view, decision, body):
    return await service.execute(view.investigation_id, decision, body,
                                 wait_for_permission=False, wait_for_completion=False)


def test_background_permits_survive_waiting_and_recovery_with_lower_limit():
    async def scenario():
        limits = ExecutionLimits(analyses=1)
        await limits.adopt_background("old-a", scope=("t", "a"))
        await limits.adopt_background("old-b", scope=("t", "b"))
        assert limits.snapshot()["analyses"]["active"] == 2
        assert not await limits.try_background("new")
        limits.release_background("old-a")
        assert limits.snapshot()["analyses"]["active"] == 1
        assert not await limits.try_background("new")
        limits.release_background("old-b")
        assert await limits.try_background("new")
        limits.release_background("new")
        assert limits.snapshot()["analyses"]["active"] == 0
    asyncio.run(scenario())


def test_foreground_analysis_release_transfers_permit_to_recovered_job():
    async def scenario():
        limits = ExecutionLimits(analyses=1)
        async with limits.slot("analyses"):
            await limits.adopt_background("recovered")
            assert limits.snapshot()["analyses"]["active"] == 2
        assert not await limits.try_background("new")
        limits.release_background("recovered")
        async with limits.slot("analyses"):
            assert limits.snapshot()["analyses"]["active"] == 1
        assert limits.snapshot()["analyses"]["active"] == 0
    asyncio.run(scenario())


def test_legacy_analysis_defers_without_blocking_a_persistent_job_poller():
    async def scenario():
        limits = ExecutionLimits(analyses=1)
        assert await limits.try_background("external-job")
        with pytest.raises(CapacityExceeded):
            async with limits.slot("analyses"):
                pytest.fail("A persistent job still owns this capacity")
        assert limits.snapshot()["analyses"]["waiting"] == 0
        limits.release_background("external-job")
    asyncio.run(scenario())


def test_submit_poll_collect_reuses_operation_and_charges_once(repository, settings):
    async def scenario():
        view, controller, _, _, ids = await collect(repository, settings, [{"v": 7}])
        service = service_for(controller)
        first = await asyncio.wait_for(execute(service, view, "job", request(ids)), timeout=1)
        assert first["execution_status"] == "running" and len(service.runner.queries) == 1
        usage = repository.budget_usage(view.investigation_id)["tool_reads"]
        for _ in range(3):
            result = await service.resume_investigation(view.investigation_id, wait_for_permission=False, wait_for_completion=False)
            assert result[0]["execution_status"] == "running"
        assert service.runner.submitted == [first["job_id"]]
        assert repository.budget_usage(view.investigation_id)["tool_reads"] == usage
        assert service.execution_limits.snapshot()["analyses"]["active"] == 1
        service.runner.finish(first["job_id"])
        finished = (await service.resume_investigation(view.investigation_id,
            wait_for_permission=False, wait_for_completion=False))[0]
        assert finished["execution_status"] == "succeeded" and "47 ms" in finished["stdout"]
        assert service.execution_limits.snapshot()["analyses"]["active"] == 0
        assert all(e.quality == "untrusted" for e in repository.list_current_evidence(view.investigation_id)
                   if e.analysis_job_id == first["job_id"])
        await execute(service, view, "job", request(ids))
        assert len(service.runner.submitted) == 1
    asyncio.run(scenario())


def test_queued_analysis_waits_for_real_capacity_and_then_starts(repository, settings):
    async def scenario():
        view, controller, _, _, ids = await collect(repository, settings, [{"v": 7}])
        service = service_for(controller, limits=ExecutionLimits(analyses=1))
        a = await execute(service, view, "a", request(ids, "print('a')"))
        b = await execute(service, view, "b", request(ids, "print('b')"))
        assert b["execution_status"] == "waiting_capacity" and len(service.runner.submitted) == 1
        service.runner.finish(a["job_id"])
        results = await service.resume_investigation(view.investigation_id, wait_for_permission=False, wait_for_completion=False)
        assert any(p["job_id"] == b["job_id"] and p["execution_status"] == "running" for p in results)
        assert len(service.runner.submitted) == 2
        assert service.execution_limits.snapshot()["analyses"]["active"] == 1
        service.runner.finish(b["job_id"])
        await service.resume_investigation(view.investigation_id, wait_for_permission=False, wait_for_completion=False)
        assert service.execution_limits.snapshot()["analyses"]["active"] == 0
    asyncio.run(scenario())


def test_restart_adopts_running_jobs_before_starting_another(repository, settings):
    async def scenario():
        view, controller, _, _, ids = await collect(repository, settings, [{"v": 7}])
        original = service_for(controller, limits=ExecutionLimits(analyses=1))
        a = await execute(original, view, "a", request(ids, "print('a')"))
        restarted = service_for(controller, original.runner, ExecutionLimits(analyses=1))
        b = await execute(restarted, view, "b", request(ids, "print('b')"))
        assert b["execution_status"] == "waiting_capacity"
        assert restarted.execution_limits.snapshot()["analyses"]["active"] == 1
        assert original.runner.submitted == [a["job_id"]]
        original.runner.finish(a["job_id"])
        await restarted.resume_investigation(view.investigation_id, wait_for_permission=False, wait_for_completion=False)
        assert len(original.runner.submitted) == 2
    asyncio.run(scenario())


def test_accepted_submission_is_recovered_after_process_dies_before_binding(repository, settings):
    class ProcessDied(BaseException):
        pass
    async def scenario():
        view, controller, _, _, ids = await collect(repository, settings, [{"v": 7}])
        service = service_for(controller)
        submit = service.runner.submit
        async def crash(spec):
            await submit(spec)
            raise ProcessDied()
        service.runner.submit = crash
        with pytest.raises(ProcessDied):
            await execute(service, view, "crash", request(ids))
        job = repository.list_analysis_jobs(view.investigation_id)[0]
        assert job.operation_ref is None  # The external operation exists independently.
        restarted = service_for(controller, service.runner, ExecutionLimits(analyses=1))
        result = await restarted.resume_investigation(view.investigation_id, wait_for_permission=False, wait_for_completion=False)
        assert result[0]["execution_status"] == "running"
        assert repository.get_analysis_job(job.spec.job_id).operation_ref
        assert len(service.runner.submitted) == 1
    asyncio.run(scenario())


def test_failed_submit_without_acceptance_is_not_retried_by_polling(repository, settings):
    async def scenario():
        view, controller, _, _, ids = await collect(repository, settings, [{"v": 7}])
        service = service_for(controller)
        async def reject(_spec):
            raise PermissionError("PRIVATE_RUNNER_CONFIGURATION")
        service.runner.submit = reject
        result = await execute(service, view, "rejected", request(ids))
        assert result["failure_kind"] == "analysis_submission_rejected"
        again = (await service.resume_investigation(view.investigation_id,
            wait_for_permission=False, wait_for_completion=False))[0]
        assert again == result and "PRIVATE_RUNNER" not in json.dumps(again)
        assert service.execution_limits.snapshot()["analyses"]["active"] == 0
    asyncio.run(scenario())


def test_cancellation_request_does_not_claim_completion_or_free_capacity(repository, settings):
    async def scenario():
        view, controller, _, _, ids = await collect(repository, settings, [{"v": 7}])
        service = service_for(controller)
        first = await execute(service, view, "cancel", request(ids))
        add_input(repository, view, "changed", "Stop the previous analysis and reassess.")
        result = (await service.resume_investigation(view.investigation_id,
            wait_for_permission=False, wait_for_completion=False))[0]
        assert result["execution_status"] == "running" and result["failure_kind"] == "cancellation_requested"
        assert service.execution_limits.snapshot()["analyses"]["active"] == 1
        assert not service.runner.removed
        service.runner.finish(first["job_id"], "canceled")
        done = (await service.resume_investigation(view.investigation_id,
            wait_for_permission=False, wait_for_completion=False))[0]
        assert done["execution_status"] == "canceled"
        assert service.execution_limits.snapshot()["analyses"]["active"] == 0
    asyncio.run(scenario())


@pytest.mark.parametrize("field,value", [("environment_runtime_digest", "sha256:" + "c" * 64),
                                         ("network_access", "approved")])
def test_collect_rejects_invalid_environment_and_retains_running_capacity(repository, settings, field, value):
    async def scenario():
        view, controller, _, _, ids = await collect(repository, settings, [{"v": 7}])
        service = service_for(controller)
        first = await execute(service, view, "bad-result", request(ids))
        service.runner.finish(first["job_id"])
        valid = service.runner.results[first["job_id"]]
        service.runner.results[first["job_id"]] = valid.model_copy(update={field: value})
        with pytest.raises(ConflictError):
            await service.resume_investigation(view.investigation_id, wait_for_permission=False, wait_for_completion=False)
        assert service.execution_limits.snapshot()["analyses"]["active"] == 1
        assert not service.runner.removed
        controller.code_execution = service
        async def unconfirmed(_operation):
            return False
        service.runner.cancel = unconfirmed
        owner = CoreActivities(repository, replace(settings, mcp_native_tools=True))
        owner.native, owner.code_execution = controller, service
        failed = await owner.native_investigation_step(view.investigation_id, True)
        assert failed["done"] and failed["status"] == "failed"
        assert failed["worker_step"]["continuation"]["pending_cleanup_resource_count"] == 1
    asyncio.run(scenario())


def test_native_worker_polls_without_schema_discovery_or_model_turns(repository, settings):
    async def scenario():
        view, controller, provider = setup(repository, settings)
        controller.code_execution = service_for(controller)
        turns = 0
        async def model(messages, tools):
            nonlocal turns
            turns += 1
            if turns == 1:
                return response(initial_plan_call(), call())
            if turns == 2:
                eid = repository.list_current_evidence(view.investigation_id)[0].evidence_id
                return response(call("agent_execute_code", request([eid]).model_dump(mode="json"), "bounded-code"))
            assert "succeeded" in messages[2]["content"]
            note = next(n for n in json.loads(messages[2]["content"])["collected_evidence_search_hits"]
                        if "47 ms" in n.get("original_excerpt", ""))
            return response(content=json.dumps({"summary": "Limited original observation.",
                "citations": [{"evidence_id": note["evidence_id"], "quote": "47 ms"}]}))
        controller._model_call = model
        await controller.run_step(view.investigation_id, principal_id="tester", scopes={"runs:read", "evidence:read"})
        waiting = await controller.run_step(view.investigation_id, principal_id="tester", scopes={"runs:read", "evidence:read"})
        assert waiting.wait_reason == "analysis" and waiting.continuation.pending_analysis_count == 1
        discoveries = provider.native_tools.await_count
        usage = repository.budget_usage(view.investigation_id)
        for _ in range(3):
            result = await controller.run_step(view.investigation_id, principal_id="tester", scopes={"runs:read", "evidence:read"})
            assert result.state == "waiting" and result.wait_reason == "analysis"
        assert turns == 2 and provider.native_tools.await_count == discoveries
        assert repository.budget_usage(view.investigation_id) == usage
        assert controller.execution_limits.snapshot()["analyses"]["active"] == 1
        assert all(controller.execution_limits.snapshot()[key]["active"] == 0 for key in ("investigations", "models", "tools"))
        restored = NativeMemory.restore(repository, controller.artifacts, view)
        assert "running" in restored.messages[2]["content"]
        controller.code_execution.runner.finish(waiting.continuation.pending_analysis_ids[0])
        done = await controller.run_step(view.investigation_id, principal_id="tester", scopes={"runs:read", "evidence:read"})
        assert done.state == "completed" and turns == 3
        assert len(controller.code_execution.runner.submitted) == 1
    asyncio.run(scenario())


def test_local_other_investigation_completes_while_code_runs(repository, settings):
    async def scenario():
        _, controller, provider = setup(repository, settings)
        limits = ExecutionLimits(investigations=1, analyses=1)
        controller.execution_limits = limits
        controller.code_execution = service_for(controller, limits=limits)
        views = {key: repository.create_investigation(InvestigationRequest(tenant_id="studio-a", project_id="game-a",
            objective={"objective_id": key, "description": "Bounded local scheduling"}, budget={"max_rounds": 6}),
            key, principal_id="tester", scopes={"runs:read", "evidence:read"})[0] for key in ("code-task", "other-task")}
        turns = {key: 0 for key in views}
        async def model(messages, tools):
            key = json.loads(messages[1]["content"])["request"]["objective"]["objective_id"]
            turns[key] += 1
            if turns[key] == 1:
                return response(initial_plan_call(), call())
            if key == "code-task" and turns[key] == 2:
                eid = repository.list_current_evidence(views[key].investigation_id)[0].evidence_id
                return response(call("agent_execute_code", request([eid]).model_dump(mode="json"), "background"))
            note = next(n for n in json.loads(messages[2]["content"])["collected_evidence_search_hits"]
                        if "47 ms" in n.get("original_excerpt", ""))
            return response(content=json.dumps({"summary": "Original observation.",
                "citations": [{"evidence_id": note["evidence_id"], "quote": "47 ms"}]}))
        controller._model_call = model
        backend = LocalWorkflowBackend(repository, None, SimpleNamespace(model_name="disabled"),
            controller.reports, controller=controller, execution_limits=limits)
        async def wait_for(predicate):
            while not predicate():
                await asyncio.sleep(.005)
        try:
            for view in views.values():
                backend.start(view.investigation_id)
            await asyncio.wait_for(wait_for(lambda: repository.get_investigation(views["other-task"].investigation_id).status == "completed"), 5)
            assert turns["code-task"] == 2 and limits.snapshot()["analyses"]["active"] == 1
            assert views["code-task"].investigation_id in backend._pending
            controller.code_execution.runner.finish(controller.code_execution.runner.submitted[0])
            await asyncio.wait_for(wait_for(lambda: repository.get_investigation(views["code-task"].investigation_id).status == "completed"), 5)
            assert turns == {"code-task": 3, "other-task": 2}
            assert provider.call_native.await_count == 2
        finally:
            await backend.close()
    asyncio.run(scenario())


def test_busy_recipe_yields_instead_of_blocking_the_only_investigation_slot(repository, settings):
    async def scenario():
        view, controller, _ = setup(repository, settings)
        controller.execution_limits = ExecutionLimits(analyses=1)
        turns = 0
        async def model(messages, tools):
            nonlocal turns
            turns += 1
            if turns == 1:
                return response(initial_plan_call(), call())
            if turns == 2:
                eid = repository.list_current_evidence(view.investigation_id)[0].evidence_id
                return response(call("agent_analyze_evidence", {"recipe": "crash", "evidence_id": eid}, "recipe"))
            note = next(n for n in json.loads(messages[2]["content"])["collected_evidence_search_hits"]
                        if "47 ms" in n.get("original_excerpt", ""))
            return response(content=json.dumps({"summary": "Limited observation.",
                "citations": [{"evidence_id": note["evidence_id"], "quote": "47 ms"}]}))
        controller._model_call = model
        scopes = {"runs:read", "evidence:read"}
        await controller.run_step(view.investigation_id, principal_id="tester", scopes=scopes)
        await controller.execution_limits.try_background("other-job")
        busy = await asyncio.wait_for(controller.run_step(view.investigation_id, principal_id="tester", scopes=scopes), 1)
        assert busy.wait_reason == "analysis_capacity" and turns == 2
        await controller.run_step(view.investigation_id, principal_id="tester", scopes=scopes)
        assert turns == 2
        controller.execution_limits.release_background("other-job")
        done = await controller.run_step(view.investigation_id, principal_id="tester", scopes=scopes)
        assert done.done and turns == 3
        assert repository.budget_usage(view.investigation_id)["tool_reads"] == 1
    asyncio.run(scenario())


def test_rejected_ingestion_never_announces_successful_evidence(repository, settings, monkeypatch):
    async def scenario():
        view, controller, provider = setup(repository, settings)
        monkeypatch.setattr("game_test_agent.workflows.native_mcp.EvidenceService.ingest",
            lambda *_args: IngestOutcome(status="rejected", reason="PRIVATE_LEDGER_DETAIL"))
        from test_native_recall import memory
        state = memory()
        tool = (await provider.native_tools())[0]
        payload, _ = await controller._execute_call(view, call(), {"inspect_incident": (provider, tool)}, state, time.time() + 5)
        assert payload["error_code"] == "EVIDENCE_INGESTION_REJECTED"
        assert "PRIVATE_LEDGER_DETAIL" not in json.dumps(payload) and "evidence_id" not in payload
        assert not state.evidence_index and not repository.list_current_evidence(view.investigation_id)
        assert not repository.list_recent_events_by_type(view.investigation_id, "mcp.native.tool_completed")
    asyncio.run(scenario())


@pytest.mark.parametrize("lease", ["absent", "alive", "dead", "partial"])
def test_windows_runner_recovers_uncertain_acceptance_without_launching_process(repository, settings, tmp_path, monkeypatch, lease):
    async def scenario():
        view, controller, _, _, ids = await collect(repository, settings, [{"v": 7}])
        service = service_for(controller)
        started = await execute(service, view, "runner-protocol", request(ids))
        spec = repository.get_analysis_job(started["job_id"]).spec
        runner = WindowsSandboxRunner.__new__(WindowsSandboxRunner)
        runner.root, runner.runner_id, runner.artifacts = tmp_path / "runner", service.runner.runner_id, controller.artifacts
        directory = runner._directory(spec.job_id)
        directory.mkdir(parents=True)
        (directory / "spec.json").write_text(canonical_json(spec), encoding="utf-8")
        (directory / "request.json").write_text("{}", encoding="utf-8")
        operation = await runner.recover_operation(spec)
        assert operation is not None and not (directory / "accepted.json").exists()
        if lease != "absent":
            raw = "{" if lease == "partial" else json.dumps({"pid": 123,
                "creation_token": "original", "started_at": datetime.now(timezone.utc).isoformat()})
            (directory / "acceptance.lock").write_text(raw, encoding="utf-8")
        monkeypatch.setattr("game_test_agent.analysis.windows_runner.process_identity",
                            lambda _pid: "original" if lease == "alive" else "different")
        killed = []
        monkeypatch.setattr("game_test_agent.analysis.windows_runner.terminate_job", killed.append)
        assert await runner.request_cancel(operation)  # Writes a request, not a confirmation.
        result = await runner.query(operation)
        if lease in {"alive", "partial"}:
            assert result is None and not killed
        else:
            assert result.execution_status == ("canceled" if lease == "absent" else "failed")
            assert result.failure_kind == ("execution_canceled" if lease == "absent" else "native_supervisor_lost")
            with pytest.raises(FileExistsError):
                (directory / "acceptance.lock").open("x").close()
            # A terminal saved result is replayed by submit, with no process start.
            async def forbidden():
                pytest.fail("Recovered terminal code must not launch a process")
            runner.verify = forbidden
            assert await runner.submit(spec) == operation
    asyncio.run(scenario())


def test_expired_worker_finishes_without_claiming_an_unknown_job_stopped(repository, settings):
    async def scenario():
        view, controller, _, _, ids = await collect(repository, settings, [{"v": 7}])
        controller.code_execution = service = service_for(controller)
        await execute(service, view, "unknown-at-deadline", request(ids))
        expired = datetime.now(timezone.utc) - timedelta(seconds=view.request.budget.max_wall_seconds + 1)
        repository._connection.execute("UPDATE investigations SET created_at=? WHERE investigation_id=?",
                                       (expired.isoformat(), view.investigation_id))
        repository._connection.commit()
        done = await controller.run_step(view.investigation_id, principal_id="tester", scopes={"runs:read", "evidence:read"})
        assert done.done and done.state == "failed" and done.continuation.pending_cleanup_resource_count == 1
        assert repository.list_analysis_jobs(view.investigation_id)[0].state == "running"
        assert service.execution_limits.snapshot()["analyses"]["active"] == 1
        report = repository.get_latest_report(view.investigation_id)[0]
        assert report.stop_reason == "wall_clock_budget_exhausted"
        assert any("未确认清理" in gap for gap in report.unresolved_questions)
        # A lost terminal acknowledgement must still expose cleanup obligations
        # when Temporal retries the activity after the investigation is failed.
        owner = CoreActivities(repository, replace(settings, mcp_native_tools=True))
        owner.native = controller
        replay = await owner.native_investigation_step(view.investigation_id, True)
        assert replay["done"] and replay["worker_step"]["continuation"]["pending_cleanup_resource_count"] == 1
    asyncio.run(scenario())


def test_temporal_starts_cleanup_for_terminal_worker_with_pending_resources(monkeypatch):
    from game_test_agent.workflows import temporal
    async def scenario():
        instance = temporal.InvestigationWorkflow()
        def start_activity(*args, **kwargs):
            async def step():
                return {"done": True, "status": "failed", "worker_step": {
                    "continuation": {"pending_cleanup_resource_count": 1}}}
            return asyncio.create_task(step())
        async def wait_condition(*args, **kwargs):
            await asyncio.Future()
        children = []
        async def start_child(*args, **kwargs):
            children.append((args, kwargs))
        monkeypatch.setattr(temporal.workflow, "start_activity", start_activity)
        monkeypatch.setattr(temporal.workflow, "wait_condition", wait_condition)
        monkeypatch.setattr(temporal.workflow, "start_child_workflow", start_child)
        monkeypatch.setattr(temporal.workflow, "patched", lambda _name: True)
        monkeypatch.setattr(temporal.workflow, "wait", asyncio.wait, raising=False)
        assert await instance._run_native("inv", {"budget": {"max_rounds": 1, "max_wall_seconds": 60}},
                                          defer_capacity=True, worker_steps=True) == "failed"
        assert len(children) == 1 and children[0][1]["id"] == "cleanup/inv"
    asyncio.run(scenario())


def test_persisted_cancellation_keeps_unconfirmed_cleanup_in_modern_activity_result(repository, settings):
    async def scenario():
        view, controller, _, _, ids = await collect(repository, settings, [{"v": 7}])
        controller.code_execution = service = service_for(controller)
        await execute(service, view, "pending-cancel", request(ids))
        async def unconfirmed(_operation):
            return False
        service.runner.cancel = unconfirmed
        owner = CoreActivities(repository, replace(settings, mcp_native_tools=True))
        owner.native, owner.code_execution = controller, service
        repository.transition(view.investigation_id, "canceling")
        result = await owner.native_investigation_step(view.investigation_id, True)
        assert result["done"] and result["status"] == "canceled"
        assert result["worker_step"]["continuation"]["pending_cleanup_resource_count"] == 1
        assert repository.pending_cleanup_resources(view.investigation_id)
    asyncio.run(scenario())
