"""Offline runtime parity, admission, trace and registered diagnostic regressions."""
import asyncio
import json
import time
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from unittest.mock import AsyncMock

from game_test_agent.analysis.diagnostics import DiagnosticRequest, crash_diagnostic, frame_diagnostic
from game_test_agent.api import create_app
from game_test_agent.observability.tracing import InvestigationTracer
from game_test_agent.runtime.execution import CapacityExceeded, ExecutionLimits
from game_test_agent.workflows.native_evidence import EvidenceSearch, search_evidence_async
from game_test_agent.workflows.native_tools import ToolLookup, lookup_tools
from game_test_agent.workflows.worker import CoreActivities
from native_plan_helpers import initial_plan_call
from test_native_enhancements import collect, document
from test_native_reliability import call, final, response, run, setup


def test_native_rounds_can_be_resumed_by_temporal_activity_without_replanning(repository, settings):
    async def scenario():
        view, controller, provider = setup(repository, settings, rounds=4)
        from game_test_agent.contracts.models import InvestigationRequest
        view, _ = repository.create_investigation(InvestigationRequest(
            tenant_id="studio-a", project_id="game-a", objective={"objective_id":"diagnose", "description":"Native parity"},
            budget={"max_rounds":4}), "native-temporal-parity", principal_id="tester", scopes={"runs:read","evidence:read"})
        owner = CoreActivities(repository, replace(settings, mcp_native_tools=True, workflow_backend="temporal"))
        owner.native = controller
        turns = 0
        async def model(messages, tools):
            nonlocal turns
            turns += 1
            return response(initial_plan_call(), call()) if turns == 1 else final(messages)
        controller._model_call = model
        control = await owner.load_control_state(view.investigation_id)
        assert control["execution_mode"] == "native_mcp"
        first = await owner.native_investigation_step(view.investigation_id, True)
        assert not first["done"] and first["status"] == "planning"
        # Replace the activity owner, keeping only durable state and the test provider.
        resumed = CoreActivities(repository, owner.settings)
        resumed.native = type(controller)(controller.settings, repository, controller.providers,
                                         controller.artifacts, controller.reports)
        resumed.native._model_call = model
        second = await resumed.native_investigation_step(view.investigation_id, True)
        assert second["done"] and second["status"] == "completed"
        assert second["worker_step"]["state"] == "completed"
        assert second["worker_step"]["continuation"]["next_turn"] == 3
        provider.call_native.assert_awaited_once()
        assert repository.budget_usage(view.investigation_id)["native_model_turns"] == 2
        assert repository.get_latest_report(view.investigation_id)[0].stop_reason == "native_mcp_investigation_complete"
    asyncio.run(scenario())


def test_execution_mode_is_frozen_across_configuration_changes(repository, settings):
    view, _, _ = setup(repository, settings)
    assert repository.investigation_execution_mode(view.investigation_id, "native_mcp") == "native_mcp"
    assert repository.investigation_execution_mode(view.investigation_id, "typed") == "native_mcp"
    replace(settings, mcp_native_tools=True, workflow_backend="temporal").validate()


def test_native_activity_cleans_up_a_persisted_cancel_before_signal_delivery(repository, settings):
    async def scenario():
        view, controller, _ = setup(repository, settings)
        owner = CoreActivities(repository, replace(settings, mcp_native_tools=True))
        owner.native = controller
        repository.transition(view.investigation_id, "canceling")
        result = await owner.native_investigation_step(view.investigation_id)
        assert result == {"done": True, "status": "canceled"}
        assert repository.get_investigation(view.investigation_id).status == "canceled"
    asyncio.run(scenario())


def test_admission_bounds_concurrency_and_releases_canceled_waiters():
    async def scenario():
        limits = ExecutionLimits(models=1, max_waiters=1)
        entered = asyncio.Event()
        async def waiting():
            async with limits.slot("models"):
                entered.set()
        async with limits.slot("models"):
            task = asyncio.create_task(waiting())
            await asyncio.sleep(0)
            with pytest.raises(CapacityExceeded):
                async with limits.slot("models"):
                    pass
            assert not entered.is_set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert limits.snapshot()["models"]["waiting"] == 0
        async with limits.slot("models"):
            assert limits.snapshot()["models"]["active"] == 1
        assert limits.snapshot()["models"]["active"] == 0
    asyncio.run(scenario())


def test_admission_deadline_never_dispatches_queued_work():
    async def scenario():
        limits = ExecutionLimits(tools=1)
        async with limits.slot("tools"):
            with pytest.raises(TimeoutError):
                async with limits.slot("tools", deadline=time.time() + .01):
                    pytest.fail("expired operation was dispatched")
        assert limits.snapshot()["tools"]["waiting"] == 0
    asyncio.run(scenario())


def test_trace_parentage_error_class_and_sensitive_payload_exclusion(repository, settings):
    view, _, _ = setup(repository, settings)
    tracer = InvestigationTracer(repository)
    with tracer.span(view.investigation_id, "investigation.run", backend="local"):
        with pytest.raises(ValueError):
            with tracer.span(view.investigation_id, "tool.execute", tool_name="read", token="secret"):
                raise ValueError("secret text")
    spans = [r["data"] for r in repository.list_recent_events_by_type(view.investigation_id, "trace.span")]
    child, parent = spans
    assert child["parent_span_id"] == parent["span_id"]
    assert child["trace_id"] == parent["trace_id"] and child["error_type"] == "ValueError"
    assert "secret" not in json.dumps(spans)


def crash_payload(symbols=True):
    return {"signal": "EXCEPTION_ACCESS_VIOLATION", "dump_kind": "minidump", "symbols_matched": symbols,
            "frames": [{"index": 0, "module": "Game", "symbol": "Actor::Tick", "file": "Actor.cpp", "line": 42}]}


def material(value):
    return {"inputs": {"evidence": {"input": json.dumps(value)}}}


@pytest.mark.parametrize("symbols", [True, False])
def test_crash_analysis_respects_matching_symbols_and_keeps_causal_limit(symbols):
    request = DiagnosticRequest(recipe="crash", evidence_id="input", data_pointer="/crash")
    result = crash_diagnostic(material({"crash": crash_payload(symbols)}), request.model_dump(mode="json"))
    assert bool(result["top_locatable_frame"]) == symbols
    assert result["frame_count"] == 1 and "not a confirmed root cause" in result["limitations"][0]


def test_frame_analysis_aligns_peak_components_and_filters_explicit_time_window():
    rows = [{"time": f"2026-10-29T10:00:0{i}+08:00", "frame_ms": value, "cpu_ms": value / 2, "gpu_ms": value / 3}
            for i, value in enumerate([999, 10, 20, 40])]
    request = DiagnosticRequest(recipe="frame_time", evidence_id="input", data_pointer="/samples",
        component_columns=["cpu_ms", "gpu_ms"], timestamp_column="time", time_from="2026-10-29T02:00:01Z")
    result = frame_diagnostic(material({"samples": rows}), request.model_dump(mode="json"))
    assert result["selected_samples"] == 3 and result["columns"]["frame_ms"]["p95"] == 40
    assert result["worst_frame"] == {"source_row": 3, "aligned_measurements_ms": {"frame_ms": 40, "cpu_ms": 20, "gpu_ms": 40 / 3}}


@pytest.mark.parametrize("invalid", [float("nan"), -1, True, "47"])
def test_frame_recipe_rejects_invalid_or_implicitly_converted_values(invalid):
    with pytest.raises(ValueError):
        frame_diagnostic(material([{"frame_ms": value} for value in [1, 2, invalid]]),
                         DiagnosticRequest(recipe="frame_time", evidence_id="input").model_dump(mode="json"))


def test_registered_native_analysis_persists_derived_evidence_without_promoting_trust(repository, settings):
    async def scenario():
        view, controller, _, memory, ids = await collect(repository, settings, [document(crash=crash_payload())])
        payload, progress = await controller._execute_call(view,
            call("agent_analyze_evidence", {"recipe": "crash", "evidence_id": ids[0], "data_pointer": "/crash"}, "analysis-1"),
            {}, memory, time.time() + 60)
        assert progress and "evidence_id" in payload
        output = repository.get_evidence(payload["evidence_id"])
        assert output.derived_from == ids and output.source_type == "analysis"
        assert output.execution_source == "unknown" and output.quality == "partial"
        assert json.loads(controller.artifacts.read_bytes(output.artifact_ref))["symbols_matched"] is True
    asyncio.run(scenario())


def test_progressive_lookup_returns_original_schema_and_only_authorized_names():
    tool = SimpleNamespace(description="Read crash stack", input_schema={"type": "object", "properties": {"id": {"type": "string"}}})
    memory = SimpleNamespace(enabled_tools=[])
    result, progress = lookup_tools({"read_crash": (None, tool)}, memory,
                                    ToolLookup(query="crash").model_dump_json())
    assert memory.enabled_tools == ["read_crash"] and not progress
    assert result["tools"][0]["parameters"] == tool.input_schema


def test_async_regex_matching_yields_to_owner_event_loop(repository, settings):
    async def scenario():
        view, controller, _, _, _ = await collect(repository, settings, [document()])
        tick = asyncio.Event()
        async def concurrent():
            await asyncio.sleep(0)
            tick.set()
        task = asyncio.create_task(concurrent())
        result = await search_evidence_async(repository, controller.artifacts, view, EvidenceSearch(query="crash|失败", regex=True))
        assert tick.is_set() and result["hits"]
        await task
    asyncio.run(scenario())


def test_trace_and_offline_trajectory_enforce_scope_and_keep_evaluation_deferred(repository, settings):
    async def scenario():
        view, controller, _ = setup(repository, settings, rounds=3)
        calls = 0
        async def model(messages, tools):
            nonlocal calls
            calls += 1
            return response(initial_plan_call(), call()) if calls == 1 else final(messages)
        controller._model_call = model
        await run(controller, view)
        app = create_app(settings, repository)
        headers = {"X-Tenant-ID": "studio-a", "X-Project-ID": "game-a", "X-Principal-ID": "tester"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            trace = await client.get(f"/v1/investigations/{view.investigation_id}/trace", headers=headers)
            assert trace.status_code == 200 and trace.json()["spans"]
            trajectory = await client.get(f"/v1/investigations/{view.investigation_id}/trajectory", headers=headers)
            assert trajectory.status_code == 200 and not trajectory.json()["evaluation"]["performed"]
            forbidden = await client.get(f"/v1/investigations/{view.investigation_id}/trace",
                                        headers={**headers, "X-Project-ID": "other"})
            assert forbidden.status_code == 403
    asyncio.run(scenario())


def test_temporal_workflows_prepare_under_sdk_deterministic_sandbox():
    from temporalio import workflow
    from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner
    from game_test_agent.workflows.temporal import InvestigationWorkflow, CleanupWorkflow
    async def scenario():
        for cls in [InvestigationWorkflow, CleanupWorkflow]:
            SandboxedWorkflowRunner().prepare_workflow(workflow._Definition.must_from_class(cls))
    asyncio.run(scenario())


def test_scoped_capacity_metrics_do_not_reveal_other_projects():
    async def scenario():
        limits = ExecutionLimits(models=2)
        async with limits.slot("models", scope=("a", "project-a")):
            assert limits.snapshot(("a", "project-a"))["models"]["active"] == 1
            assert limits.snapshot(("b", "project-b"))["models"]["active"] == 0
        assert not limits._scopes
    asyncio.run(scenario())


def test_claim_checks_do_not_treat_literal_quotes_as_verified_causality(repository, settings):
    async def scenario():
        view, controller, _ = setup(repository, settings, rounds=3)
        turn = 0
        async def model(messages, tools):
            nonlocal turn
            turn += 1
            return response(initial_plan_call(), call()) if turn == 1 else final(messages)
        controller._model_call = model
        report = await run(controller, view)
        check = report.claims[0].verification_details
        assert check.citation == "verified" and check.support == "literal_only"
        assert check.provenance == "unknown" and check.causality == "not_verified"
    asyncio.run(scenario())


def test_tracing_and_admission_preserve_immutable_provider_exception(repository, settings):
    from game_test_agent.mcp_gateway.gateway import StandardToolError
    async def scenario():
        view, _, _ = setup(repository, settings)
        error = StandardToolError("CONNECTION_LOST", "private detail", side_effect_status="unknown")
        with pytest.raises(StandardToolError) as caught:
            async with ExecutionLimits().slot("tools"):
                with InvestigationTracer(repository).span(view.investigation_id, "tool.execute"):
                    raise error
        assert caught.value is error
    asyncio.run(scenario())


def test_temporal_native_workflow_drives_bounded_rounds(monkeypatch):
    from game_test_agent.workflows import temporal
    async def scenario():
        instance = temporal.InvestigationWorkflow()
        calls = []
        def start_activity(name, **kwargs):
            calls.append(name)
            async def step():
                return {"done": len(calls) == 2, "status": "completed" if len(calls) == 2 else "planning"}
            return asyncio.create_task(step())
        async def wait_condition(predicate):
            await asyncio.Future()
        monkeypatch.setattr(temporal.workflow, "start_activity", start_activity)
        monkeypatch.setattr(temporal.workflow, "wait_condition", wait_condition)
        assert await instance._run_native("inv", {"budget": {"max_rounds": 4, "max_wall_seconds": 60}}) == "completed"
        assert calls == ["native_investigation_step", "native_investigation_step"]
    asyncio.run(scenario())


def test_temporal_native_cancel_waits_for_activity_cancellation(monkeypatch):
    from game_test_agent.workflows import temporal
    async def scenario():
        instance = temporal.InvestigationWorkflow()
        instance._cancel = AsyncMock(return_value="canceled")
        started, canceled = asyncio.Event(), asyncio.Event()
        async def pending():
            started.set()
            try:
                await asyncio.Future()
            finally:
                canceled.set()
        monkeypatch.setattr(temporal.workflow, "start_activity", lambda *a, **k: asyncio.create_task(pending()))
        async def wait_condition(predicate):
            while not predicate():
                await asyncio.sleep(0)
        monkeypatch.setattr(temporal.workflow, "wait_condition", wait_condition)
        task = asyncio.create_task(instance._run_native("inv", {"budget": {"max_rounds": 4, "max_wall_seconds": 60}}))
        await started.wait()
        await instance.cancel("cancel-1")
        assert await task == "canceled" and canceled.is_set()
        instance._cancel.assert_awaited_once_with("inv")
    asyncio.run(scenario())


def test_source_index_does_not_drop_sources_after_500_events(repository, settings):
    from game_test_agent.workflows.native_evidence import source_index
    view, _, _ = setup(repository, settings)
    for index in range(510):
        repository.record_investigation_event(view.investigation_id, "mcp.native.tool_completed",
            {"evidence_id": str(index), "source": {"values": {"run_id": str(index)}}})
    assert len(source_index(repository, view.investigation_id)) == 510


def test_native_project_knowledge_is_resolved_with_scope_and_not_current_evidence(repository, settings):
    from game_test_agent.contracts.models import ProjectPack
    from game_test_agent.memory import ProjectPackRegistry
    from game_test_agent.workflows.native_context import NativeMemory
    from game_test_agent.workflows.native_knowledge import refresh_knowledge
    view, _, _ = setup(repository, settings, constraints={"project_pack_version": "p1"})
    ProjectPackRegistry(repository).register(ProjectPack(pack_id="pack", version="p1", tenant_id="studio-a", project_id="game-a",
        terminology={"frame": "one captured frame"}, reviewed_by="reviewer"))
    memory = NativeMemory()
    refresh_knowledge(repository, view, memory)
    assert memory.project_knowledge["project_pack"]["pack_id"] == "pack"
    assert "not current-run evidence" in memory.project_knowledge["instructions"]
