"""Regressions for JSON diagnostics, read amplification, recovery and backpressure."""
import asyncio
import hashlib
import json
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from game_test_agent.analysis.diagnostics import NativeDiagnosticService
from game_test_agent.contracts.models import EvidenceRecord, InvestigationRequest
from game_test_agent.evidence.service import EvidenceService
from game_test_agent.reports import ReportService
from game_test_agent.runtime.execution import ExecutionLimits
from game_test_agent.workflows.local import LocalWorkflowBackend
from game_test_agent.workflows.native_context import NativeMemory
from game_test_agent.workflows.native_evidence import EvidenceSearch, search_evidence_async
from game_test_agent.workflows.worker import CoreActivities
from test_native_reliability import call, setup


def memory():
    return NativeMemory(messages=[{"role": "system", "content": ""},
        {"role": "user", "content": ""}, {"role": "user", "content": "{}"}])


def seed(repository, controller, view, value, mime="application/json", ident="input"):
    raw = value.encode() if isinstance(value, str) else json.dumps(value, ensure_ascii=False).encode()
    stored = controller.artifacts.put_bytes("studio-a", "game-a", raw)
    record = EvidenceRecord(evidence_id=ident, tenant_id="studio-a", project_id="game-a",
        artifact_ref=stored.ref, kind="review", mime_type=mime, sha256=stored.sha256,
        producer="fixture", producer_version="1", quality="partial",
        collected_at=datetime.now(timezone.utc), locator={"kind": "region", "value": "whole"},
        source_type="user_upload", source_evidence_id=ident)
    EvidenceService(repository).ingest(view.investigation_id, record)
    return record


async def diagnose(controller, view, state, request, ident):
    return await controller._execute_call(view, call("agent_analyze_evidence", request, ident),
        {}, state, time.time() + 30)


@pytest.mark.parametrize("mime", ["application/json", "application/json; charset=utf-8", "text/plain"])
@pytest.mark.parametrize("recipe", ["crash", "frame_time"])
def test_diagnostic_full_service_accepts_json_and_text(repository, settings, mime, recipe):
    async def scenario():
        view, controller, _ = setup(repository, settings)
        value = ({"crash": {"signal": "SIGSEGV", "dump_kind": "minidump",
            "symbols_matched": False, "frames": []}} if recipe == "crash" else
            {"samples": [{"frame_ms": v} for v in [10, 20, 30]]})
        seed(repository, controller, view, value, mime)
        result, added = await diagnose(controller, view, memory(), {"recipe": recipe,
            "evidence_id": "input", "data_pointer": "/crash" if recipe == "crash" else "/samples"}, "diag")
        assert added and "evidence_id" in result
        evidence = repository.get_evidence(result["evidence_id"])
        assert evidence.derived_from == ["input"] and evidence.quality == "partial"
    asyncio.run(scenario())


def test_duplicate_diagnostic_reuses_persisted_output_without_spending_or_progress(repository, settings):
    async def scenario():
        view, controller, _ = setup(repository, settings)
        seed(repository, controller, view, [{"frame_ms": n} for n in [10, 20, 30]])
        request = {"recipe": "frame_time", "evidence_id": "input"}
        first, added = await diagnose(controller, view, memory(), request, "diag-1")
        assert added
        # Replace the owner and discard all conversational caches.
        controller.diagnostics = NativeDiagnosticService(repository, controller.artifacts)
        controller.diagnostics.recipes.runner.run = lambda *a: pytest.fail("duplicate computation")
        second, added = await diagnose(controller, view, memory(), request, "diag-2")
        assert first == second and not added
        assert repository.budget_usage(view.investigation_id)["tool_reads"] == 1
        assert len(repository.list_recent_events_by_type(view.investigation_id, "analysis.diagnostic_completed")) == 1
    asyncio.run(scenario())


def test_diagnostic_recovers_after_ingestion_before_completion_event(repository, settings, monkeypatch):
    async def scenario():
        view, controller, _ = setup(repository, settings)
        seed(repository, controller, view, [{"frame_ms": n} for n in [10, 20, 30]])
        request = {"recipe": "frame_time", "evidence_id": "input"}
        def interrupted(*args):
            raise RuntimeError("interrupted after durable evidence")
        monkeypatch.setattr(controller.diagnostics, "_completed", interrupted)
        with pytest.raises(RuntimeError):
            await diagnose(controller, view, memory(), request, "diag-1")
        assert len(repository.list_current_evidence(view.investigation_id)) == 2
        controller.diagnostics = NativeDiagnosticService(repository, controller.artifacts)
        result, added = await diagnose(controller, view, memory(), request, "diag-1")
        assert "evidence_id" in result and not added
        assert repository.budget_usage(view.investigation_id)["tool_reads"] == 1
    asyncio.run(scenario())


def test_diagnostic_cache_includes_parameters_and_preserves_equal_outputs(repository, settings):
    async def scenario():
        view, controller, _ = setup(repository, settings)
        seed(repository, controller, view, [{"time": f"2026-10-31T00:00:0{i}Z", "frame_ms": n}
            for i, n in enumerate([10, 20, 30])])
        first, _ = await diagnose(controller, view, memory(), {"recipe": "frame_time",
            "evidence_id": "input", "timestamp_column": "time"}, "diag-1")
        request = {"recipe": "frame_time", "evidence_id": "input", "timestamp_column": "time",
                   "time_from": "2026-10-30T00:00:00Z"}
        second, _ = await diagnose(controller, view, memory(), request, "diag-2")
        assert first["result"] == second["result"]
        assert first["evidence_id"] != second["evidence_id"]
        third, added = await diagnose(controller, view, memory(), request, "diag-3")
        assert third == second and not added
        assert repository.budget_usage(view.investigation_id)["tool_reads"] == 2
    asyncio.run(scenario())


def test_search_reads_artifact_once_and_preserves_utf8_audits(repository, settings, monkeypatch):
    async def scenario():
        view, controller, _ = setup(repository, settings)
        seed(repository, controller, view, ("错误 crash " + "日志" * 100 + "\n") * 30, "text/plain")
        original = controller.artifacts.read_bytes
        reads = []
        def counted(ref):
            reads.append(ref)
            return original(ref)
        monkeypatch.setattr(controller.artifacts, "read_bytes", counted)
        result = await search_evidence_async(repository, controller.artifacts, view,
            EvidenceSearch(query="crash", max_hits=20))
        assert len(result["hits"]) == 20 and len(reads) == 1
        audits = repository.list_recent_events_by_type(view.investigation_id, "evidence.excerpt_read", limit=100)
        raw = original(reads[0])
        assert len(audits) == 20
        for row in audits:
            excerpt = row["data"]["excerpt"]
            start = excerpt["byte_offset"]
            body = raw[start:start + excerpt["returned_bytes"]]
            assert body.decode() == excerpt["text"]
            assert hashlib.sha256(body).hexdigest() == excerpt["excerpt_sha256"]
            assert excerpt["total_bytes"] == len(raw)
    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["search", "diagnostic"])
def test_file_io_yields_and_repository_stays_on_owner_thread(repository, settings, monkeypatch, operation):
    async def scenario():
        view, controller, _ = setup(repository, settings)
        seed(repository, controller, view, [{"frame_ms": n} for n in [10, 20, 30]])
        owner = threading.get_ident()
        original_read, original_event = controller.artifacts.read_bytes, repository.record_investigation_event
        io_started, release = threading.Event(), threading.Event()
        def slow_read(ref):
            assert threading.get_ident() != owner
            io_started.set()
            assert release.wait(5)
            return original_read(ref)
        def event(*args, **kwargs):
            assert threading.get_ident() == owner
            return original_event(*args, **kwargs)
        monkeypatch.setattr(controller.artifacts, "read_bytes", slow_read)
        monkeypatch.setattr(repository, "record_investigation_event", event)
        work = (search_evidence_async(repository, controller.artifacts, view, EvidenceSearch(query="frame_ms"))
            if operation == "search" else diagnose(controller, view, memory(),
                {"recipe": "frame_time", "evidence_id": "input"}, "diag"))
        task = asyncio.create_task(work)
        try:
            async def wait_for_io():
                while not io_started.is_set():
                    await asyncio.sleep(.001)
            await asyncio.wait_for(wait_for_io(), timeout=5)
            assert not task.done()  # The event loop remained responsive while I/O was blocked.
        finally:
            release.set()
        await task
    asyncio.run(scenario())


def new_request(repository, index):
    return repository.create_investigation(InvestigationRequest(tenant_id="studio-a", project_id="game-a",
        objective={"objective_id": "diagnose", "description": "Queue regression"}), f"queue-{index}")[0]


def test_local_overflow_remains_durable_and_recovers_after_restart(repository):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        class Controller:
            execution_mode = "typed"
            _operation_updates = {}
            def notify_event(self, *args):
                pass
            async def run(self, investigation_id, **kwargs):
                entered.set()
                await release.wait()
        def backend():
            return LocalWorkflowBackend(repository, None, SimpleNamespace(model_name="disabled"),
                ReportService(repository), controller=Controller(),
                execution_limits=ExecutionLimits(investigations=1, max_waiters=0))
        views = [new_request(repository, i) for i in range(3)]
        first = backend()
        first.start(views[0].investigation_id)
        await entered.wait()
        for view in views[1:]:
            first.start(view.investigation_id)
        assert len(first._tasks) == 1 and len(first._pending) == 2
        assert {r["investigation_id"] for r in repository.pending_workflow_starts()} == {
            v.investigation_id for v in views[1:]}
        assert all(repository.get_investigation(v.investigation_id).status == "accepted" for v in views)
        await first.close()
        second = backend()
        second.recover()
        assert len(second._tasks) == 1 and len(second._pending) == 2
        # Cancellation of a queued investigation must bypass the busy execution pool.
        queued = views[-1].investigation_id
        second.request_cancel(queued)
        await second._tasks[queued]
        assert repository.get_investigation(queued).status == "canceled"
        assert queued not in {r["investigation_id"] for r in repository.pending_workflow_starts()}
        await second.close()
    asyncio.run(scenario())


def test_local_deferred_work_runs_once_after_capacity_returns(repository):
    async def scenario():
        visited = []
        limits = ExecutionLimits(investigations=1, max_waiters=0)
        class Controller:
            execution_mode = "typed"
            _operation_updates = {}
            async def run(self, investigation_id, **kwargs):
                visited.append(investigation_id)
        backend = LocalWorkflowBackend(repository, None, SimpleNamespace(model_name="disabled"),
            ReportService(repository), controller=Controller(), execution_limits=limits)
        view = new_request(repository, "deferred")
        try:
            async with limits.slot("investigations"):
                backend.start(view.investigation_id)
                async def deferred():
                    while view.investigation_id not in backend._pending:
                        await asyncio.sleep(.001)
                await asyncio.wait_for(deferred(), 3)
                assert not visited
                assert repository.get_investigation(view.investigation_id).status == "accepted"
            async def finished():
                while not visited:
                    await asyncio.sleep(.001)
            await asyncio.wait_for(finished(), 3)
            assert visited == [view.investigation_id]
        finally:
            await backend.close()
    asyncio.run(scenario())


def test_temporal_capacity_deferral_does_not_publish_failure_or_spend_round(repository, settings):
    async def scenario():
        view, controller, _ = setup(repository, settings)
        owner = CoreActivities(repository, replace(settings, mcp_native_tools=True))
        owner.native = controller
        owner.execution_limits = ExecutionLimits(investigations=1, max_waiters=0)
        async with owner.execution_limits.slot("investigations"):
            result = await owner.native_investigation_step(view.investigation_id)
        assert result == {"done": False, "status": "accepted", "capacity_deferred": True}
        assert not repository.list_recent_events_by_type(view.investigation_id, "report.published")
        assert repository.budget_usage(view.investigation_id).get("native_model_turns", 0) == 0
    asyncio.run(scenario())


def test_temporal_capacity_waits_do_not_consume_round_bound(monkeypatch):
    from game_test_agent.workflows import temporal
    async def scenario():
        instance = temporal.InvestigationWorkflow()
        attempts = 0
        def start_activity(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            async def step():
                return ({"done": False, "status": "accepted", "capacity_deferred": True}
                    if attempts < 6 else {"done": True, "status": "completed"})
            return asyncio.create_task(step())
        async def wait_condition(predicate, **kwargs):
            if "timeout" in kwargs:
                raise TimeoutError()
            await asyncio.Future()
        monkeypatch.setattr(temporal.workflow, "start_activity", start_activity)
        monkeypatch.setattr(temporal.workflow, "wait_condition", wait_condition)
        monkeypatch.setattr(temporal.workflow, "wait", asyncio.wait, raising=False)
        assert await instance._run_native("inv", {"budget": {"max_rounds": 1,
            "max_wall_seconds": 60}}, defer_capacity=True) == "completed"
        assert attempts == 6
    asyncio.run(scenario())
