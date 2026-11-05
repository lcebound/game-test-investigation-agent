from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from mcp.server.mcpserver import MCPServer


server = MCPServer(
    name="game-test-reference-provider",
    version="1.0.0",
    instructions="Protocol-only simulated game test provider. Never use as real validation evidence.",
)

_actions: dict[str, dict[str, Any]] = {}
_operations: dict[str, dict[str, Any]] = {}
_action_hashes: dict[str, str] = {}
_artifacts: dict[str, tuple[bytes, str]] = {}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@server.tool(structured_output=True)
async def gt_capabilities_list() -> dict[str, Any]:
    """List game-test capabilities exposed by this simulated provider."""
    return {
        "capabilities": [
            {
                "schema_version": "2.0",
                "capability_id": "test.run",
                "capability_version": "1.0.0",
                "provider_id": "reference-simulated",
                "provider_revision": "reference-1",
                "tool_name": "gt_runs_submit",
                "project_ids": ["game-a"],
                "supported_engines": ["reference"],
                "target_kinds": ["simulated"],
                "input_schema": {
                    "type": "object",
                    "properties": {"case": {"type": "string"}},
                    "required": ["case"],
                    "additionalProperties": False,
                },
                "output_schema": {"type": "object"},
                "availability": "ready",
                "effects": ["create_test_run"],
                "risk_level": "bounded_execution",
                "required_scopes": ["runs:submit"],
                "execution_owner": "reference-simulated",
                "resource_domain": "reference-simulated",
                "idempotency": "required",
                "async": True,
                "supports_action_lookup": True,
                "evidence_types": ["frame_metrics"],
                "estimated_cost": {"device_minutes": 0.1},
                "operation_kind": "experiment",
                "creates_run": True,
                "requires_existing_run": False,
                "resource_mutation": "bounded",
                "cost_category": "execution",
            }
        ]
    }


@server.tool(structured_output=True)
async def gt_runs_submit(action: dict[str, Any]) -> dict[str, Any]:
    """Accept a strongly typed ActionSpec and create one simulated operation."""
    action_id = str(action.get("action_id", ""))
    if not action_id:
        return {"error": {"code": "INVALID_ARGUMENT", "message": "action_id is required"}}
    digest = hashlib.sha256(_canonical(action).encode()).hexdigest()
    if action_id in _action_hashes and _action_hashes[action_id] != digest:
        return {
            "error": {
                "code": "IDEMPOTENCY_CONFLICT",
                "message": "action ID reused with different payload",
                "action_id": action_id,
                "side_effect_status": "committed",
            }
        }
    if action_id in _actions:
        return _actions[action_id]
    operation_id = f"op-{uuid.uuid4().hex}"
    run_id = f"run-{uuid.uuid4().hex}"
    project_id = str(action["project_id"])
    run_ref = {
        "provider_id": "reference-simulated",
        "project_id": project_id,
        "run_id": run_id,
    }
    operation = {
        "accepted": True,
        "action_id": action_id,
        "run_ref": run_ref,
        "operation_id": operation_id,
        "execution_status": "queued",
        "suggested_poll_interval_seconds": 0.1,
    }
    csv_content = b"frame_ms\n16.1\n17.2\n18.4\n19.0\n"
    artifact_ref = f"reference://artifact/{run_id}/frame.csv"
    _artifacts[artifact_ref] = (csv_content, "text/csv")
    now = datetime.now(timezone.utc).isoformat()
    _operations[operation_id] = {
        "run_ref": run_ref,
        "revision": 1,
        "execution_status": "succeeded",
        "test_verdict": "passed",
        "execution_source": "simulated",
        "build_ref": action.get("build_ref"),
        "environment_fingerprint": action.get("target_selector", {}).get("environment_fingerprint"),
        "provider_status": "REFERENCE_COMPLETED",
        "started_at": now,
        "finished_at": now,
        "environment": {"adapter": "reference", "simulation": True},
        "findings": [],
        "evidence_manifest": [
            {
                "evidence_id": f"ev-{run_id}-frame",
                "artifact_ref": artifact_ref,
                "kind": "frame_metrics",
                "mime_type": "text/csv",
                "sha256": hashlib.sha256(csv_content).hexdigest(),
                "collected_at": now,
                "locator": {"kind": "sample_window", "value": "all rows"},
                "quality": "simulated",
                "producer": "reference-simulated",
                "producer_version": "1.0.0",
                "limitations": ["protocol test double"],
            }
        ],
        "missing_fields": ["real_target", "real_engine", "real_artifacts"],
    }
    _action_hashes[action_id] = digest
    _actions[action_id] = operation
    return operation


@server.tool(structured_output=True)
async def gt_actions_lookup(action_id: str) -> dict[str, Any]:
    """Look up an action after an ambiguous submit result."""
    operation = _actions.get(action_id)
    return (
        {"found": operation is not None, "operation": operation} if operation else {"found": False}
    )


@server.tool(structured_output=True)
async def gt_operations_get(operation_id: str) -> dict[str, Any]:
    """Return the simulated terminal RunResult."""
    result = _operations.get(operation_id)
    if result is None:
        return {"error": {"code": "NOT_FOUND", "message": "operation not found"}}
    return result


@server.tool(structured_output=True)
async def gt_runs_cancel(action_id: str) -> dict[str, Any]:
    """Cooperatively cancel a simulated run."""
    operation = _actions.get(action_id)
    if operation is None:
        return {"accepted": False, "action_id": action_id}
    operation_id = operation["operation_id"]
    _operations[operation_id]["execution_status"] = "canceled"
    _operations[operation_id]["provider_status"] = "REFERENCE_CANCELED"
    return {"accepted": True, "action_id": action_id}


@server.tool(structured_output=True)
async def gt_artifacts_get(artifact_ref: str) -> dict[str, Any]:
    """Return one simulated artifact through the controlled provider channel."""
    artifact = _artifacts.get(artifact_ref)
    if artifact is None:
        return {"error": {"code": "NOT_FOUND", "message": "artifact not found"}}
    content, mime_type = artifact
    return {
        "content_base64": base64.b64encode(content).decode("ascii"),
        "mime_type": mime_type,
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def main() -> None:
    host = os.getenv("GTA_REFERENCE_MCP_HOST", "127.0.0.1")
    port = int(os.getenv("GTA_REFERENCE_MCP_PORT", "8090"))
    try:
        server.run(
            transport="streamable-http",
            host=host,
            port=port,
            streamable_http_path="/mcp",
            json_response=True,
            stateless_http=False,
            max_request_body_size=2 * 1024 * 1024,
        )
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
