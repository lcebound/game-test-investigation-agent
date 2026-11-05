from __future__ import annotations

import asyncio
import base64
import hashlib
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from mcp import types

from game_test_agent.adapters.lyra_mcp import LyraMcpProvider
from game_test_agent.contracts.models import ActionSpec, RunRef
from game_test_agent.mcp_gateway.config import McpProviderConfig
from game_test_agent.mcp_gateway.gateway import StandardToolError


def provider(**updates):
    return LyraMcpProvider(McpProviderConfig(
        provider_id="platform-local", contract="lyra_v1",
        url="http://127.0.0.1:8010/mcp", project_ids=["default"],
        allow_insecure_localhost=True, **updates,
    ))


def session_for(monkeypatch, instance, handler):
    calls = []

    async def call_tool(name, arguments):
        calls.append((name, arguments))
        return types.CallToolResult(content=[], structuredContent=handler(name, arguments))

    @asynccontextmanager
    async def session():
        yield SimpleNamespace(call_tool=call_tool)

    monkeypatch.setattr(instance, "_session", session)
    return calls


def action(**updates):
    values = dict(
        tenant_id="studio-a", project_id="default", investigation_id="inv-one",
        action_id="action-one", idempotency_key="stable-key",
        capability_id="test.run", capability_version="1.0.0", provider_id="platform-local",
        test_ref={"test_id": "case:team-compare"}, parameters={"capture_profile": "none"},
        target_selector={"target_kind": "windows_client"},
        constraints={"timeout_seconds": 180, "root_event_id": "event-one",
                     "causation_id": "inv-one", "investigation_depth": 1},
        expected_observation="test verdict", plan_revision=1,
    )
    values.update(updates)
    return ActionSpec(**values)


def test_lyra_submission_uses_mcp_request_and_preserves_child_provenance(monkeypatch):
    instance = provider()
    ref = {"provider_id": "platform-local", "project_id": "default", "run_id": "run-new"}
    calls = session_for(monkeypatch, instance, lambda *_: {
        "accepted": True, "run_ref": ref, "execution_status": "queued",
        "suggested_poll_interval_ms": 2000,
    })
    operation = asyncio.run(instance.submit(action()))
    assert operation.operation_id == "run-new" and operation.suggested_poll_interval_seconds == 2
    name, arguments = calls[0]
    assert name == "gt_runs_submit" and set(arguments) == {"request"}
    payload = arguments["request"]
    assert payload["idempotency_key"] == "stable-key"
    assert payload["origin"] == "agent" and payload["root_event_id"] == "event-one"
    assert payload["target_selector"]["target_kind"] == "windows_client"
    assert payload["constraints"]["timeout_seconds"] == 180
    assert "schema_version" not in payload


def test_lyra_scope_and_provenance_rejected_before_tool_call(monkeypatch):
    instance = provider()
    calls = session_for(monkeypatch, instance, lambda *_: pytest.fail("must not call MCP"))

    async def run():
        with pytest.raises(PermissionError, match="scope"):
            await instance.submit(action(project_id="other"))
        with pytest.raises(ValueError, match="root event"):
            await instance.submit(action(constraints={}))
        with pytest.raises(PermissionError, match="scope"):
            await instance.query_source_run(RunRef(
                provider_id="other", project_id="default", run_id="run-one"
            ))

    asyncio.run(run())
    assert calls == []


def test_lyra_mcp_artifact_chunks_and_hash_are_checked(monkeypatch):
    instance = provider(max_response_bytes=4096)
    content = bytes(range(256)) * 10

    def handler(name, arguments):
        assert name == "gt_evidence_read" and arguments["project_id"] == "default"
        offset, limit = arguments["offset"], arguments["limit"]
        chunk = content[offset:offset + limit]
        return {
            "artifact_id": arguments["artifact_id"], "encoding": "base64",
            "content": base64.b64encode(chunk).decode(), "content_type": "application/octet-stream",
            "offset": offset, "returned_bytes": len(chunk), "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    calls = session_for(monkeypatch, instance, handler)
    actual, mime = asyncio.run(instance.fetch_artifact("artifact-one"))
    assert actual == content and mime == "application/octet-stream"
    assert len(calls) > 1 and calls[1][1]["offset"] > 0


@pytest.mark.parametrize("change", [
    {"offset": 1}, {"artifact_id": "artifact-other"}, {"sha256": "0" * 64},
    {"returned_bytes": 99}, {"content": "%%%", "encoding": "base64"},
    {"size_bytes": 9999999},
])
def test_lyra_mcp_rejects_corrupt_or_oversized_evidence(monkeypatch, change):
    instance = provider()
    result = {
        "artifact_id": "artifact-one", "offset": 0, "encoding": "utf-8", "content": "test",
        "content_type": "text/plain", "returned_bytes": 4, "size_bytes": 4,
        "sha256": hashlib.sha256(b"test").hexdigest(), **change,
    }
    session_for(monkeypatch, instance, lambda *_: result)
    with pytest.raises(StandardToolError):
        asyncio.run(instance.fetch_artifact("artifact-one"))


def test_lyra_query_maps_run_and_manifest_without_http_fallback(monkeypatch):
    instance = provider()
    ref = {"provider_id": "platform-local", "project_id": "default", "run_id": "run-one"}

    def handler(name, arguments):
        assert arguments == {"run_id": "run-one", "project_id": "default"}
        if name == "gt_runs_get":
            return {"run_ref": ref, "revision": 3, "execution_status": "succeeded",
                    "test_verdict": "passed", "execution_source": "real", "build": {"id": None},
                    "environment": {}, "missing_fields": ["build.hash"]}
        assert name == "gt_evidence_list"
        return {"items": [{"availability": "ready", "evidence_id": "evidence-one",
                           "artifact_id": "artifact-one", "kind": "log", "mime_type": "text/plain",
                           "hash": {"value": hashlib.sha256(b"test").hexdigest()},
                           "producer": "lyra", "producer_version": "1"}]}

    calls = session_for(monkeypatch, instance, handler)
    result = asyncio.run(instance.query_source_run(RunRef(**ref)))
    assert result.schema_version == "2.0" and result.test_verdict == "passed"
    assert result.build_ref is None and result.missing_fields == ["build.hash"]
    assert result.evidence_manifest[0].artifact_ref == "artifact-one"
    assert [name for name, _ in calls] == ["gt_runs_get", "gt_evidence_list"]
