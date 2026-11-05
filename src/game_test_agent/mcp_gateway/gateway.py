from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Protocol

from game_test_agent.contracts.models import (
    ActionPermit,
    ActionSpec,
    OperationRef,
    RunRef,
    RunResult,
)


@dataclass(frozen=True, slots=True)
class StandardToolError(Exception):
    code: str
    message: str
    retryable: bool = False
    retry_after_ms: int | None = None
    action_id: str | None = None
    side_effect_status: Literal["none", "committed", "unknown"] = "none"
    run_ref: RunRef | None = None

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class ProviderAdapter(Protocol):
    provider_id: str

    async def submit(self, action: ActionSpec) -> OperationRef: ...

    async def lookup_action(self, action_id: str) -> OperationRef | None: ...

    async def query_operation(self, operation_id: str) -> RunResult: ...

    async def cancel(self, action_id: str) -> dict[str, Any]: ...


class ActionDispatcher:
    def __init__(self) -> None:
        self._providers: dict[str, ProviderAdapter] = {}

    def register(self, provider: ProviderAdapter) -> None:
        if provider.provider_id in self._providers:
            raise ValueError(f"provider {provider.provider_id!r} is already registered")
        self._providers[provider.provider_id] = provider

    async def dispatch(self, action: ActionSpec, permit: ActionPermit) -> OperationRef:
        if action.action_id != permit.action_id:
            raise PermissionError("permit does not belong to this action")
        if permit.expires_at <= datetime.now(timezone.utc):
            raise PermissionError("action permit has expired")
        dispatched_action = action
        if permit.admission_ticket_id is not None:
            dispatched_action = action.model_copy(
                update={
                    "constraints": {
                        **action.constraints,
                        "_admission": {
                            "ticket_id": permit.admission_ticket_id,
                            "resource_domain": permit.resource_domain,
                            "fencing_token": permit.fencing_token,
                            "plan_step_id": permit.plan_step_id,
                            "plan_node_revision": permit.plan_node_revision,
                        },
                    }
                }
            )
        provider = self._providers.get(action.provider_id)
        if provider is None:
            raise StandardToolError(
                "CAPABILITY_UNAVAILABLE", "provider is not connected", action_id=action.action_id
            )
        try:
            return await provider.submit(dispatched_action)
        except StandardToolError as error:
            if error.side_effect_status == "unknown":
                found = await provider.lookup_action(action.action_id)
                if found is not None:
                    return found
            raise

    async def lookup(self, action: ActionSpec) -> OperationRef | None:
        provider = self._providers.get(action.provider_id)
        if provider is None:
            raise StandardToolError(
                "CAPABILITY_UNAVAILABLE", "provider is not connected", action_id=action.action_id
            )
        return await provider.lookup_action(action.action_id)

    async def query(self, provider_id: str, operation_id: str) -> RunResult:
        provider = self._providers.get(provider_id)
        if provider is None:
            raise StandardToolError("CAPABILITY_UNAVAILABLE", "provider is not connected")
        return await provider.query_operation(operation_id)

    async def cancel(self, action: ActionSpec) -> dict[str, Any]:
        provider = self._providers.get(action.provider_id)
        if provider is None:
            raise StandardToolError(
                "CAPABILITY_UNAVAILABLE", "provider is not connected", action_id=action.action_id
            )
        return await provider.cancel(action.action_id)
