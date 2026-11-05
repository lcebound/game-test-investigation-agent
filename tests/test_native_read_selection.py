"""Automatic action selection follows the same live read state as the worker."""
import asyncio
import json
import time
from unittest.mock import Mock

import pytest
from mcp import types

from game_test_agent.memory.recall import RecallService
from game_test_agent.storage.feedback import FeedbackStore
from game_test_agent.workflows.native_context import NativeMemory
from game_test_agent.workflows.native_mcp import CollectedEvidenceRead
from game_test_agent.workflows.native_reasoning import select_candidate
from game_test_agent.workflows.native_read_state import refresh_search_coverage
from native_plan_helpers import initial_plan_call
from test_cross_task_recall import bind, source, target
from test_native_enhancements import collect
from test_native_recall import add_input, memory
from test_native_reliability import call, final, response, run, setup
from test_task_feedback import feedback, view as feedback_view


def choose(repository, view, state, name, arguments, **kwargs):
    result, progress = select_candidate(repository, view, state, {}, None,
        json.dumps({"candidates": [{"candidate_id": "next", "tool_name": name,
            "arguments": arguments, "question": "Resolve the remaining evidence gap"}]}),
        CollectedEvidenceRead, **kwargs)
    assert not progress
    return result


async def execute(controller, view, state, name, arguments, ident="read"):
    return await controller._execute_call(view, call(name, arguments, ident), {}, state, time.time() + 20)


@pytest.mark.parametrize("kind", ["directory", "report", "evidence_directory", "original"])
def test_history_candidates_follow_pins_without_reading_originals(repository, settings, monkeypatch, kind):
    old, _, digest, ids = source(repository, settings)
    own = target(repository)
    service = RecallService(repository)
    bind(service, own, old, digest)
    ref = {"directory": "history", "report": f"history/{old.investigation_id}/report",
           "evidence_directory": f"history/{old.investigation_id}/evidence",
           "original": f"history/{old.investigation_id}/evidence/{ids[0]}"}[kind]
    # Metadata selection must not spend read budget or open a source artifact.
    monkeypatch.setattr("game_test_agent.storage.LocalArtifactStore.read_bytes", Mock(side_effect=AssertionError("no artifact read")))
    before = repository.budget_usage(own.investigation_id)
    result = choose(repository, own, memory(), "agent_read_context", {"ref": ref})
    assert result["selected_candidate"]["arguments"]["ref"] == ref
    assert repository.budget_usage(own.investigation_id) == before
    assert not repository.list_current_evidence(own.investigation_id)
    assert not repository.list_recent_events_by_type(own.investigation_id, "evidence.excerpt_read")


@pytest.mark.parametrize("failure", ["unattached", "uncited", "revoked", "wrong_hash", "foreign"])
def test_history_candidates_cannot_grant_or_reuse_invalid_access(repository, settings, failure):
    old, _, digest, ids = source(repository, settings)
    own = target(repository)
    service = RecallService(repository)
    if failure != "unattached":
        bind(service, own, old, digest)
    if failure == "revoked":
        service.revoke(own, old.investigation_id)
    if failure == "foreign":
        own = repository.create_investigation(own.request.model_copy(update={"project_id": "other"}), "foreign")[0]
    ref = f"history/{old.investigation_id}/evidence/{ids[1] if failure == 'uncited' else ids[0]}"
    args = {"ref": ref, **({"expected_sha256": "0" * 64} if failure == "wrong_hash" else {})}
    assert choose(repository, own, memory(), "agent_read_context", args)["selected_candidate"] is None


def test_feedback_candidates_require_sealed_own_delivery_and_matching_hash(repository):
    own = feedback_view(repository)
    other = target(repository)
    store = FeedbackStore(repository)
    store.accept(feedback())
    cursor = repository._connection.execute("SELECT cursor FROM task_feedback_deliveries WHERE investigation_id=?", (own.investigation_id,)).fetchone()[0]
    args = {"ref": f"feedback/{cursor}"}
    assert choose(repository, own, memory(), "agent_read_context", args)["selected_candidate"] is None
    store.prepare_batches()
    assert choose(repository, own, memory(), "agent_read_context", args)["selected_candidate"]
    assert choose(repository, own, memory(), "agent_read_context", {"ref": "feedbacks"})["selected_candidate"]
    assert choose(repository, other, memory(), "agent_read_context", args)["selected_candidate"] is None
    assert choose(repository, own, memory(), "agent_read_context", {**args, "expected_sha256": "0" * 64})["selected_candidate"] is None
    assert not repository.list_current_evidence(own.investigation_id)


@pytest.mark.parametrize("name,args", [("agent_read_context", {"ref": "history", "query": "bad"}),
                                      ("agent_read_context", {"ref": "feedbacks", "byte_offset": 1}),
                                      ("agent_find_history", {"query": ""})])
def test_invalid_discovery_and_directory_arguments_are_not_selected(repository, name, args):
    own = target(repository)
    assert choose(repository, own, memory(), name, args)["selected_candidate"] is None


@pytest.mark.parametrize("name", ["agent_read_collected_evidence", "agent_read_context"])
def test_empty_query_cannot_bypass_cached_action_assessment(repository, settings, name):
    async def scenario():
        own, controller, _, state, ids = await collect(repository, settings, [{"value": 7}])
        args = {"evidence_id": ids[0]} if name == "agent_read_collected_evidence" else {"ref": "evidence/" + ids[0]}
        await execute(controller, own, state, name, args)
        result = choose(repository, own, state, name, {**args, "query": ""})
        assert result["selected_candidate"] is None
        assert result["assessments"][0]["reasons"] == ["already_available_in_working_state"]
        before = repository.budget_usage(own.investigation_id)["tool_reads"]
        await execute(controller, own, state, name, {**args, "query": ""}, "same-read")
        assert repository.budget_usage(own.investigation_id)["tool_reads"] == before
    asyncio.run(scenario())


@pytest.mark.parametrize("legacy", [False, True])
def test_negative_search_is_invalidated_by_new_evidence_and_survives_checkpoint(repository, settings, legacy):
    async def scenario():
        own, controller, provider, state, _ = await collect(repository, settings, [{"log": "normal"}])
        args = {"query": "RECOVERY_FAILED"}
        payload, _ = await execute(controller, own, state, "agent_search_evidence", args)
        assert payload["hits"] == []
        assert choose(repository, own, state, "agent_search_evidence", args)["selected_candidate"] is None
        if legacy:
            for memo in state.cache.values():
                memo.pop("state_fingerprint", None)
        state.save(repository, controller.artifacts, own)
        restored = NativeMemory.restore(repository, controller.artifacts, own)
        provider.call_native.return_value = types.CallToolResult(content=[types.TextContent(type="text", text='{"log":"RECOVERY_FAILED"}')])
        tool = (await provider.native_tools())[0]
        await controller._execute_call(own, call(arguments={"id": "new"}, ident="new-evidence"),
            {"inspect_incident": (provider, tool)}, restored, time.time() + 20)
        refresh_search_coverage(repository, own, restored)
        assert not restored.search_coverage
        assert choose(repository, own, restored, "agent_search_evidence", args)["selected_candidate"]
        payload, _ = await execute(controller, own, restored, "agent_search_evidence", args, "search-again")
        assert len(payload["hits"]) == 1 and "RECOVERY_FAILED" in payload["hits"][0]["result"]
    asyncio.run(scenario())


def test_journal_recovery_does_not_restore_revoked_history_reads(repository, settings):
    async def scenario():
        old, _, digest, ids = source(repository, settings)
        own, controller, _ = setup(repository, settings, rounds=8)
        service = RecallService(repository)
        bind(service, own, old, digest)
        state = memory()
        state.sync_inputs(repository, own.investigation_id)
        await execute(controller, own, state, "agent_read_context",
                      {"ref": f"history/{old.investigation_id}/evidence/{ids[0]}"})
        assert state.history_read_ranges
        state.save(repository, controller.artifacts, own)
        service.revoke(own, old.investigation_id)

        async def model(messages, tools):
            assert "HISTORY_RAW_SECRET" not in json.dumps(messages)
            return response(initial_plan_call())

        controller._model_call = model
        assert await controller.run(own.investigation_id, principal_id="tester",
            scopes={"runs:read", "evidence:read"}, max_model_turns=1) is None
        restored = NativeMemory.restore(repository, controller.artifacts, own)
        assert not restored.history_read_ranges
        assert not any(m["payload"].get("historical") for m in restored.cache.values())
    asyncio.run(scenario())


def test_history_discovery_is_fresh_even_when_previous_response_was_empty(repository, settings):
    async def scenario():
        own, controller, _ = setup(repository, settings)
        state = memory()
        args = {"query": "旧任务"}
        payload, _ = await execute(controller, own, state, "agent_find_history", args)
        assert not payload["items"]
        source(repository, settings)
        assert choose(repository, own, state, "agent_find_history", args)["selected_candidate"]
        payload, _ = await execute(controller, own, state, "agent_find_history", args, "fresh-discovery")
        assert len(payload["items"]) == 1
    asyncio.run(scenario())


def test_input_directory_refreshes_against_the_original_ledger(repository, settings):
    async def scenario():
        own, controller, _ = setup(repository, settings)
        state = memory()
        args = {"ref": "inputs"}
        await execute(controller, own, state, "agent_read_context", args)
        add_input(repository, own, "new", "Independent recovery validation is required")
        # Deliberately do not call sync_inputs: both consumers consult the ledger.
        assert choose(repository, own, state, "agent_read_context", args)["selected_candidate"]
        result, _ = await execute(controller, own, state, "agent_read_context", args, "new-directory")
        assert result["total_items"] == 1
    asyncio.run(scenario())


def test_revocation_after_selection_is_enforced_again_at_execution(repository, settings):
    async def scenario():
        old, _, digest, ids = source(repository, settings)
        own, controller, _ = setup(repository, settings)
        service = RecallService(repository)
        bind(service, own, old, digest)
        state = memory()
        args = {"ref": f"history/{old.investigation_id}/evidence/{ids[0]}"}
        assert choose(repository, own, state, "agent_read_context", args)["selected_candidate"]
        await execute(controller, own, state, "agent_read_context", args)
        service.revoke(own, old.investigation_id)
        assert choose(repository, own, state, "agent_read_context", args)["selected_candidate"] is None
        result, _ = await execute(controller, own, state, "agent_read_context", args, "after-revoke")
        assert "error_code" in result and "result" in result
        assert "历史原文" not in result["result"]
    asyncio.run(scenario())


def test_candidate_cannot_select_a_tool_hidden_in_the_current_turn(repository):
    own = target(repository)
    result = choose(repository, own, memory(), "agent_find_history", {"query": "旧任务"},
                    available_tool_names={"agent_read_context"})
    assert result["selected_candidate"] is None
    assert "tool_not_offered_this_turn" in result["assessments"][0]["reasons"]


def test_registered_diagnostic_can_be_selected_without_running_it(repository, settings):
    async def scenario():
        own, controller, _, state, ids = await collect(repository, settings, [{"frames": [{"frame_ms": x} for x in [10, 20, 30]]}])
        args = {"recipe": "frame_time", "evidence_id": ids[0], "data_pointer": "/frames"}
        before = repository.budget_usage(own.investigation_id)
        assert choose(repository, own, state, "agent_analyze_evidence", args, diagnostics=controller.diagnostics)["selected_candidate"]
        assert repository.budget_usage(own.investigation_id) == before
        await execute(controller, own, state, "agent_analyze_evidence", args)
        result = choose(repository, own, state, "agent_analyze_evidence", args, diagnostics=controller.diagnostics)
        assert result["selected_candidate"] is None
        assert result["assessments"][0]["reasons"] == ["already_available_in_working_state"]
    asyncio.run(scenario())


def test_native_worker_automatically_selects_and_reads_attached_history(repository, settings):
    async def scenario():
        old, _, digest, ids = source(repository, settings)
        own, controller, provider = setup(repository, settings, rounds=8)
        bind(RecallService(repository), own, old, digest)
        args = {"ref": f"history/{old.investigation_id}/evidence/{ids[0]}"}
        turns = 0

        async def model(messages, tools):
            nonlocal turns
            turns += 1
            if turns == 1:
                plan = initial_plan_call()
                body = json.loads(plan.function.arguments)
                body["steps"][0]["remaining_gaps"] = ["Compare historical recovery"]
                return response(call("agent_update_plan", body, "initial-plan"), call())
            if turns == 2:
                assert "agent_choose_next_action" in {t["function"]["name"] for t in tools}
                return response(call("agent_choose_next_action", {"candidates": [{"candidate_id": "history",
                    "tool_name": "agent_read_context", "arguments": args, "question": "Compare recovery",
                    "fills_gaps": ["Compare historical recovery"]}]}, "choose-history"))
            if turns == 3:
                selected = json.loads(messages[-1]["content"])["selected_candidate"]
                assert selected["candidate_id"] == "history"
                return response(call(selected["tool_name"], selected["arguments"], "read-history"))
            assert any("historical_evidence" in (m.get("content") or "") for m in messages)
            return final(messages)

        controller._model_call = model
        report = await run(controller, own)
        assert report.stop_reason == "native_mcp_investigation_complete" and turns == 4
        provider.call_native.assert_awaited_once()
        assert all(e.evidence_id not in ids for e in repository.list_current_evidence(own.investigation_id))
    asyncio.run(scenario())
