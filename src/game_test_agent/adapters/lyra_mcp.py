"""Map Lyra's v1 MCP tools to the platform-neutral investigation contracts.

All platform I/O uses MCP. No platform HTTP API client or fallback lives here.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from typing import Any

from game_test_agent.contracts.models import (
    ActionSpec, BuildRef, CapabilityDescriptor, EvidenceManifestItem,
    OperationRef, RunRef, RunResult,
)
from game_test_agent.mcp_gateway.catalog import TestCatalogItem, TestCatalogPage
from game_test_agent.mcp_gateway.config import McpProviderConfig
from game_test_agent.mcp_gateway.gateway import StandardToolError
from game_test_agent.mcp_gateway.http_provider import McpStreamableHttpProvider

_TERMINAL = {"succeeded", "failed", "canceled"}
_TARGETS = {"android", "windows_client", "windows_server"}
_CAPTURE_PROFILES = {"standard", "frame_csv", "none"}


class LyraMcpProvider(McpStreamableHttpProvider):
    """Use the platform's MCP endpoint, mapping its v1 tool payloads at this boundary."""

    def __init__(self, config: McpProviderConfig) -> None:
        if config.contract != "lyra_v1" or len(config.project_ids) != 1:
            raise ValueError("Lyra MCP requires the lyra_v1 contract and exactly one project")
        super().__init__(config)
        self.project_id = config.project_ids[0]
        self.origin = "agent"

    @property
    def catalog_supported(self) -> bool:
        return True

    @property
    def source_run_supported(self) -> bool:
        return True

    async def discover_capabilities(self) -> list[CapabilityDescriptor]:
        payload = await self._call(
            "gt_capabilities_list", {"project_id": self.project_id}
        )
        if payload.get("provider_id") != self.provider_id:
            raise StandardToolError("SCHEMA_MISMATCH", "platform provider identity changed")
        descriptors: list[CapabilityDescriptor] = []
        for raw in payload.get("capabilities", []):
            if raw.get("capability_id") != "test.run":
                continue
            if raw.get("project_ids") != [self.project_id]:
                raise StandardToolError("PERMISSION_DENIED", "platform project scope changed")
            descriptors.append(
                CapabilityDescriptor(
                    capability_id="test.run",
                    capability_version=raw["capability_version"],
                    provider_id=self.provider_id,
                    provider_revision=payload.get("provider_revision", "unknown"),
                    tool_name="gt_runs_submit",
                    project_ids=[self.project_id],
                    supported_engines=raw.get("supported_engines", []),
                    target_kinds=raw.get("target_kinds", []),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "test_ref": {
                                "type": "object",
                                "properties": {"test_id": {"type": "string"}},
                                "required": ["test_id"],
                                "additionalProperties": False,
                            },
                            "capture_profile": {
                                "type": "string",
                                "enum": sorted(_CAPTURE_PROFILES),
                            },
                        },
                        "required": ["test_ref"],
                        "additionalProperties": False,
                    },
                    availability=raw.get("availability", "unavailable"),
                    preconditions=raw.get("preconditions", []),
                    effects=raw.get("effects", []),
                    risk_level="bounded_execution",
                    required_scopes=raw.get("required_scopes", []),
                    execution_owner=raw.get("execution_owner", self.provider_id),
                    resource_domain=raw.get("resource_domain", self.provider_id),
                    idempotency="required",
                    supports_action_lookup=True,
                    evidence_types=raw.get("evidence_types", []),
                    estimated_cost=raw.get("estimated_cost", {}),
                )
            )
        return descriptors

    async def search_tests(
        self, query: str = "", *, page: int = 1, page_size: int = 100
    ) -> dict[str, Any]:
        return await self._call(
            "gt_tests_search",
            {"project_id": self.project_id, "query": query,
             "page": page, "page_size": page_size},
        )

    async def list_tests(
        self, query: str = "", *, page: int = 1, page_size: int = 50
    ) -> TestCatalogPage:
        payload = await self.search_tests(query, page=page, page_size=page_size)
        return TestCatalogPage(
            items=[TestCatalogItem(
                test_ref=item["test_ref"], name=item["name"],
                description=item.get("description", ""),
                target_kinds=item.get("target_kinds", []), tags=item.get("tags", []),
            ) for item in payload["items"]],
            total=payload["total"], page=payload["page"], page_size=payload["page_size"],
        )

    async def list_targets(self) -> dict[str, Any]:
        return await self._call("gt_targets_list", {"project_id": self.project_id})

    async def submit(self, action: ActionSpec) -> OperationRef:
        if action.provider_id != self.provider_id or action.project_id != self.project_id:
            raise PermissionError("action is outside this platform adapter scope")
        test_ref = action.test_ref or action.parameters.get("test_ref")
        if not test_ref:
            raise ValueError("platform test.run requires an explicit registered test_ref")
        if hasattr(test_ref, "model_dump"):
            test_ref = test_ref.model_dump(mode="json", exclude_none=True)
        target_kind = action.target_selector.target_kind
        if target_kind not in _TARGETS:
            raise ValueError("platform target_kind must be explicit and supported")
        capture = action.parameters.get("capture_profile", "standard")
        if capture not in _CAPTURE_PROFILES:
            raise ValueError("unsupported platform capture profile")
        timeout = int(action.constraints.get("timeout_seconds", 600))
        if timeout < 1 or timeout > 21600:
            raise ValueError("platform timeout is outside the supported range")
        if self.origin == "agent":
            root_event_id = action.constraints.get("root_event_id")
            causation_id = action.constraints.get("causation_id")
            depth = action.constraints.get("investigation_depth")
            if not root_event_id or not causation_id or not isinstance(depth, int) or depth < 1:
                raise ValueError("agent child runs require root event, causation and depth")
        else:
            root_event_id = causation_id = depth = None
        payload: dict[str, Any] = {
            "project_id": self.project_id,
            "action_id": action.action_id,
            "idempotency_key": action.idempotency_key,
            "capability_id": action.capability_id,
            "capability_version": action.capability_version,
            "test_ref": test_ref,
            "target_selector": {
                "target_kind": target_kind,
                "resource_pool": action.target_selector.resource_pool,
                "environment_fingerprint": action.target_selector.environment_fingerprint,
            },
            "parameters": {"capture_profile": capture},
            "constraints": {"timeout_seconds": timeout},
            "origin": self.origin,
        }
        if action.build_ref is not None:
            payload["build_ref"] = action.build_ref.model_dump(mode="json", exclude_none=True)
        if self.origin == "agent":
            payload.update(
                investigation_id=action.investigation_id,
                root_event_id=root_event_id,
                causation_id=causation_id,
                investigation_depth=depth,
            )
        raw = await self._call(
            "gt_runs_submit", {"request": payload},
            action_id=action.action_id, write_call=True,
        )
        return self._operation(raw, action.action_id)

    async def lookup_action(self, action_id: str) -> OperationRef | None:
        try:
            raw = await self._call(
                "gt_runs_find_by_action",
                {"action_id": action_id, "project_id": self.project_id},
            )
        except StandardToolError as error:
            if error.code == "NOT_FOUND":
                return None
            raise
        ref = RunRef.model_validate(raw["run_ref"])
        if ref.provider_id != self.provider_id or ref.project_id != self.project_id:
            raise StandardToolError("SCHEMA_MISMATCH", "lookup returned an out-of-scope run")
        return OperationRef(
            accepted=True,
            action_id=action_id,
            run_ref=ref,
            operation_id=ref.run_id,
            execution_status=raw["execution_status"],
        )

    async def query_source_run(self, run_ref: RunRef) -> RunResult:
        if run_ref.provider_id != self.provider_id or run_ref.project_id != self.project_id:
            raise PermissionError("source run is outside this platform adapter scope")
        return await self.query_operation(run_ref.run_id)

    async def query_operation(self, operation_id: str) -> RunResult:
        raw = await self._call(
            "gt_runs_get", {"run_id": operation_id, "project_id": self.project_id}
        )
        ref = RunRef.model_validate(raw["run_ref"])
        if ref.provider_id != self.provider_id or ref.project_id != self.project_id:
            raise StandardToolError("SCHEMA_MISMATCH", "result is outside this adapter scope")
        if ref.run_id != operation_id:
            raise StandardToolError("SCHEMA_MISMATCH", "result returned a different run identity")
        evidence: list[EvidenceManifestItem] = []
        if raw["execution_status"] in _TERMINAL:
            manifest = await self._call(
                "gt_evidence_list", {"run_id": operation_id, "project_id": self.project_id}
            )
            for item in manifest.get("items", []):
                if item.get("availability") != "ready":
                    continue
                digest = item.get("hash", {}).get("value")
                if not digest:
                    raise StandardToolError("SCHEMA_MISMATCH", "evidence lacks SHA-256")
                evidence.append(
                    EvidenceManifestItem(
                        evidence_id=item["evidence_id"],
                        artifact_ref=item["artifact_id"],
                        kind=item["kind"],
                        mime_type=item["mime_type"].split(";", 1)[0].strip().lower(),
                        sha256=digest,
                        collected_at=item.get("captured_at"),
                        locator={"kind": "region", "value": "whole"},
                        quality="partial",
                        producer=item["producer"],
                        producer_version=item["producer_version"],
                        limitations=["platform_build_or_environment_provenance_may_be_incomplete"],
                    )
                )
        build = raw.get("build") or {}
        build_ref = BuildRef.model_validate(build) if build.get("id") else None
        status = raw["execution_status"]
        verdict = raw.get("test_verdict")
        if verdict not in {"passed", "failed"}:
            verdict = "not_run" if status in _TERMINAL else "unknown"
        environment = raw.get("environment") or {}
        return RunResult(
            run_ref=ref,
            revision=raw["revision"],
            execution_status=status,
            test_verdict=verdict,
            execution_source=raw["execution_source"],
            build_ref=build_ref,
            environment_fingerprint=environment.get("environment_fingerprint"),
            provider_status=raw.get("raw_status") or status,
            started_at=raw.get("started_at"),
            finished_at=raw.get("finished_at"),
            environment=environment,
            findings=raw.get("findings") or [],
            evidence_manifest=evidence,
            missing_fields=raw.get("missing_fields") or [],
        )

    async def cancel(self, action_id: str) -> dict[str, Any]:
        operation = await self.lookup_action(action_id)
        if operation is None or operation.run_ref is None:
            raise StandardToolError("NOT_FOUND", "action has no platform run", action_id=action_id)
        return await self._call(
            "gt_runs_cancel",
            {"run_id": operation.run_ref.run_id, "project_id": self.project_id},
            action_id=action_id, write_call=True,
        )

    async def fetch_artifact(self, artifact_ref: str) -> tuple[bytes, str]:
        if not artifact_ref.startswith("artifact-"):
            raise ValueError("platform artifact reference is invalid")
        chunks: list[bytes] = []
        offset = 0
        size: int | None = None
        digest: str | None = None
        mime_type: str | None = None
        # Reserve room for metadata and worst-case JSON escaping of text content.
        chunk_limit = min(1024 * 1024, (self.config.max_response_bytes - 512) // 6)
        while size is None or offset < size:
            payload = await self._call(
                "gt_evidence_read",
                {"artifact_id": artifact_ref, "project_id": self.project_id,
                 "offset": offset, "limit": chunk_limit},
            )
            if payload.get("artifact_id") != artifact_ref or payload.get("offset") != offset:
                raise StandardToolError("SCHEMA_MISMATCH", "artifact identity or offset mismatch")
            current_size = payload.get("size_bytes")
            current_digest = payload.get("sha256")
            current_mime = str(payload.get("content_type", "application/octet-stream"))
            current_mime = current_mime.split(";", 1)[0].strip().lower()
            if (not isinstance(current_size, int) or isinstance(current_size, bool)
                    or current_size < 0 or not isinstance(current_digest, str)
                    or len(current_digest) != 64):
                raise StandardToolError("SCHEMA_MISMATCH", "artifact lacks size or SHA-256")
            if current_size > self.config.max_response_bytes:
                raise StandardToolError("RESPONSE_TOO_LARGE", "artifact exceeds configured limit")
            if size is None:
                size, digest, mime_type = current_size, current_digest, current_mime
            elif (size, digest, mime_type) != (current_size, current_digest, current_mime):
                raise StandardToolError("SCHEMA_MISMATCH", "artifact changed during retrieval")
            value = payload.get("content")
            try:
                if not isinstance(value, str):
                    raise ValueError("content must be a string")
                if payload.get("encoding") == "base64":
                    chunk = base64.b64decode(value, validate=True)
                elif payload.get("encoding") == "utf-8":
                    chunk = value.encode("utf-8")
                else:
                    raise ValueError("unsupported encoding")
            except (ValueError, binascii.Error) as error:
                raise StandardToolError("SCHEMA_MISMATCH", "invalid artifact encoding") from error
            if (payload.get("returned_bytes") != len(chunk) or len(chunk) > chunk_limit
                    or offset + len(chunk) > size or (not chunk and offset < size)):
                raise StandardToolError("SCHEMA_MISMATCH", "invalid artifact chunk length")
            chunks.append(chunk)
            offset += len(chunk)
        content = b"".join(chunks)
        if hashlib.sha256(content).hexdigest() != digest:
            raise StandardToolError("SCHEMA_MISMATCH", "artifact SHA-256 mismatch")
        return content, mime_type or "application/octet-stream"

    def _operation(self, raw: dict[str, Any], action_id: str) -> OperationRef:
        ref = RunRef.model_validate(raw["run_ref"])
        if ref.provider_id != self.provider_id or ref.project_id != self.project_id:
            raise StandardToolError("SCHEMA_MISMATCH", "submission returned an out-of-scope run")
        if not raw.get("accepted"):
            raise StandardToolError("PROVIDER_ERROR", "platform did not accept the action")
        return OperationRef(
            accepted=True,
            action_id=action_id,
            run_ref=ref,
            operation_id=ref.run_id,
            execution_status=raw["execution_status"],
            suggested_poll_interval_seconds=max(
                0.1, float(raw.get("suggested_poll_interval_ms", 5000)) / 1000
            ),
        )
