"""Protocol fixture matching the Lyra v1 MCP tool signatures; all runs are simulated."""
import base64
import hashlib
import sys
from typing import Any

from mcp.server.mcpserver import MCPServer

server = MCPServer("lyra-protocol-fixture")
content = b"simulated fixture log: test passed"
ref = {"provider_id": "platform-local", "project_id": "default", "run_id": "run-fixture"}
submitted: dict[str, Any] = {}


@server.tool()
async def gt_capabilities_list(project_id: str) -> dict[str, Any]:
    return {"provider_id": "platform-local", "provider_revision": "fixture-v1", "capabilities": [{
        "capability_id": "test.run", "capability_version": "1.0.0", "project_ids": [project_id],
        "availability": "ready", "target_kinds": ["windows_client"],
        "required_scopes": ["runs:submit"],
    }]}


@server.tool(structured_output=True)
async def gt_tests_search(
    project_id: str, query: str = "", page: int = 1, page_size: int = 50
) -> dict[str, Any]:
    return {"items": [{"test_ref": {"test_id": "case:fixture"}, "name": "Fixture case",
                       "platform_only": "must not reach workbench"}],
            "total": 1, "page": page, "page_size": page_size}


@server.tool()
async def gt_runs_submit(request: dict[str, Any]) -> dict[str, Any]:
    submitted.update(request)
    return {"accepted": True, "run_ref": ref, "execution_status": "queued"}


@server.tool(structured_output=True)
async def gt_runs_find_by_action(action_id: str, project_id: str) -> dict[str, Any]:
    if submitted.get("action_id") != action_id:
        return {"error": {"code": "NOT_FOUND", "message": "No such action"}}
    return {"run_ref": ref, "execution_status": "succeeded"}


@server.tool(structured_output=True)
async def gt_runs_get(run_id: str, project_id: str) -> dict[str, Any]:
    return {"run_ref": ref, "revision": 1, "execution_status": "succeeded",
            "test_verdict": "passed", "execution_source": "simulated", "environment": {},
            "missing_fields": ["real_execution"]}


@server.tool(structured_output=True)
async def gt_evidence_list(run_id: str, project_id: str) -> dict[str, Any]:
    return {"items": [{"availability": "ready", "evidence_id": "evidence-fixture",
                       "artifact_id": "artifact-fixture", "kind": "log", "mime_type": "text/plain",
                       "hash": {"value": hashlib.sha256(content).hexdigest()},
                       "producer": "lyra-fixture", "producer_version": "1"}]}


@server.tool(structured_output=True)
async def gt_evidence_read(
    artifact_id: str, project_id: str, offset: int = 0, limit: int = 1024 * 1024
) -> dict[str, Any]:
    chunk = content[offset:offset + limit]
    return {"artifact_id": artifact_id, "content_type": "text/plain", "encoding": "base64",
            "content": base64.b64encode(chunk).decode(), "offset": offset,
            "returned_bytes": len(chunk), "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest()}


@server.tool(structured_output=True)
async def gt_runs_cancel(run_id: str, project_id: str) -> dict[str, Any]:
    return {"accepted": True, "run_id": run_id}


if __name__ == "__main__":
    server.run(transport="streamable-http", host="127.0.0.1", port=int(sys.argv[1]),
               streamable_http_path="/mcp", json_response=True)
