"""Offline field navigation, literal fidelity and durable recall regressions."""
import asyncio
import hashlib
import json
import time
from dataclasses import replace

import pytest
from mcp import types

from game_test_agent.contracts.models import InvestigationRequest
from game_test_agent.storage import canonical_json
from game_test_agent.workflows.native_context import NativeMemory
from game_test_agent.workflows.native_json import read_json_field
from game_test_agent.workflows.native_recall import ContextRead, input_page
from native_plan_helpers import initial_plan_call
from test_native_enhancements import collect
from test_native_recall import add_input, memory
from test_native_reliability import call, response, run, setup


def field(raw, pointer="", **kwargs):
    raw = raw.encode() if isinstance(raw, str) else raw
    return read_json_field(raw, ContextRead(ref="evidence/e", pointer=pointer, **kwargs),
        evidence_id="e", sha256=hashlib.sha256(raw).hexdigest(), source_revision=1, source={})


def assert_literal(payload, raw):
    start = payload["byte_offset"]
    fragment = payload["result"].encode()
    assert raw[start:start + len(fragment)] == fragment
    assert hashlib.sha256(fragment).hexdigest() == payload["excerpt_sha256"]


@pytest.mark.parametrize("literal,kind", [
    ('"\\u4e2d文\\nquoted\\\""', "string"), ("900719925474099312345", "number"),
    ("-0.0012300e+08", "number"), ("true", "boolean"), ("null", "null"),
])
def test_fields_preserve_exact_original_literals_and_utf8_positions(literal, kind):
    raw = (' {"说明":"前缀", "a/b": {"~k": [ ' + literal + ' ]}} \n').encode()
    payload = field(raw, "/a~1b/~0k/0")
    assert payload["result"] == literal and payload["value_type"] == kind
    assert payload["value_bytes"] == len(literal.encode())
    assert not payload["truncated"] and payload["next_byte_offset"] is None
    assert_literal(payload, raw)


def test_structure_directory_pages_escaped_keys_and_array_children():
    raw = b'{"a/b":{"~":[1,2]},"":null,"last":false}'
    root = field(raw, limit=1)
    assert root["entries"][0]["pointer"] == "/a~1b" and root["next_offset"] == 1
    assert root["entries"][0]["total_items"] == 1
    second = field(raw, offset=root["next_offset"], limit=1)
    assert second["entries"][0]["pointer"] == "/" and second["next_offset"] == 2
    last = field(raw, offset=2)
    assert last["entries"][0]["pointer"] == "/last" and last["next_offset"] is None
    array = field(raw, "/a~1b/~0", limit=1)
    assert array["entries"][0]["pointer"] == "/a~1b/~0/0"
    assert array["entries"][0]["literal_preview"] == "1"
    assert field(raw, "/a~1b/~0", offset=99)["entries"] == []
    assert "not remote-page" in array["scope"]
    for payload in (root, second, last, array):
        assert_literal(payload, raw)


@pytest.mark.parametrize("raw,pointer", [
    ('{"v":1,"v":2}', "/v"), ('{"v":{"x":1,"x":2}}', ""),
    ('{"v":NaN}', "/v"), ('{"v":Infinity}', "/v"), ('{} {}', ""),
    ('[1,2]', "/01"), ('[1,2]', "/-1"), ('[1,2]', "/2"),
    ('{"v":1}', "/v/x"), ('{"v":1}', "/missing"), ('', ""),
])
def test_invalid_or_ambiguous_fields_are_rejected(raw, pointer):
    with pytest.raises((ValueError, LookupError)):
        field(raw, pointer)


@pytest.mark.parametrize("args", [
    {"ref": "input/x", "pointer": ""}, {"ref": "evidence/e", "pointer": "v"},
    {"ref": "evidence/e", "pointer": "/bad~2escape"},
    {"ref": "evidence/e", "pointer": "", "query": "x"},
    {"ref": "evidence/e", "pointer": "", "byte_offset": 1},
    {"ref": "evidence/e", "pointer": "/x" * 33},
])
def test_incompatible_field_requests_are_rejected(args):
    with pytest.raises(ValueError):
        ContextRead(**args)


def test_field_size_hash_and_entire_response_bounds():
    with pytest.raises(ValueError, match="4 MiB"):
        field(b'"' + b"x" * (4 * 1024 * 1024) + b'"')
    with pytest.raises(ValueError, match="hash"):
        read_json_field(b"{}", ContextRead(ref="evidence/e", pointer=""),
            evidence_id="e", sha256="0" * 64, source_revision=1, source={})
    with pytest.raises(ValueError):
        field('{"v":1}', "/v", max_bytes=128)
    with pytest.raises(ValueError):
        field('{"v":1}', "/v", offset=1)
    for budget in (1024, 2048, 4096):
        raw = json.dumps({"v": "中文\n\"" * 1000}, ensure_ascii=False).encode()
        payload = field(raw, "/v", max_bytes=budget)
        assert len(canonical_json(payload).encode()) <= budget
        assert payload["truncated"] and payload["next_byte_offset"] > payload["byte_offset"]
        assert_literal(payload, raw)
        directory = field(json.dumps({f"field-{i}": i for i in range(100)}),
                          max_bytes=budget, limit=20)
        assert len(canonical_json(directory).encode()) <= budget
        assert directory["entries"] and directory["next_offset"]


def test_input_directory_searches_original_text_and_resumes_scan(repository, settings):
    view, _, _ = setup(repository, settings)
    for index in range(103):
        add_input(repository, view, f"input-{index}", "前缀" * 500 + ("TARGET" if index == 101 else "tail"))
    first = input_page(repository, view.investigation_id, ContextRead(ref="inputs", query="target"))
    assert first["items"] == [] and first["next_offset"] == 100
    second = input_page(repository, view.investigation_id,
                        ContextRead(ref="inputs", query="target", offset=first["next_offset"]))
    assert second["items"][0]["command_id"] == "input-101"
    assert "TARGET" not in second["items"][0]["content"] and second["next_offset"] is None


def test_input_reading_progress_does_not_stall_after_three_pages(repository, settings):
    view, _, _ = setup(repository, settings)
    add_input(repository, view, "long", "中文" * 2500)
    state = memory()
    state.sync_inputs(repository, view.investigation_id)
    offset, turns = 0, 0
    while True:
        payload = input_page(repository, view.investigation_id,
            ContextRead(ref="input/long", byte_offset=offset, max_bytes=512))
        state.cover_input(payload)
        assert state.register_progress(True) == "ledger_changed"
        turns += 1
        offset = payload["next_byte_offset"]
        if offset is None:
            break
    assert turns > 3 and not state.finalize
    for _ in range(3):
        state.register_progress(False)
    assert state.finalize


def test_json_recall_is_cached_scoped_and_revision_checked(repository, settings):
    async def scenario():
        view, controller, provider, state, ids = await collect(repository, settings, [{"queue": 7}])
        args = {"ref": "evidence/" + ids[0], "pointer": "/queue"}
        result, _ = await controller._execute_call(view, call("agent_read_context", args, "json-read"),
                                                  {}, state, time.time() + 10)
        assert result["result"] == "7" and result["source_revision"] == 1
        count = repository.budget_usage(view.investigation_id)["tool_reads"]
        reused, _ = await controller._execute_call(view, call("agent_read_context", args, "cached"),
                                                  {}, state, time.time() + 10)
        assert reused == result and repository.budget_usage(view.investigation_id)["tool_reads"] == count
        record = repository.get_evidence(ids[0])
        revised = record.model_copy(update={"source_revision": 2})
        repository._connection.execute("UPDATE evidence_records SET evidence_json=? WHERE evidence_id=?",
                                       (canonical_json(revised), ids[0]))
        repository._connection.commit()
        fresh, _ = await controller._execute_call(view, call("agent_read_context", args, "revision-2"),
                                                 {}, state, time.time() + 10)
        assert fresh["source_revision"] == 2
        assert repository.budget_usage(view.investigation_id)["tool_reads"] == count + 1
        other, _ = repository.create_investigation(InvestigationRequest(
            tenant_id="studio-a", project_id="game-a",
            objective={"objective_id": "other", "description": "Independent investigation"}), "other-json")
        foreign, _ = await controller._execute_call(other,
            call("agent_read_context", args, "foreign-investigation"), {}, memory(), time.time() + 10)
        assert foreign["error_code"] == "EVIDENCE_UNAVAILABLE" and "source" not in foreign
        for index, rejected_args in enumerate([
            {**args, "expected_sha256": "0" * 64}, {**args, "ref": "evidence/foreign"},
            {**args, "pointer": "/missing"},
        ]):
            rejected, _ = await controller._execute_call(view,
                call("agent_read_context", rejected_args, f"reject-{index}"), {}, state, time.time() + 10)
            assert rejected.get("error_code")
        provider.call_native.assert_awaited_once()
    asyncio.run(scenario())


def test_previous_context_cache_identities_survive_optional_field_upgrade(repository, settings):
    async def scenario():
        view, controller, _ = setup(repository, settings)
        add_input(repository, view, "saved", "Original input")
        state = memory()
        state.sync_inputs(repository, view.investigation_id)
        payload, _ = await controller._execute_call(view,
            call("agent_read_context", {"ref": "input/saved"}, "original-read"),
            {}, state, time.time() + 10)
        key = next(iter(state.cache))
        identity = json.loads(key)
        identity[1].pop("pointer")  # Phase-one saved request.
        state.cache[canonical_json(identity)] = state.cache.pop(key)
        state.save(repository, controller.artifacts, view)
        restored = NativeMemory.restore(repository, controller.artifacts, view)
        reused, _ = await controller._execute_call(view,
            call("agent_read_context", {"ref": "input/saved"}, "upgraded-read"),
            {}, restored, time.time() + 10)
        assert reused == payload and repository.budget_usage(view.investigation_id)["tool_reads"] == 1
    asyncio.run(scenario())


def test_json_read_budget_is_checked_before_artifact_access(repository, settings, monkeypatch):
    async def scenario():
        view, controller, _, state, ids = await collect(repository, settings, [{"secret": 7}])
        view = view.model_copy(update={"request": view.request.model_copy(update={
            "budget": view.request.budget.model_copy(update={"max_tool_reads": 1})})})
        def forbidden(*_args):
            pytest.fail("Budget-exhausted read accessed artifact content")
        monkeypatch.setattr(controller.artifacts, "read_bytes", forbidden)
        payload, _ = await controller._execute_call(view,
            call("agent_read_context", {"ref": "evidence/" + ids[0], "pointer": "/secret"}, "over-budget"),
            {}, state, time.time() + 10)
        assert state.finalize and "secret" not in canonical_json(payload)
    asyncio.run(scenario())


@pytest.mark.parametrize("failure,code", [("corrupt", "JSON_FIELD_REJECTED"),
                                           ("timeout", "JSON_FIELD_TIMEOUT")])
def test_json_parser_failures_return_bounded_feedback(repository, settings, monkeypatch, failure, code):
    async def scenario():
        view, controller, _, state, ids = await collect(repository, settings, [{"queue": 7}])
        def failed(*_args, **_kwargs):
            error = ValueError if failure == "corrupt" else TimeoutError
            raise error("PRIVATE_ARTIFACT_CONTENT")
        monkeypatch.setattr("game_test_agent.workflows.native_mcp.read_json_field", failed)
        payload, _ = await controller._execute_call(view,
            call("agent_read_context", {"ref": "evidence/" + ids[0], "pointer": "/queue"}, "failed"),
            {}, state, time.time() + 10)
        assert payload["error_code"] == code and "PRIVATE_ARTIFACT_CONTENT" not in canonical_json(payload)
    asyncio.run(scenario())


def test_field_result_recovers_before_checkpoint_without_repeated_charge(repository, settings):
    async def scenario():
        view, controller, _, state, ids = await collect(repository, settings, [{"queue": 7}])
        state.save(repository, controller.artifacts, view)
        args = {"ref": "evidence/" + ids[0], "pointer": "/queue"}
        payload, _ = await controller._execute_call(view, call("agent_read_context", args, "before-crash"),
                                                   {}, state, time.time() + 10)
        count = repository.budget_usage(view.investigation_id)["tool_reads"]
        async def model(messages, tools):
            notes = json.loads(messages[2]["content"])["collected_evidence_search_hits"]
            assert any(n.get("pointer") == "/queue" and n["original_excerpt"] == "7" for n in notes)
            return response(initial_plan_call())
        controller._model_call = model
        assert await controller.run(view.investigation_id, principal_id="tester",
                                    scopes={"runs:read", "evidence:read"}, max_model_turns=1) is None
        restored = NativeMemory.restore(repository, controller.artifacts, view)
        reused, _ = await controller._execute_call(view, call("agent_read_context", args, "after-crash"),
                                                   {}, restored, time.time() + 10)
        assert reused == payload and repository.budget_usage(view.investigation_id)["tool_reads"] == count
    asyncio.run(scenario())


def test_model_can_navigate_spilled_json_then_publish_verified_field(repository, settings):
    async def scenario():
        view, controller, provider = setup(repository, settings)
        controller.settings = replace(controller.settings, model_context_window_tokens=16384,
                                      model_max_output_tokens=512)
        provider.call_native.return_value = types.CallToolResult(content=[types.TextContent(
            type="text", text=json.dumps({"padding": "x" * 7000, "queue": {"depth": 7}}))])
        turns = 0
        async def model(messages, tools):
            nonlocal turns
            turns += 1
            names = {tool["function"]["name"] for tool in tools}
            if turns == 1:
                assert "agent_read_context" not in names
                return response(initial_plan_call(), call())
            eid = repository.list_current_evidence(view.investigation_id)[0].evidence_id
            if turns in {2, 3}:
                assert "agent_read_context" in names and "agent_read_collected_evidence" not in names
                if turns == 3:
                    directory = json.loads(messages[-1]["content"])
                    assert any(entry["pointer"] == "/queue" for entry in directory["entries"])
                return response(call("agent_read_context", {"ref": "evidence/" + eid,
                    "pointer": "" if turns == 2 else "/queue/depth"}, f"field-{turns}"))
            assert json.loads(messages[-1]["content"])["result"] == "7"
            return response(content=json.dumps({"summary": "Recorded queue depth.", "findings": [{
                "finding_id": "q", "text": "Queue observation.",
                "citations": [{"evidence_id": eid, "quote": "7"}],
                "assertions": [{"evidence_id": eid, "pointer": "/queue/depth", "expected": 7}]}]}))
        controller._model_call = model
        report = await run(controller, view)
        assert report.status == "completed" and turns == 4
        assert next(c for c in report.claims if c.assertions).verification_details.support == "deterministic"
        provider.call_native.assert_awaited_once()
    asyncio.run(scenario())
