from __future__ import annotations

import json
import re
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class NativeOperationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lookup_tool: str = Field(min_length=1, max_length=256)
    query_tool: str = Field(min_length=1, max_length=256)
    cancel_tool: str = Field(min_length=1, max_length=256)
    request_id_argument: str = Field(default="request_id", pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
    operation_id_argument: str = Field(default="operation_id", pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


class McpProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    url: str
    project_ids: list[str] = Field(min_length=1)
    contract: Literal["generic_v2", "lyra_v1"] = "generic_v2"
    token_env: str | None = None
    allowed_tools: list[str] = Field(default_factory=list)
    # Per-tool JSON Pointers into the ORIGINAL structured response. No inferred values.
    native_source_fields: dict[str, dict[str, str]] = Field(default_factory=dict)
    native_operations: dict[str, NativeOperationPolicy] = Field(default_factory=dict, max_length=20)
    capabilities_tool: str = "gt_capabilities_list"
    action_lookup_tool: str = "gt_actions_lookup"
    operation_query_tool: str = "gt_operations_get"
    cancel_tool: str = "gt_runs_cancel"
    artifact_get_tool: str = "gt_artifacts_get"
    tests_search_tool: str | None = None
    source_run_get_tool: str | None = None
    allow_insecure_localhost: bool = False
    max_response_bytes: int = Field(default=2 * 1024 * 1024, ge=1024, le=32 * 1024 * 1024)

    @field_validator("native_source_fields")
    @classmethod
    def validate_source_fields(cls, value: dict[str, dict[str, str]]):
        allowed = {"run_id", "build_id", "environment", "timestamp", "phase", "execution_source"}
        if len(value) > 100:
            raise ValueError("too many native source mappings")
        for tool, fields in value.items():
            if not tool or len(tool) > 256 or set(fields) - allowed:
                raise ValueError("invalid native source mapping")
            for pointer in fields.values():
                if not pointer.startswith("/") or len(pointer) > 500:
                    raise ValueError("native source fields must be bounded JSON Pointers")
                if re.search(r"~(?![01])", pointer):
                    raise ValueError("invalid JSON Pointer escape")
        return value

    @field_validator("token_env")
    @classmethod
    def validate_token_env(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[A-Z][A-Z0-9_]{1,127}", value):
            raise ValueError("token_env must be an uppercase environment variable name")
        return value

    @model_validator(mode="after")
    def validate_endpoint(self) -> "McpProviderConfig":
        parsed = urlparse(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("MCP URL must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError("MCP URL cannot contain credentials or fragments")
        local_hosts = {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme != "https" and not (
            self.allow_insecure_localhost and parsed.hostname in local_hosts
        ):
            raise ValueError("MCP endpoints require HTTPS except explicitly allowed localhost")
        if len(set(self.project_ids)) != len(self.project_ids):
            raise ValueError("project_ids must be unique")
        if self.contract == "lyra_v1" and len(self.project_ids) != 1:
            raise ValueError("Lyra MCP providers require exactly one project")
        return self


def load_provider_configs(raw_json: str) -> list[McpProviderConfig]:
    raw = json.loads(raw_json or "[]")
    if not isinstance(raw, list):
        raise ValueError("GTA_MCP_PROVIDERS_JSON must contain a JSON array")
    configs = [McpProviderConfig.model_validate(item) for item in raw]
    identities = [item.provider_id for item in configs]
    if len(identities) != len(set(identities)):
        raise ValueError("MCP provider IDs must be unique")
    return configs
