"""Durable readonly MCP jobs using operator-declared submit/lookup/query/cancel tools."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
import hashlib
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace

from mcp import types

from game_test_agent.storage.repository import canonical_json, payload_hash, ConflictError
from game_test_agent.mcp_gateway.http_provider import extract_tool_payload


class NativeOperations:
    def __init__(self, controller):
        self.controller, self.repository = controller, controller.repository

    def pending(self, investigation_id):
        rows = self.repository._connection.execute(
            """SELECT * FROM native_tool_jobs WHERE investigation_id=?
            AND state NOT IN ('collected','canceled') ORDER BY created_at,job_id LIMIT 17""",
            (investigation_id,),
        ).fetchall()
        if len(rows) > 16:
            raise ValueError("pending native operation limit exceeded")
        return rows

    def _update(self, row, state, *, operation_id=None, result=None):
        now = datetime.now(timezone.utc).isoformat()
        with self.repository.transaction() as connection:
            connection.execute(
                """UPDATE native_tool_jobs SET state=?,operation_id=COALESCE(?,operation_id),
                result_json=COALESCE(?,result_json),updated_at=? WHERE job_id=?""",
                (
                    state,
                    operation_id,
                    canonical_json(result) if result is not None else None,
                    now,
                    row["job_id"],
                ),
            )

    @staticmethod
    def _timeout(deadline, limit=30):
        remaining = deadline - time.time()
        if remaining <= 0:
            raise TimeoutError("native operation deadline reached")
        return min(limit, remaining)

    async def execute(self, view, call_id, provider, tool, arguments, *, deadline=None):
        deadline = (
            deadline
            if deadline is not None
            else view.created_at.timestamp() + view.request.budget.max_wall_seconds
        )
        self._timeout(deadline)
        policy = provider.config.native_operations[tool.name]
        if tool.annotations is None or tool.annotations.read_only_hint is not True:
            raise PermissionError("asynchronous native tools must declare readonly behavior")
        identity = payload_hash(
            [provider.config.model_dump(mode="json"), tool.name, tool.input_schema]
        )
        row = self.repository._connection.execute(
            "SELECT * FROM native_tool_jobs WHERE investigation_id=? AND call_id=?",
            (view.investigation_id, call_id),
        ).fetchone()
        fresh = row is None
        if fresh:
            if len(self.pending(view.investigation_id)) >= 16:
                raise ValueError("at most 16 native operations may remain pending")
            job_id = "native-job-" + payload_hash([view.investigation_id, call_id])
            now = datetime.now(timezone.utc).isoformat()
            with self.repository.transaction() as connection:
                connection.execute(
                    """INSERT INTO native_tool_jobs
                    (job_id,investigation_id,call_id,provider_id,tool_name,request_json,request_hash,config_hash,state,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,'submitting',?,?)""",
                    (
                        job_id,
                        view.investigation_id,
                        call_id,
                        provider.provider_id,
                        tool.name,
                        canonical_json(arguments),
                        payload_hash(arguments),
                        identity,
                        now,
                        now,
                    ),
                )
                self.repository._insert_event(
                    connection,
                    view.investigation_id,
                    "mcp.native.operation_submitting",
                    {"job_id": job_id},
                )
            row = self.repository._connection.execute(
                "SELECT * FROM native_tool_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        if row["config_hash"] != identity or row["request_hash"] != payload_hash(arguments):
            raise PermissionError("operation identity or configuration changed")
        self.repository.record_resource_cleanup(
            view.investigation_id,
            row["job_id"],
            resource_kind="mcp_operation",
            cleanup_owner=provider.provider_id,
        )
        if row["result_json"]:
            if row["state"] == "completed":
                self.repository.resolve_cleanup(row["job_id"])
            return types.CallToolResult.model_validate_json(row["result_json"])
        if fresh:
            request = {**arguments, policy.request_id_argument: row["job_id"]}
            try:
                timeout = self._timeout(deadline)
                response = await asyncio.wait_for(provider.call_native(tool, request), timeout)
            except (TimeoutError, ConnectionError):
                return None  # A committed submission is resolved by lookup, never resubmitted.
        else:
            poll = row["polls"] + 1
            try:
                self.repository.reserve_native_read(
                    view.investigation_id,
                    f"{row['job_id']}-poll-{poll}",
                    payload_hash([identity, poll]),
                    view.request.budget.max_tool_reads,
                )
            except ConflictError as error:
                if str(error) != "READ_BUDGET_EXHAUSTED":
                    raise
                error = types.CallToolResult(
                    content=[],
                    structured_content={"error": "operation_read_budget_exhausted"},
                    is_error=True,
                )
                self._update(row, "unresolved_result", result=error.model_dump(mode="json"))
                return error
            with self.repository.transaction() as connection:
                connection.execute(
                    "UPDATE native_tool_jobs SET polls=? WHERE job_id=?", (poll, row["job_id"])
                )
            control, request = (
                (policy.query_tool, {policy.operation_id_argument: row["operation_id"]})
                if row["operation_id"]
                else (policy.lookup_tool, {policy.request_id_argument: row["job_id"]})
            )
            try:
                timeout = self._timeout(deadline)
                response = await asyncio.wait_for(
                    provider.call_operation_control(control, request), timeout
                )
            except (TimeoutError, ConnectionError):
                return None
        payload = extract_tool_payload(response, provider.config.max_response_bytes)
        operation_id = payload.get("operation_id", row["operation_id"])
        if operation_id is not None and (
            not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 256
        ):
            raise ValueError("invalid native operation identity")
        if row["operation_id"] and operation_id != row["operation_id"]:
            raise PermissionError("provider changed the operation identity")
        if payload.get("status") in {"completed", "succeeded", "failed", "canceled"}:
            error = payload["status"] in {"failed", "canceled"}
            value = payload.get("result", {"error": payload["status"]})
            result = types.CallToolResult(
                content=[],
                structured_content=value if isinstance(value, dict) else {"value": value},
                is_error=error,
            )
            self._update(
                row, "completed", operation_id=operation_id, result=result.model_dump(mode="json")
            )
            self.repository.resolve_cleanup(row["job_id"])
            return result
        self._update(row, "running" if operation_id else "uncertain", operation_id=operation_id)
        return None

    async def resume(self, view, bindings, memory, deadline):
        for row in self.pending(view.investigation_id):
            if time.time() >= deadline:
                memory.finalize = True
                break
            binding = next(
                (
                    (provider, tool)
                    for provider, tool in bindings.values()
                    if provider.provider_id == row["provider_id"] and tool.name == row["tool_name"]
                ),
                None,
            )
            if binding is None:
                raise PermissionError("pending operation provider or tool is unavailable")
            provider, tool = binding
            limits = getattr(self.controller, "execution_limits", None)
            from game_test_agent.runtime.execution import CapacityExceeded

            try:
                async with (
                    limits.slot(
                        "tools",
                        deadline=deadline,
                        scope=(view.request.tenant_id, view.request.project_id),
                    )
                    if limits
                    else nullcontext()
                ):
                    result = await self.execute(
                        view,
                        row["call_id"],
                        provider,
                        tool,
                        json.loads(row["request_json"]),
                        deadline=deadline,
                    )
                    if result is None:
                        continue
                    name = next(name for name, value in bindings.items() if value == binding)
                    call = SimpleNamespace(
                        id=row["call_id"],
                        function=SimpleNamespace(name=name, arguments=row["request_json"]),
                    )
                    payload, _ = await self.controller._execute_call_unbounded(
                        view, call, bindings, memory, deadline, native_result_override=result
                    )
            except (CapacityExceeded, TimeoutError):
                break
            expected_id = (
                "mcp-"
                + hashlib.sha256(f"{view.investigation_id}:{row['call_id']}".encode()).hexdigest()[
                    :32
                ]
            )
            if payload.get("evidence_id") == expected_id:
                self._update(row, "collected")
            else:
                # A handle or an expired ingestion attempt is not collected evidence.
                # Keep the persisted result available for a later fenced recovery.
                memory.notes["operation:" + row["job_id"]] = {
                    "job_id": row["job_id"],
                    "collection_gap": payload.get("error_code", "EVIDENCE_NOT_COLLECTED"),
                }
        memory.save(self.repository, self.controller.artifacts, view)

    async def cancel_investigation(self, investigation_id):
        rows = self.repository._connection.execute(
            """SELECT j.* FROM native_tool_jobs j JOIN cleanup_operations c
            ON c.action_id=j.job_id WHERE j.investigation_id=? AND c.state='pending'
            ORDER BY c.updated_at,j.job_id LIMIT 4""",
            (investigation_id,),
        ).fetchall()
        deadline = time.monotonic() + 15

        async def control_call(function, *args):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("cleanup deadline reached")
            limits = getattr(self.controller, "execution_limits", None)
            async with (
                limits.slot("tools", deadline=time.time() + remaining) if limits else nullcontext()
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("cleanup deadline reached")
                return await asyncio.wait_for(function(*args), remaining)

        for row in rows:
            if time.monotonic() >= deadline:
                break
            # Rotate every attempted item, including missing configuration. An
            # unavailable oldest item must not starve later owned resources.
            self.repository.note_cleanup_attempt(
                row["job_id"], "native_operation_cancel_unconfirmed"
            )
            provider = self.controller.providers.providers.get(row["provider_id"])
            if provider is None:
                continue
            policy = provider.config.native_operations.get(row["tool_name"])
            if policy is None:
                continue
            try:
                tools = await control_call(provider.native_tools)
                tool = next((tool for tool in tools if tool.name == row["tool_name"]), None)
                if (
                    tool is None
                    or payload_hash(
                        [provider.config.model_dump(mode="json"), tool.name, tool.input_schema]
                    )
                    != row["config_hash"]
                ):
                    continue
                operation_id = row["operation_id"]
                if not operation_id:
                    lookup = extract_tool_payload(
                        await control_call(
                            provider.call_operation_control,
                            policy.lookup_tool,
                            {policy.request_id_argument: row["job_id"]},
                        ),
                        provider.config.max_response_bytes,
                    )
                    operation_id = lookup.get("operation_id")
                if not operation_id:
                    continue  # Absence is not proof an ambiguous submission was never committed.
                response = extract_tool_payload(
                    await control_call(
                        provider.call_operation_control,
                        policy.cancel_tool,
                        {policy.operation_id_argument: operation_id},
                    ),
                    provider.config.max_response_bytes,
                )
                if response.get("status") in {"canceled", "completed", "failed"}:
                    self._update(row, "canceled", operation_id=operation_id)
                    self.repository.resolve_cleanup(row["job_id"])
            except Exception:
                self.repository.note_cleanup_attempt(
                    row["job_id"], "native_operation_cancel_unconfirmed"
                )
