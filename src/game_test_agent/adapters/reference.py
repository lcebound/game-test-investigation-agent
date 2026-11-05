from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from game_test_agent.contracts.models import ActionSpec, OperationRef, RunRef, RunResult


class ReferenceProviderAdapter:
    """Protocol test double. Every result is marked simulated and is never production evidence."""

    provider_id = "reference-simulated"

    def __init__(self) -> None:
        self._actions: dict[str, OperationRef] = {}
        self._results: dict[str, RunResult] = {}

    async def submit(self, action: ActionSpec) -> OperationRef:
        existing = self._actions.get(action.action_id)
        if existing:
            return existing
        operation_id = f"op-{uuid.uuid4().hex}"
        run_ref = RunRef(
            provider_id=self.provider_id,
            project_id=action.project_id,
            run_id=f"run-{uuid.uuid4().hex}",
        )
        operation = OperationRef(
            accepted=True,
            action_id=action.action_id,
            run_ref=run_ref,
            operation_id=operation_id,
            execution_status="queued",
            suggested_poll_interval_seconds=0.1,
        )
        self._actions[action.action_id] = operation
        self._results[operation_id] = RunResult(
            run_ref=run_ref,
            revision=1,
            execution_status="succeeded",
            test_verdict="passed",
            execution_source="simulated",
            provider_status="REFERENCE_COMPLETED",
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            environment={"adapter": "reference", "simulation": True},
            missing_fields=["real_target", "real_artifacts"],
        )
        return operation

    async def lookup_action(self, action_id: str) -> OperationRef | None:
        return self._actions.get(action_id)

    async def query_operation(self, operation_id: str) -> RunResult:
        return self._results[operation_id]

    async def cancel(self, action_id: str) -> dict[str, Any]:
        operation = self._actions[action_id]
        if operation.operation_id:
            prior = self._results[operation.operation_id]
            self._results[operation.operation_id] = prior.model_copy(
                update={"execution_status": "canceled", "provider_status": "REFERENCE_CANCELED"}
            )
        return {"accepted": True, "action_id": action_id}
