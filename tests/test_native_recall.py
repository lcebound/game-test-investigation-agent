"""Recoverability, scoped reads and bounded context, without real models."""
import asyncio
import hashlib
import json
import time
from dataclasses import replace
import pytest

from game_test_agent.contracts.models import InputCommand, InvestigationRequest
from game_test_agent.storage import canonical_json
from game_test_agent.workflows.native_context import NativeMemory, fit_native_context, report_context
from game_test_agent.workflows.native_recall import ContextRead, input_page
from native_plan_helpers import initial_plan_call
from test_native_enhancements import collect
from test_native_reliability import call, final, response, run, setup


def add_input(repository, view, identity, content):
    repository.enqueue_command(view.investigation_id, identity, "input",
        InputCommand(command_id=identity, expected_revision=view.revision, content=content))


def memory():
    return NativeMemory(messages=[{"role": "system", "content": "system"},
                                  {"role": "user", "content": "{}"},
                                  {"role": "user", "content": "{}"}])


def test_input_directory_recalls_omitted_middle_and_utf8_pages(repository, settings):
    view, controller, _ = setup(repository, settings)
    original = "早期要求：" + "中" * 300 + "TARGET" + "末" * 400
    for index in range(7):
        add_input(repository, view, f"input-{index}", original if index == 2 else f"detail-{index}")
    state = memory()
    state.sync_inputs(repository, view.investigation_id)
    working = json.loads(state.messages[2]["content"])
    assert working["latest_user_inputs"][0]["command_id"] == "input-0"
    assert "input-2" not in {item["command_id"] for item in working["latest_user_inputs"]}
    assert working["user_input_directory"]["omitted_items"] == 3
    page = input_page(repository, view.investigation_id, ContextRead(ref="inputs", limit=3))
    reference = page["items"][2]
    assert page["next_offset"] == 3 and reference["read_ref"] == "input/input-2"
    assert reference["truncated"]

    async def scenario():
        offset, parts = 0, []
        while True:
            payload, _ = await controller._execute_call(view, call("agent_read_context",
                {"ref": reference["read_ref"], "byte_offset": offset, "max_bytes": 512,
                 "expected_sha256": reference["sha256"]}, f"page-{offset}"), {}, state, time.time() + 10)
            assert payload["sha256"] == hashlib.sha256(original.encode()).hexdigest()
            assert payload["excerpt_sha256"] == hashlib.sha256(payload["result"].encode()).hexdigest()
            parts.append(payload["result"])
            if payload["next_byte_offset"] is None:
                break
            assert payload["next_byte_offset"] > offset
            offset = payload["next_byte_offset"]
        assert "".join(parts) == original
        assert reference["read_ref"] not in state.input_gaps()
        state.save(repository, controller.artifacts, view)
        restored = NativeMemory.restore(repository, controller.artifacts, view)
        assert restored.input_read_ranges == state.input_read_ranges
    asyncio.run(scenario())


def test_recall_is_local_hash_checked_and_cached_without_double_charge(repository, settings):
    view, controller, _ = setup(repository, settings)
    add_input(repository, view, "own", "Do not describe history as current.")
    other, _ = repository.create_investigation(InvestigationRequest(
        tenant_id="studio-a", project_id="game-a",
        objective={"objective_id": "other", "description": "Independent task"}), "other-task")
    add_input(repository, other, "foreign", "OTHER_TASK_SECRET")
    state = memory()
    state.sync_inputs(repository, view.investigation_id)

    async def scenario():
        first, _ = await controller._execute_call(view,
            call("agent_read_context", {"ref": "input/own"}, "first"), {}, state, time.time() + 10)
        count = repository.budget_usage(view.investigation_id)["tool_reads"]
        reused, changed = await controller._execute_call(view,
            call("agent_read_context", {"ref": "input/own"}, "repeat"), {}, state, time.time() + 10)
        assert reused == first and not changed
        assert repository.budget_usage(view.investigation_id)["tool_reads"] == count
        for index, args in enumerate([
            {"ref": "input/foreign"},
            {"ref": "../../agent.db"},
            {"ref": "input/own", "expected_sha256": "0" * 64},
        ]):
            rejected, _ = await controller._execute_call(view,
                call("agent_read_context", args, f"bad-{index}"), {}, state, time.time() + 10)
            assert rejected.get("error_code")
            assert "OTHER_TASK_SECRET" not in canonical_json(rejected)
        # Identical call IDs with different resources may not bypass the budget.
        rejected, _ = await controller._execute_call(view,
            call("agent_read_context", {"ref": "inputs"}, "first"), {}, state, time.time() + 10)
        assert rejected["error_code"] == "CALL_ID_REUSED"
    asyncio.run(scenario())


def test_large_tool_result_spills_contiguously_and_can_recall_hidden_fact(repository, settings):
    async def scenario():
        text = "头" * 1800 + "needle: 47 ms" + "尾" * 1800
        view, controller, provider, state, ids = await collect(repository, settings, [{"log": text}])
        saved = controller.artifacts.read_bytes(repository.list_current_evidence(view.investigation_id)[0].artifact_ref)
        memo = next(iter(state.cache.values()))["payload"]
        assert memo["spilled"] and memo["truncated"]
        assert len(memo["result"].encode()) <= 1200 and "needle" not in memo["result"]
        assert saved.startswith(memo["result"].encode())
        assert memo["next_byte_offset"] == len(memo["result"].encode())
        result, _ = await controller._execute_call(view,
            call("agent_read_context", {"ref": memo["read_ref"], "query": "needle: 47 ms",
                                      "expected_sha256": memo["sha256"]}, "recall"),
            {}, state, time.time() + 10)
        assert "needle: 47 ms" in result["result"] and result["matched"]
        assert saved[result["byte_offset"]:].startswith(result["result"].encode())
        provider.call_native.assert_awaited_once()
        assert result["evidence_id"] == ids[0]
    asyncio.run(scenario())

def test_directory_response_is_bounded_and_offsets_reject_split_characters(repository, settings):
    view, _, _ = setup(repository, settings)
    for index in range(8):
        add_input(repository, view, f"entry-{index}", "中文" * 80)
    request = ContextRead(ref="inputs", limit=20, max_bytes=512)
    page = input_page(repository, view.investigation_id, request)
    assert len(canonical_json(page).encode()) <= 512 and page["next_offset"]
    with pytest.raises(ValueError):
        input_page(repository, view.investigation_id,
                   ContextRead(ref="input/entry-0", byte_offset=1))
    result = input_page(repository, view.investigation_id,
                        ContextRead(ref="input/entry-0", query="不存在", byte_offset=3))
    assert not result["matched"] and result["searched_from_byte"] == 3


def test_recall_respects_read_budget_before_content_access(repository, settings):
    view, controller, _ = setup(repository, settings)
    add_input(repository, view, "sensitive", "CONTENT_MUST_NOT_BE_READ")
    view = view.model_copy(update={"request": view.request.model_copy(update={
        "budget": view.request.budget.model_copy(update={"max_tool_reads": 0})})})
    state = memory()
    async def scenario():
        result, _ = await controller._execute_call(view,
            call("agent_read_context", {"ref": "input/sensitive"}, "no-budget"),
            {}, state, time.time() + 10)
        assert "CONTENT_MUST_NOT_BE_READ" not in canonical_json(result)
        assert state.finalize and not repository.list_recent_events_by_type(
            view.investigation_id, "mcp.native.context_read")
    asyncio.run(scenario())


def test_long_inputs_fit_report_and_remain_recoverable(repository, settings):
    view, controller, _ = setup(repository, settings)
    for index in range(4):
        add_input(repository, view, f"long-{index}", "补充说明" * 1000)
    state = memory()
    state.sync_inputs(repository, view.investigation_id)
    assert len(canonical_json(state.messages).encode()) < 10000
    report = report_context(state)
    assert fit_native_context(report, [], 24576)
    working = json.loads(report[2]["content"])
    assert working["user_input_directory"]["incomplete_inputs"] == 4
    assert all(item["truncated"] and item["read_ref"] for item in working["latest_user_inputs"])
    assert input_page(repository, view.investigation_id,
                      ContextRead(ref="input/long-0", byte_offset=1200))["result"]
    state.save(repository, controller.artifacts, view)
    # Simulate a v1/v2 checkpoint without the newly added range field or cards.
    event = repository.list_recent_events_by_type(view.investigation_id, "mcp.native.checkpoint")[0]
    raw = json.loads(controller.artifacts.read_bytes(event["data"]["artifact_ref"]))
    raw["state"].pop("input_read_ranges")
    raw["state"]["inputs"] = [{"command_id": "long-0", "content": "old truncated preview"}]
    stored = controller.artifacts.put_bytes(view.request.tenant_id, view.request.project_id,
                                           canonical_json(raw).encode())
    repository.record_investigation_event(view.investigation_id, "mcp.native.checkpoint",
                                         {"version": 2, "artifact_ref": stored.ref})
    restored = NativeMemory.restore(repository, controller.artifacts, view)
    restored.refresh_working_message()
    assert len(restored.inputs) == 4 and restored.inputs[0]["sha256"] == state.inputs[0]["sha256"]


def test_followup_exposes_diagnostics_even_when_keyword_was_spilled(repository, settings):
    async def scenario():
        view, controller, provider = setup(repository, settings)
        add_input(repository, view, "performance", "详细背景" * 600 + " 请分析 CPU/GPU 帧耗时")
        turns = 0
        async def model(messages, tools):
            nonlocal turns
            turns += 1
            if turns == 1:
                names = {tool["function"]["name"] for tool in tools}
                assert {"agent_analyze_evidence", "agent_read_context"}.issubset(names)
                assert "CPU/GPU" not in messages[2]["content"]
                return response(initial_plan_call(), call())
            return final(messages)
        controller._model_call = model
        report = await run(controller, view)
        assert report.status == "completed"
        assert any("未完整召回" in gap for gap in report.unresolved_questions)
        provider.call_native.assert_awaited_once()
    asyncio.run(scenario())


def test_read_result_recovers_before_next_checkpoint_without_repeating_tool(repository, settings):
    async def scenario():
        view, controller, _ = setup(repository, settings)
        add_input(repository, view, "long", "完整要求" * 300)
        state = memory()
        state.sync_inputs(repository, view.investigation_id)
        state.save(repository, controller.artifacts, view)  # Before the recall.
        payload, _ = await controller._execute_call(view,
            call("agent_read_context", {"ref": "input/long"}, "read-input"), {}, state, time.time() + 10)
        assert payload["next_byte_offset"] is None
        async def model(messages, tools):
            assert json.loads(messages[2]["content"])["user_input_directory"]["incomplete_inputs"] == 0
            return response(initial_plan_call())
        controller._model_call = model
        assert await controller.run(view.investigation_id, principal_id="test",
                                    scopes={"runs:read", "evidence:read"}, max_model_turns=1) is None
        restored = NativeMemory.restore(repository, controller.artifacts, view)
        assert not restored.input_gaps()
        assert repository.budget_usage(view.investigation_id)["tool_reads"] == 1
    asyncio.run(scenario())


def test_budget_pressure_with_long_inputs_can_still_publish_finite_report(repository, settings):
    async def scenario():
        view, controller, provider = setup(repository, settings)
        for index in range(4):
            add_input(repository, view, f"long-{index}", "补充说明" * 1000)
        turns = 0
        async def model(messages, tools):
            nonlocal turns
            turns += 1
            if turns == 1:
                controller.settings = replace(controller.settings, model_max_total_tokens=30000)
                return response(initial_plan_call(), call())
            assert {tool["function"]["name"] for tool in tools} == {"agent_update_plan", "agent_submit_report"}
            return final(messages)
        controller._model_call = model
        report = await run(controller, view)
        assert report.status == "completed" and turns == 2
        assert any("未完整召回" in gap for gap in report.unresolved_questions)
        provider.call_native.assert_awaited_once()
    asyncio.run(scenario())
