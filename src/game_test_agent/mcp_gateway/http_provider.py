from __future__ import annotations

import base64
import binascii
import json
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

from game_test_agent.contracts.models import (
    ActionSpec,
    CapabilityDescriptor,
    OperationRef,
    RunRef,
    RunResult,
)
from game_test_agent.mcp_gateway.config import McpProviderConfig
from game_test_agent.mcp_gateway.gateway import StandardToolError
from game_test_agent.mcp_gateway.catalog import TestCatalogItem, TestCatalogPage


def extract_tool_payload(result: types.CallToolResult, max_bytes: int) -> dict[str, Any]:
    if result.structured_content is not None:
        payload = result.structured_content
    else:
        texts = [item.text for item in result.content if isinstance(item, types.TextContent)]
        if len(texts) != 1:
            raise StandardToolError(
                "SCHEMA_MISMATCH",
                "tool must return structuredContent or exactly one JSON text block",
            )
        try:
            payload = json.loads(texts[0])
        except json.JSONDecodeError as error:
            raise StandardToolError("SCHEMA_MISMATCH", "tool returned invalid JSON text") from error
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    if len(encoded) > max_bytes:
        raise StandardToolError("RESPONSE_TOO_LARGE", "tool response exceeds configured limit")
    if not isinstance(payload, dict):
        raise StandardToolError("SCHEMA_MISMATCH", "tool response must be a JSON object")
    if result.is_error or "error" in payload:
        error = payload.get("error", payload)
        raise StandardToolError(
            code=str(error.get("code", "PROVIDER_ERROR")),
            message=str(error.get("message", "provider returned an error")),
            retryable=bool(error.get("retryable", False)),
            retry_after_ms=error.get("retry_after_ms"),
            action_id=error.get("action_id"),
            side_effect_status=error.get("side_effect_status", "none"),
            run_ref=(RunRef.model_validate(error["run_ref"]) if error.get("run_ref") else None),
        )
    return payload


class McpStreamableHttpProvider:
    """Allowlisted MCP provider adapter with a fresh process-local session per operation."""

    def __init__(self, config: McpProviderConfig) -> None:
        self.config = config
        self.provider_id = config.provider_id
        self._capability_tools: dict[tuple[str, str], str] = {}

    @property
    def catalog_supported(self) -> bool:
        return self.config.tests_search_tool is not None

    @property
    def source_run_supported(self) -> bool:
        return self.config.source_run_get_tool is not None

    async def list_tests(
        self, query: str = "", *, page: int = 1, page_size: int = 50
    ) -> TestCatalogPage:
        if not self.config.tests_search_tool:
            raise StandardToolError("UNSUPPORTED_OPERATION", "MCP provider has no test catalog")
        payload = await self._call(
            self.config.tests_search_tool,
            {"query": query, "page": page, "page_size": page_size},
        )
        return TestCatalogPage(
            items=[
                TestCatalogItem.model_validate({
                    key: value for key, value in item.items()
                    if key in TestCatalogItem.model_fields
                })
                for item in payload.get("items", [])
            ],
            total=payload["total"], page=payload["page"], page_size=payload["page_size"],
        )

    async def query_source_run(self, run_ref: RunRef) -> RunResult:
        if run_ref.provider_id != self.provider_id or run_ref.project_id not in self.config.project_ids:
            raise PermissionError("source run is outside this MCP provider scope")
        if not self.config.source_run_get_tool:
            raise StandardToolError("UNSUPPORTED_OPERATION", "MCP provider has no source-run tool")
        payload = await self._call(
            self.config.source_run_get_tool, {"run_ref": run_ref.model_dump(mode="json")}
        )
        result = RunResult.model_validate(payload)
        if result.run_ref != run_ref:
            raise StandardToolError("SCHEMA_MISMATCH", "source-run tool returned a different run")
        return result

    async def close(self) -> None:
        # Each operation owns its MCP session; no HTTP client survives a call.
        pass

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[ClientSession]:
        import httpx2

        headers = {"Accept": "application/json, text/event-stream"}
        if self.config.token_env:
            token = os.getenv(self.config.token_env)
            if not token:
                raise StandardToolError(
                    "CREDENTIAL_UNAVAILABLE",
                    f"credential environment variable {self.config.token_env} is not set",
                )
            headers["Authorization"] = f"Bearer {token}"
        async with httpx2.AsyncClient(headers=headers, timeout=30, trust_env=False) as client:
            async with streamable_http_client(
                self.config.url, http_client=client, terminate_on_close=True
            ) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    yield session

    async def _call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        action_id: str | None = None,
        write_call: bool = False,
    ) -> dict[str, Any]:
        try:
            async with self._session() as session:
                result = await session.call_tool(tool_name, arguments=arguments)
        except StandardToolError:
            raise
        except Exception as error:
            raise StandardToolError(
                "CONNECTION_LOST",
                "MCP transport failed",
                retryable=True,
                action_id=action_id,
                side_effect_status="unknown" if write_call else "none",
            ) from error
        if not isinstance(result, types.CallToolResult):
            raise StandardToolError("SCHEMA_MISMATCH", "MCP tool requested additional input")
        return extract_tool_payload(result, self.config.max_response_bytes)

    async def discover_capabilities(self) -> list[CapabilityDescriptor]:
        async with self._session() as session:
            listed = await session.list_tools()
            tools = {tool.name: tool for tool in listed.tools}
            if self.config.capabilities_tool not in tools:
                raise StandardToolError(
                    "CAPABILITY_UNAVAILABLE", "provider has no capability catalog tool"
                )
            result = await session.call_tool(self.config.capabilities_tool, arguments={})
        if not isinstance(result, types.CallToolResult):
            raise StandardToolError("SCHEMA_MISMATCH", "capability catalog requested input")
        payload = extract_tool_payload(result, self.config.max_response_bytes)
        raw_capabilities = payload.get("capabilities")
        if not isinstance(raw_capabilities, list):
            raise StandardToolError("SCHEMA_MISMATCH", "capability catalog lacks capabilities[]")
        descriptors: list[CapabilityDescriptor] = []
        for raw in raw_capabilities:
            descriptor = CapabilityDescriptor.model_validate(raw)
            if descriptor.provider_id != self.provider_id:
                raise StandardToolError("SCHEMA_MISMATCH", "capability provider ID mismatch")
            if not set(descriptor.project_ids).issubset(self.config.project_ids):
                raise StandardToolError(
                    "PERMISSION_DENIED", "capability declares an unallowlisted project"
                )
            tool = tools.get(descriptor.tool_name)
            if tool is None:
                raise StandardToolError("SCHEMA_MISMATCH", "capability binds a missing MCP tool")
            tool_schema = tool.input_schema
            if tool_schema.get("type") != "object" or "action" not in tool_schema.get(
                "properties", {}
            ):
                raise StandardToolError(
                    "SCHEMA_MISMATCH",
                    "bound write tool must accept a structured action object",
                )
            self.bind_capability(descriptor)
            descriptors.append(descriptor)
        return descriptors

    def bind_capability(self, descriptor: CapabilityDescriptor) -> None:
        if descriptor.provider_id != self.provider_id:
            raise ValueError("cannot bind a capability from another provider")
        self._capability_tools[(descriptor.capability_id, descriptor.capability_version)] = (
            descriptor.tool_name
        )

    async def submit(self, action: ActionSpec) -> OperationRef:
        if action.provider_id != self.provider_id:
            raise PermissionError("action provider does not match adapter")
        payload = await self._call(
            self._submit_tool(action),
            {"action": action.model_dump(mode="json", by_alias=True)},
            action_id=action.action_id,
            write_call=True,
        )
        operation = OperationRef.model_validate(payload)
        if operation.action_id != action.action_id:
            raise StandardToolError("SCHEMA_MISMATCH", "provider returned a different action ID")
        return operation

    async def lookup_action(self, action_id: str) -> OperationRef | None:
        payload = await self._call(self.config.action_lookup_tool, {"action_id": action_id})
        if payload.get("found") is False:
            return None
        operation = OperationRef.model_validate(payload.get("operation", payload))
        if operation.action_id != action_id:
            raise StandardToolError("SCHEMA_MISMATCH", "lookup returned a different action ID")
        return operation

    async def query_operation(self, operation_id: str) -> RunResult:
        payload = await self._call(self.config.operation_query_tool, {"operation_id": operation_id})
        return RunResult.model_validate(payload)

    async def cancel(self, action_id: str) -> dict[str, Any]:
        return await self._call(
            self.config.cancel_tool,
            {"action_id": action_id},
            action_id=action_id,
            write_call=True,
        )

    async def fetch_artifact(self, artifact_ref: str) -> tuple[bytes, str]:
        payload = await self._call(self.config.artifact_get_tool, {"artifact_ref": artifact_ref})
        encoded = payload.get("content_base64")
        if not isinstance(encoded, str):
            raise StandardToolError("SCHEMA_MISMATCH", "artifact response lacks content_base64")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as error:
            raise StandardToolError(
                "SCHEMA_MISMATCH", "artifact content is not valid base64"
            ) from error
        if len(content) > self.config.max_response_bytes:
            raise StandardToolError("RESPONSE_TOO_LARGE", "artifact exceeds configured limit")
        return content, str(payload.get("mime_type", "application/octet-stream"))

    def _submit_tool(self, action: ActionSpec) -> str:
        tool_name = self._capability_tools.get((action.capability_id, action.capability_version))
        if tool_name is None:
            raise StandardToolError(
                "CAPABILITY_UNAVAILABLE",
                "capability binding has not been discovered in this process",
                action_id=action.action_id,
            )
        return tool_name
