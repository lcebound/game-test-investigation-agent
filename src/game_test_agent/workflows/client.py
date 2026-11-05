from __future__ import annotations

from typing import Any

from temporalio.client import Client
from temporalio.exceptions import WorkflowAlreadyStartedError

from game_test_agent.workflows.temporal import InvestigationWorkflow


class TemporalWorkflowClient:
    def __init__(self, client: Client, task_queue: str) -> None:
        self.client = client
        self.task_queue = task_queue

    async def start(self, workflow_id: str, investigation_id: str) -> None:
        try:
            await self.client.start_workflow(
                InvestigationWorkflow.run,
                investigation_id,
                id=workflow_id,
                task_queue=self.task_queue,
            )
        except WorkflowAlreadyStartedError:
            # Stable workflow IDs make a lost start acknowledgement safe to retry.
            return

    async def signal(
        self, workflow_id: str, kind: str, command_id: str, payload: dict[str, Any]
    ) -> None:
        handle = self.client.get_workflow_handle(workflow_id)
        if kind == "cancel":
            await handle.signal(InvestigationWorkflow.cancel, command_id)
            return
        signal_name = "evidence_updated" if kind == "evidence" else "input"
        await handle.signal(signal_name, {"command_id": command_id, "payload": payload})
