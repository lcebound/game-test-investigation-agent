from __future__ import annotations

import json

import pytest
from mcp import types

from game_test_agent.mcp_gateway import McpProviderConfig, StandardToolError
from game_test_agent.mcp_gateway.http_provider import extract_tool_payload


def test_mcp_endpoint_requires_https_except_explicit_localhost():
    with pytest.raises(ValueError, match="HTTPS"):
        McpProviderConfig(
            provider_id="lab-a",
            url="http://lab.internal/mcp",
            project_ids=["game-a"],
        )
    local = McpProviderConfig(
        provider_id="lab-a",
        url="http://127.0.0.1:9000/mcp",
        project_ids=["game-a"],
        allow_insecure_localhost=True,
    )
    assert local.provider_id == "lab-a"


def test_mcp_payload_accepts_structured_or_single_json_text():
    structured = types.CallToolResult(content=[], structuredContent={"accepted": True})
    assert extract_tool_payload(structured, 1024) == {"accepted": True}
    text = types.CallToolResult(content=[types.TextContent(text=json.dumps({"found": False}))])
    assert extract_tool_payload(text, 1024) == {"found": False}


def test_mcp_error_is_normalized():
    result = types.CallToolResult(
        content=[],
        structuredContent={
            "error": {
                "code": "RESOURCE_BUSY",
                "message": "busy",
                "retryable": True,
                "side_effect_status": "none",
            }
        },
        isError=True,
    )
    with pytest.raises(StandardToolError) as captured:
        extract_tool_payload(result, 1024)
    assert captured.value.code == "RESOURCE_BUSY"
    assert captured.value.retryable is True
