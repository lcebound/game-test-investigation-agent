"""Offline reproductions of the observed memory, budget and failure defects."""
import asyncio
import json
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from game_test_agent.workflows.native_context import NativeMemory, fit_native_context
from game_test_agent.contracts.models import InvestigationStatus
from game_test_agent.storage import ConflictError
from native_plan_helpers import initial_plan_call
from test_native_enhancements import collect, document
from test_native_reliability import call, final, response, run, setup


def test_plain_read_survives_dropped_tool_group_and_checkpoint(repository, settings):
    view, controller, _ = setup(repository, settings)
    memory = NativeMemory(messages=[{"role": "system", "content": "system"},
                                   {"role": "user", "content": "question"},
                                   {"role": "user", "content": "{}"}])
    memory.evidence_index["e1"] = {"evidence_id": "e1", "sha256": "a" * 64}
    memory.remember_search({"evidence_id": "e1", "matched": True, "query": None,
                            "byte_offset": 100, "total_bytes": 9000,
                            "result": "Recovery has not been independently verified."})
    memory.messages.extend([
        {"role": "assistant", "tool_calls": [{"id": "c", "function": {"arguments": "x" * 8000}}]},
        {"role": "tool", "tool_call_id": "c", "content": '{"result":"original"}'},
    ])
    assert fit_native_context(memory.messages, [], 4000)
    assert not any(m["role"] == "tool" for m in memory.messages)
    memory.save(repository, controller.artifacts, view)
    restored = NativeMemory.restore(repository, controller.artifacts, view)
    excerpt = json.loads(restored.messages[2]["content"])["collected_evidence_search_hits"][0]
    assert excerpt["original_excerpt"] == "Recovery has not been independently verified."
    assert excerpt["sha256"] == "a" * 64 and excerpt["byte_offset"] == 100
    assert excerpt["partial"] and excerpt["total_bytes"] == 9000


def test_empty_and_null_queries_reuse_the_same_read(repository, settings):
    async def scenario():
        view, controller, _, memory, ids = await collect(repository, settings, [document()])
        for index, query in enumerate([None, ""]):
            payload, _ = await controller._execute_call(view,
                call("agent_read_collected_evidence", {"evidence_id": ids[0], "query": query}, f"r{index}"),
                {}, memory, datetime.now(timezone.utc).timestamp() + 10)
            assert payload["matched"]
        assert repository.budget_usage(view.investigation_id)["tool_reads"] == 2  # Seed + one read.
        assert repository.list_recent_events_by_type(view.investigation_id, "mcp.native.tool_reused")
        assert memory.notes
    asyncio.run(scenario())


def test_budget_pressure_switches_to_small_report_before_next_read(repository, settings):
    async def scenario():
        view, controller, provider = setup(repository, settings, rounds=8)
        count = 0

        async def model(messages, tools):
            nonlocal count
            count += 1
            if count == 1:
                # The first response settles at 120 tokens; the next exploration
                # plus its report reserve cannot fit, but a compact report can.
                controller.settings = replace(controller.settings, model_max_total_tokens=30000)
                return response(initial_plan_call(), call())
            assert {t["function"]["name"] for t in tools} == {"agent_update_plan", "agent_submit_report"}
            assert "47 ms" in messages[2]["content"]
            return final(messages)

        controller._model_call = model
        report = await run(controller, view)
        assert report.stop_reason == "native_mcp_investigation_complete"
        assert report.status == "completed" and count == 2
        provider.call_native.assert_awaited_once()
        selected = repository.list_recent_events_by_type(view.investigation_id, "mcp.native.report_budget_selected")
        assert selected and selected[0]["data"]["available_tokens"] > 0
        reservations = repository.list_recent_events_by_type(view.investigation_id, "mcp.native.model_reserved")
        assert reservations[0]["data"]["report_reserve_tokens"] > 0
        assert reservations[-1]["data"]["report_reserve_tokens"] == 0
    asyncio.run(scenario())


@pytest.mark.parametrize("reason", ["native_model_unavailable", "native_model_token_budget_exhausted",
                                   "native_report_validation_failed", "native_context_budget_exhausted"])
def test_no_diagnosis_is_failed_in_report_and_persisted_state(repository, settings, reason):
    view, controller, _ = setup(repository, settings)
    repository.transition(view.investigation_id, InvestigationStatus.DISCOVERING)
    report = controller._finish(view, None, reason)
    assert report.status == repository.get_investigation(view.investigation_id).status == "failed"
    assert "未形成有效调查结论" in report.summary
    assert "补充能够区分" not in " ".join(report.recommended_actions)
    assert repository.list_recent_events_by_type(view.investigation_id, "mcp.native.outcome")[0]["data"]["diagnosis_available"] is False


@pytest.mark.parametrize("recover", [False, True])
def test_truncated_tool_batch_is_charged_but_never_dispatched(repository, settings, recover):
    async def scenario():
        view, controller, provider = setup(repository, settings)
        truncated = response(initial_plan_call(), call())
        truncated.choices[0].finish_reason = "length"
        truncated.usage.completion_tokens_details = type("Details", (), {"reasoning_tokens": 12})()
        count = 0

        async def model(messages, tools):
            nonlocal count
            count += 1
            if count == 1 or not recover:
                return truncated
            if count == 2:
                assert any("整批工具均未执行" in (m.get("content") or "") for m in messages)
                provider.call_native.assert_not_awaited()
                return response(initial_plan_call(), call(ident="repaired-read"))
            return final(messages)

        controller._model_call = model
        report = await run(controller, view)
        assert report.status == ("completed" if recover else "failed")
        if recover:
            provider.call_native.assert_awaited_once()
        else:
            provider.call_native.assert_not_awaited()
            assert report.stop_reason == "native_model_output_truncated" and count == 2
        settled = repository.list_recent_events_by_type(view.investigation_id, "mcp.native.model_settled")
        assert settled[0]["data"]["finish_reason"] == "length"
        assert settled[0]["data"]["reasoning_tokens"] == 12
        assert repository.native_model_usage(view.investigation_id)["tokens"] == count * 120
    asyncio.run(scenario())


def test_tiny_budget_fails_without_a_model_or_provider_call(repository, settings):
    async def scenario():
        view, controller, provider = setup(repository, settings)
        controller.settings = replace(controller.settings, model_max_total_tokens=1)
        controller._model_call = AsyncMock()
        report = await run(controller, view)
        assert report.status == "failed" and report.stop_reason == "native_model_token_budget_exhausted"
        controller._model_call.assert_not_awaited()
        provider.call_native.assert_not_awaited()
    asyncio.run(scenario())


def test_report_holdback_blocks_exploration_without_becoming_a_charge(repository, settings):
    view, _, _ = setup(repository, settings)
    params = dict(tokens=60, estimated_cost=0, token_limit=100, cost_limit=None, round_limit=8)
    with pytest.raises(ConflictError, match="token_budget"):
        repository.reserve_native_model_call(view.investigation_id, 1, **params, report_reserve_tokens=41)
    assert repository.native_model_usage(view.investigation_id)["attempts"] == 0
    repository.reserve_native_model_call(view.investigation_id, 1, **params, report_reserve_tokens=40)
    assert repository.native_model_usage(view.investigation_id)["tokens"] == 60
