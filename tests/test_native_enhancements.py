"""Offline regressions for native source, reasoning and failure boundaries."""
import asyncio
import json
import subprocess
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from mcp import types
from openai import AuthenticationError, RateLimitError

from game_test_agent.contracts.models import Hypothesis
from game_test_agent.mcp_gateway.config import McpProviderConfig
from game_test_agent.storage import ConflictError
from game_test_agent.workflows.native_context import NativeMemory
from game_test_agent.workflows.native_evidence import (
    EvidenceSearch, NativeFinding, extract_source, finding_errors, search_evidence,
    source_index, text_consistency_errors,
)
from game_test_agent.workflows.native_mcp import NativeDiagnosis
from game_test_agent.workflows.native_reasoning import select_candidate, update_hypotheses
from native_plan_helpers import initial_plan_call
from test_native_reliability import call, final, response, run, setup


FIELDS = {"run_id": "/run/id", "build_id": "/build", "environment": "/device",
          "timestamp": "/time", "phase": "/phase"}


async def collect(repository, settings, documents):
    view, controller, provider = setup(repository, settings, rounds=12)
    provider.config.native_source_fields = {"inspect_incident": FIELDS}
    provider.config.max_response_bytes = max(20000, max(len(json.dumps(d, ensure_ascii=False).encode()) for d in documents))
    memory = NativeMemory(messages=[{"role": "system", "content": ""},
                                   {"role": "user", "content": ""},
                                   {"role": "user", "content": "{}"}])
    tool = (await provider.native_tools())[0]
    ids = []
    for index, document in enumerate(documents):
        provider.call_native.return_value = types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(document, ensure_ascii=False))])
        payload, _ = await controller._execute_call(view, call(arguments={"id": str(index)}, ident=f"seed-{index}"),
            {"inspect_incident": (provider, tool)}, memory,
            datetime.now(timezone.utc).timestamp() + 60)
        ids.append(payload["evidence_id"])
    return view, controller, provider, memory, ids


def document(run_id="run-1", build="b1", device="d1", phase="injection", **extra):
    return {"run": {"id": run_id}, "build": build, "device": device, "phase": phase,
            "time": "2026-10-29T10:00:00+08:00", "log": "失败 crash: measured 47 ms", **extra}


@pytest.mark.parametrize("bad", [
    {"tool": {"invented": "/run"}}, {"tool": {"run_id": "run.id"}},
    {"tool": {"run_id": "/bad~2escape"}},
])
def test_source_mapping_requires_explicit_valid_json_pointers(bad):
    with pytest.raises(ValueError):
        McpProviderConfig(provider_id="lab", url="https://lab.test/mcp", project_ids=["game-a"],
                          native_source_fields=bad)


def test_source_extraction_never_guesses_or_trusts_invalid_values():
    assert not extract_source(json.dumps(document()), {})["values"]
    assert not extract_source("run-1 crashed", FIELDS)["values"]
    values = extract_source(json.dumps(document(time="2026-10-29T10:00:00")), FIELDS)["values"]
    assert values["run_id"] == "run-1" and "timestamp" not in values
    assert extract_source('{"a/b":[{"~id":"r"}]}', {"run_id": "/a~1b/0/~0id"})["values"] == {"run_id": "r"}


@pytest.mark.parametrize("text", ["每帧阻塞47毫秒", "Measured 47 s", "Measured 99 ms",
                                  "root cause confirmed: measured 47 ms"])
def test_literal_quote_does_not_authorize_extrapolated_measurements_or_frequency(text):
    assert text_consistency_errors(text, ["one sample measured 47 ms"])


def test_numeric_unit_conversion_is_consistent():
    assert not text_consistency_errors("测得0.047秒", ["measured 47 ms"])


@pytest.mark.parametrize("mismatch", ["run_id", "build_id", "environment", "phase"])
def test_finding_scope_is_checked_against_every_source(repository, settings, mismatch):
    async def scenario():
        view, controller, _, _, ids = await collect(repository, settings, [document()])
        finding = NativeFinding(finding_id="f1", text="测得47毫秒", scope="single_run",
                                citations=[{"evidence_id": ids[0], "quote": "47 ms"}],
                                **{mismatch: "unrelated"})
        errors = controller._citation_errors(view, NativeDiagnosis(summary="Measured 47 ms", findings=[finding]))
        assert any(mismatch in e for e in errors)
    asyncio.run(scenario())


def test_cross_run_comparison_requires_matching_known_build_and_environment(repository, settings):
    async def scenario():
        view, _, _, _, ids = await collect(repository, settings, [document(), document("run-2", build="b2")])
        finding = NativeFinding(finding_id="f", text="Measured 47 ms", scope="cross_run",
            citations=[{"evidence_id": eid, "quote": "47 ms"} for eid in ids])
        assert any("build_id" in e for e in finding_errors(finding, source_index(repository, view.investigation_id), view))
        sources = source_index(repository, view.investigation_id)
        sources[ids[1]]["values"]["build_id"] = "b1"
        assert not finding_errors(finding, sources, view)
        finding.scope = "single_run"
        assert finding_errors(finding, sources, view)
    asyncio.run(scenario())


def test_source_aware_findings_publish_with_individual_citations(repository, settings):
    async def scenario():
        view, controller, provider = setup(repository, settings, rounds=4)
        provider.config.native_source_fields = {"inspect_incident": FIELDS}
        provider.call_native.return_value = types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(document()))])
        turns = 0
        async def model(messages, tools):
            nonlocal turns
            turns += 1
            if turns == 1:
                return response(initial_plan_call(), call())
            eid = repository.list_current_evidence(view.investigation_id)[0].evidence_id
            return response(content=json.dumps({"summary": "Measured 47 ms", "findings": [{
                "finding_id": "measurement", "text": "测得47毫秒", "scope": "single_run",
                "run_id": "run-1", "build_id": "b1", "environment": "d1", "phase": "injection",
                "citations": [{"evidence_id": eid, "quote": "47 ms"}]}]}))
        controller._model_call = model
        report = await run(controller, view)
        assert report.stop_reason == "native_mcp_investigation_complete"
        finding = next(c for c in report.claims if c.claim_id == "finding-measurement")
        assert finding.scope == "run:run-1" and finding.supporting_excerpt == "47 ms"
        assert "测得47毫秒" in report.summary
        assert report.conclusion_level == "observation"
    asyncio.run(scenario())


def test_cross_file_regex_search_keeps_original_unicode_offsets_and_source_filters(repository, settings):
    async def scenario():
        view, controller, _, _, ids = await collect(repository, settings, [document(), document("run-2", phase="recovery")])
        hits = search_evidence(repository, controller.artifacts, view,
                               EvidenceSearch(query="失败|crash", regex=True, run_id="run-2", phase="recovery"))
        assert hits["hits"] and all(h["evidence_id"] == ids[1] for h in hits["hits"])
        raw = controller.artifacts.read_bytes(repository.list_current_evidence(view.investigation_id)[1].artifact_ref)
        for hit in hits["hits"]:
            record = next(e for e in repository.list_current_evidence(view.investigation_id) if e.evidence_id == hit["evidence_id"])
            raw = controller.artifacts.read_bytes(record.artifact_ref)
            assert raw[hit["byte_offset"]:hit["byte_offset"] + len(hit["result"].encode())].decode() == hit["result"]
        assert not search_evidence(repository, controller.artifacts, view,
            EvidenceSearch(query="crash", time_from="2026-10-29T03:00:00Z"))["hits"]
    asyncio.run(scenario())


def test_search_cursor_reaches_every_match_without_repeating(repository, settings):
    async def scenario():
        view, controller, _, _, _ = await collect(repository, settings, [document(log="crash A crash B"), document("run-2", log="crash C")])
        cursor, count = {}, 0
        for _ in range(6):
            result = search_evidence(repository, controller.artifacts, view, EvidenceSearch(query="crash", max_hits=1, **cursor))
            count += len(result["hits"])
            if result["next_cursor"] is None:
                break
            cursor = result["next_cursor"]
        assert count == 3 and result["complete"]
    asyncio.run(scenario())


def test_search_rejects_cross_investigation_ids_and_kills_slow_patterns(repository, settings, monkeypatch):
    async def scenario():
        view, controller, _, _, _ = await collect(repository, settings, [document()])
        with pytest.raises(ValueError, match="readable"):
            search_evidence(repository, controller.artifacts, view, EvidenceSearch(query="crash", evidence_ids=["foreign"]))
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("search", 2)))
        with pytest.raises(ValueError, match="time limit"):
            search_evidence(repository, controller.artifacts, view, EvidenceSearch(query="(x+)+$", regex=True))
    asyncio.run(scenario())


def hypothesis(view, eid, revision=1, **changes):
    value = dict(hypothesis_id="h1", investigation_id=view.investigation_id, revision=revision,
                 statement="Blocking may cause the observed delay", scope="one source run",
                 supporting_evidence_ids=[eid], falsifiable_prediction="Recovery samples return below baseline",
                 missing_evidence=["independent recovery"], status="supported")
    value.update(changes)
    return Hypothesis(**value)


def test_native_hypotheses_reuse_ledger_with_atomic_revision_and_reference_checks(repository, settings):
    async def scenario():
        view, _, _, _, ids = await collect(repository, settings, [document()])
        h = hypothesis(view, ids[0])
        assert repository.save_native_hypotheses(view.investigation_id, [h], "initial")
        assert not repository.save_native_hypotheses(view.investigation_id, [h], "replayed")
        with pytest.raises(ConflictError):
            repository.save_native_hypotheses(view.investigation_id, [h.model_copy(update={"statement": "changed"})], "bad")
        valid = hypothesis(view, ids[0], 2, missing_evidence=[])
        invalid = hypothesis(view, "foreign", hypothesis_id="h2")
        with pytest.raises(ValueError):
            repository.save_native_hypotheses(view.investigation_id, [valid, invalid], "must rollback")
        assert repository.list_hypotheses(view.investigation_id) == [h]
        with pytest.raises(ValueError, match="confirm"):
            repository.save_native_hypotheses(view.investigation_id, [hypothesis(view, ids[0], 2, status="confirmed")], "bad")
        assert repository.list_evidence_relations(view.investigation_id)[0].relation_type == "supports"
    asyncio.run(scenario())


def test_candidate_selection_validates_original_schema_and_uses_competing_predictions(repository, settings):
    async def scenario():
        view, controller, provider, memory, ids = await collect(repository, settings, [document()])
        h1 = hypothesis(view, ids[0])
        h2 = hypothesis(view, ids[0], hypothesis_id="h2", statement="Environment may cause the delay")
        repository.save_native_hypotheses(view.investigation_id, [h1, h2], "compare")
        tool = (await provider.native_tools())[0]
        local = {"candidate_id": "read", "tool_name": "agent_read_collected_evidence",
                 "arguments": {"evidence_id": ids[0], "query": "recovery"},
                 "question": "Did recovery occur?", "hypothesis_ids": ["h1", "h2"],
                 "fills_gaps": ["independent recovery"], "predictions": {
                     "h1": [{"observation_key": "recovered", "comparator": "eq", "value": "yes"}],
                     "h2": [{"observation_key": "recovered", "comparator": "eq", "value": "no"}]}}
        remote = {**local, "candidate_id": "remote", "tool_name": "inspect_incident", "arguments": {"id": "new"}}
        invalid = {**remote, "candidate_id": "bad", "arguments": {"invented": True}}
        from game_test_agent.workflows.native_mcp import CollectedEvidenceRead
        result, progress = select_candidate(repository, view, memory, {"inspect_incident": (provider, tool)},
            None, json.dumps({"candidates": [remote, local, invalid]}), CollectedEvidenceRead)
        assert result["selected_candidate"]["candidate_id"] == "read" and not progress
        assert result["assessments"][1]["discriminated_pairs"] == 1
        assert not result["assessments"][2]["executable"]
        assert provider.call_native.await_count == 1  # Only the seeded evidence was read.
    asyncio.run(scenario())


def test_hypothesis_tool_survives_checkpoint_and_enters_working_context(repository, settings):
    async def scenario():
        view, controller, _, memory, ids = await collect(repository, settings, [document()])
        body = hypothesis(view, ids[0]).model_dump(mode="json")
        body.pop("investigation_id")
        body.pop("observable_predictions")
        update_hypotheses(repository, view, memory, json.dumps({"reason": "observed delay", "hypotheses": [body]}))
        memory.save(repository, controller.artifacts, view)
        restored = NativeMemory.restore(repository, controller.artifacts, view)
        restored.refresh_working_message()
        assert json.loads(restored.messages[2]["content"])["hypotheses"][0]["hypothesis_id"] == "h1"
    asyncio.run(scenario())


def test_changing_irrelevant_bytes_cannot_keep_investigation_alive_forever():
    memory = NativeMemory()
    actions = [memory.register_progress(True) for _ in range(10)]
    assert "replan" in actions and memory.finalize
    assert actions.count("new_read_range") == 3


def test_provider_discovery_failure_preserves_other_available_sources(repository, settings):
    async def scenario():
        view, controller, provider = setup(repository, settings, rounds=3)
        broken = SimpleNamespace(config=SimpleNamespace(project_ids=["game-a"]),
                                 native_tools=AsyncMock(side_effect=TimeoutError()))
        controller.providers.providers = {"broken": broken, "lab": provider}
        count = 0
        async def model(messages, tools):
            nonlocal count
            count += 1
            return response(initial_plan_call(), call()) if count == 1 else final(messages)
        controller._model_call = model
        report = await run(controller, view)
        assert report.stop_reason == "native_mcp_investigation_complete"
        assert any("broken" in gap for gap in report.unresolved_questions)
    asyncio.run(scenario())


def test_token_budget_preflight_prevents_unaccounted_calls(repository, settings):
    async def scenario():
        view, controller, _ = setup(repository, settings)
        controller.settings = replace(controller.settings, model_max_total_tokens=1)
        controller._model_call = AsyncMock()
        report = await run(controller, view)
        assert report.stop_reason == "native_model_token_budget_exhausted"
        controller._model_call.assert_not_awaited()
    asyncio.run(scenario())


def test_interrupted_model_attempt_keeps_reservation_and_settlement_is_durable(repository, settings):
    async def scenario():
        view, controller, _ = setup(repository, settings)
        controller._model_call = AsyncMock(side_effect=asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await run(controller, view)
        usage = repository.native_model_usage(view.investigation_id)
        assert usage["tokens"] > 0 and usage["attempts"] == 1
        repository.record_investigation_event(view.investigation_id, "mcp.native.model_settled",
                                             {"turn": 1, "tokens": 12, "estimated_cost": 0.2})
        assert repository.native_model_usage(view.investigation_id)["tokens"] == 12
        with pytest.raises(ConflictError, match="token_budget"):
            repository.reserve_native_model_call(view.investigation_id, 2, tokens=10, estimated_cost=0,
                token_limit=20, cost_limit=None, round_limit=10)
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["auth", "rate_limit"])
def test_model_failures_are_classified_without_retrying_permanent_errors(repository, settings, kind):
    async def scenario():
        view, controller, _ = setup(repository, settings)
        status = 401 if kind == "auth" else 429
        http_response = httpx.Response(status, request=httpx.Request("POST", "https://model.test/"))
        error_type = AuthenticationError if kind == "auth" else RateLimitError
        controller._model_call = AsyncMock(side_effect=error_type("secret must not leak", response=http_response, body={}))
        await run(controller, view)
        assert controller._model_call.await_count == (1 if kind == "auth" else 2)
        events = repository.list_recent_events_by_type(view.investigation_id, "mcp.native.model_error")
        assert events[-1]["data"]["error_code"] == ("MODEL_AUTHORIZATION_FAILED" if kind == "auth" else "MODEL_RATE_LIMITED")
        assert "secret" not in json.dumps(events)
    asyncio.run(scenario())


def test_read_result_survives_crash_before_conversation_checkpoint(repository, settings, monkeypatch):
    async def scenario():
        view, controller, provider = setup(repository, settings, rounds=4)
        original = NativeMemory.save
        def crash_after_result(memory, repository, artifacts, view):
            if repository.list_recent_events_by_type(view.investigation_id, "mcp.native.call_result"):
                raise asyncio.CancelledError()
            return original(memory, repository, artifacts, view)
        monkeypatch.setattr(NativeMemory, "save", crash_after_result)
        controller._model_call = AsyncMock(return_value=response(initial_plan_call(), call()))
        with pytest.raises(asyncio.CancelledError):
            await run(controller, view)
        monkeypatch.setattr(NativeMemory, "save", original)
        count = 0
        async def model(messages, tools):
            nonlocal count
            count += 1
            return response(call(ident="after-crash")) if count == 1 else final(messages)
        controller._model_call = model
        report = await run(controller, view)
        assert report.stop_reason == "native_mcp_investigation_complete"
        provider.call_native.assert_awaited_once()
    asyncio.run(scenario())


def test_reused_call_id_cannot_dispatch_different_arguments_without_budget(repository, settings):
    async def scenario():
        view, controller, provider, memory, _ = await collect(repository, settings, [document()])
        tool = (await provider.native_tools())[0]
        payload, progress = await controller._execute_call(view,
            call(arguments={"id": "another"}, ident="seed-0"),
            {"inspect_incident": (provider, tool)}, memory,
            datetime.now(timezone.utc).timestamp() + 60)
        assert payload["error_code"] == "CALL_ID_REUSED" and not progress
        provider.call_native.assert_awaited_once()
        assert repository.budget_usage(view.investigation_id)["tool_reads"] == 1
    asyncio.run(scenario())


def test_cost_budget_and_missing_prices_are_checked_before_calling_model(repository, settings):
    async def scenario():
        view, controller, _ = setup(repository, settings)
        controller.settings = replace(controller.settings, model_max_estimated_cost=0,
            model_cost_per_input_token=0.001, model_cost_per_output_token=0.002)
        controller._model_call = AsyncMock()
        report = await run(controller, view)
        assert report.stop_reason == "native_model_cost_budget_exhausted"
        controller._model_call.assert_not_awaited()
        with pytest.raises(ValueError, match="prices"):
            replace(settings, model_max_estimated_cost=1).validate()
        with pytest.raises(ValueError, match="finite"):
            replace(settings, model_cost_per_input_token=float("nan")).validate()
    asyncio.run(scenario())


def test_malformed_model_envelopes_are_bounded_and_charged(repository, settings):
    async def scenario():
        view, controller, provider = setup(repository, settings)
        controller._model_call = AsyncMock(return_value=SimpleNamespace(choices=[], usage=None))
        report = await run(controller, view)
        assert report.stop_reason == "native_model_unavailable"
        assert controller._model_call.await_count == 2
        provider.call_native.assert_not_awaited()
        usage = repository.native_model_usage(view.investigation_id)
        assert usage["attempts"] == 2 and usage["tokens"] > 0
        assert all(e["data"]["error_code"] == "MODEL_INVALID_RESPONSE"
            for e in repository.list_recent_events_by_type(view.investigation_id, "mcp.native.model_error"))
    asyncio.run(scenario())


def test_literal_search_finds_matches_crossing_scan_chunk_boundaries(repository, settings):
    async def scenario():
        # Place the marker across the actual serialized byte boundary.
        prefix = json.dumps(document(log="")).split('"log": "')[0] + '"log": "'
        offset = 2 * 1024 * 1024 - len(prefix.encode()) - 8
        doc = document(log="x" * offset + "CROSS_BOUNDARY_MARKER")
        view, controller, _, _, _ = await collect(repository, settings, [doc])
        first = search_evidence(repository, controller.artifacts, view, EvidenceSearch(query="CROSS_BOUNDARY_MARKER"))
        assert not first["hits"] and not first["complete"]
        second = search_evidence(repository, controller.artifacts, view,
            EvidenceSearch(query="CROSS_BOUNDARY_MARKER", **first["next_cursor"]))
        assert second["hits"] and second["complete"]
    asyncio.run(scenario())
