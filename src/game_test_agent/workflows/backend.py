from __future__ import annotations

from typing import Protocol


class WorkflowBackend(Protocol):
    def start(self, investigation_id: str) -> None: ...

    def request_cancel(self, investigation_id: str) -> bool: ...

    async def close(self) -> None: ...


class DeferredWorkflowBackend:
    """Leaves durable outbox rows pending for the Temporal bridge process."""

    def start(self, investigation_id: str) -> None:
        return None

    def request_cancel(self, investigation_id: str) -> bool:
        return False

    async def close(self) -> None:
        return None
