"""Native MCP discovery and calls. No platform catalog or result contract is required."""
from __future__ import annotations

import hashlib
import json

from jsonschema import Draft202012Validator

from game_test_agent.contracts.models import CapabilityDescriptor
from game_test_agent.mcp_gateway.gateway import StandardToolError
from game_test_agent.mcp_gateway.http_provider import McpStreamableHttpProvider


class NativeMcpProvider(McpStreamableHttpProvider):
    @property
    def catalog_supported(self) -> bool:
        return False

    @property
    def source_run_supported(self) -> bool:
        return False

    async def native_tools(self):
        async with self._session() as session:
            result = await session.list_tools()
            tools = list(result.tools)
            while result.next_cursor:
                result = await session.list_tools(cursor=result.next_cursor)
                tools.extend(result.tools)
        # Operators explicitly authorize tool names, or the server declares read-only tools.
        internal = {name for policy in self.config.native_operations.values()
                    for name in (policy.lookup_tool, policy.query_tool, policy.cancel_tool)}
        return [tool for tool in tools if tool.name not in internal and (
            tool.name in self.config.allowed_tools if self.config.allowed_tools else
            tool.annotations is not None and tool.annotations.read_only_hint is True
        )]

    async def call_operation_control(self, name, arguments):
        internal = {value for policy in self.config.native_operations.values()
                    for value in (policy.lookup_tool, policy.query_tool, policy.cancel_tool)}
        if name not in internal:
            raise PermissionError("operation control is not configured")
        async with self._session() as session:
            page = await session.list_tools()
            tools = list(page.tools)
            while page.next_cursor:
                page = await session.list_tools(cursor=page.next_cursor)
                tools.extend(page.tools)
            tool = next((item for item in tools if item.name == name), None)
            if tool is None:
                raise StandardToolError("CAPABILITY_UNAVAILABLE", "operation control tool was removed")
            Draft202012Validator(tool.input_schema).validate(arguments)
            return await session.call_tool(name, arguments=arguments)

    async def discover_capabilities(self):
        descriptors = []
        for tool in await self.native_tools():
            digest = hashlib.sha256(json.dumps(tool.input_schema, sort_keys=True).encode()).hexdigest()[:16]
            descriptors.append(CapabilityDescriptor(
                capability_id=f"mcp.{tool.name}", capability_version=digest,
                provider_id=self.provider_id, provider_revision=digest, tool_name=tool.name,
                project_ids=self.config.project_ids, input_schema=tool.input_schema,
                operation_kind="retrieve_evidence", creates_run=False, risk_level="read_only",
                resource_mutation="none", cost_category="external_read",
                required_scopes=["evidence:read"], execution_owner=self.provider_id,
                resource_domain=self.provider_id, idempotency="none", supports_action_lookup=False,
            ))
        return descriptors

    async def call_native(self, tool, arguments):
        # Re-discover so removed tools and changed authorization cannot be reused from stale context.
        current = {item.name: item for item in await self.native_tools()}
        if tool.name not in current or current[tool.name].input_schema != tool.input_schema:
            raise StandardToolError("CAPABILITY_UNAVAILABLE", "MCP tool changed or is not authorized")
        Draft202012Validator(tool.input_schema).validate(arguments)
        async with self._session() as session:
            return await session.call_tool(tool.name, arguments=arguments)
