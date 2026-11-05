from __future__ import annotations

from typing import Any, Protocol

from game_test_agent.contracts.models import TERMINAL_STATUSES
from game_test_agent.storage import Repository
from game_test_agent.storage.feedback import FeedbackStore


class WorkflowClient(Protocol):
    async def start(self, workflow_id: str, investigation_id: str) -> None: ...

    async def signal(
        self, workflow_id: str, kind: str, command_id: str, payload: dict[str, Any]
    ) -> None: ...


class EventPublisher(Protocol):
    async def publish(
        self, event_id: str, event_type: str, payload: dict[str, Any], payload_hash: str
    ) -> None: ...


class DurableDispatcher:
    """At-least-once outbox dispatcher; receivers must deduplicate stable IDs."""

    def __init__(
        self,
        repository: Repository,
        workflow_client: WorkflowClient,
        event_publisher: EventPublisher,
    ) -> None:
        self.repository = repository
        self.workflow_client = workflow_client
        self.event_publisher = event_publisher

    async def dispatch_starts(self, limit: int = 100) -> int:
        sent = 0
        for item in self.repository.pending_workflow_starts(limit):
            try:
                await self.workflow_client.start(item["workflow_id"], item["investigation_id"])
            except Exception as error:
                self.repository.note_workflow_start_attempt(
                    item["investigation_id"], type(error).__name__
                )
                continue
            self.repository.mark_workflow_started(item["investigation_id"])
            sent += 1
        return sent

    async def dispatch_commands(self, limit: int = 100) -> int:
        sent = 0
        attempted = set()
        for item in self.repository.pending_commands(limit):
            if item["kind"] != "cancel":
                continue
            attempted.add(item["command_id"])
            sent += await self._dispatch_command(item)
        if len(attempted) >= limit:
            return sent
        FeedbackStore(self.repository).prepare_batches(limit=limit)
        for item in self.repository.pending_commands(limit):
            if item["command_id"] in attempted:
                continue
            if len(attempted) >= limit:
                break
            attempted.add(item["command_id"])
            sent += await self._dispatch_command(item)
        return sent

    async def _dispatch_command(self, item) -> int:
        if item["kind"] == "input" and self.repository._connection.execute("""SELECT 1 FROM task_input_returns
                WHERE source_investigation_id=? AND source_command_id=?""",
                (item["investigation_id"], item["command_id"])).fetchone():
            self.repository.mark_command_obsolete(item["command_id"], "input_returned")
            return 0
        view = self.repository.get_investigation(item["investigation_id"])
        if view.status in TERMINAL_STATUSES:
            self.repository.mark_command_obsolete(item["command_id"], f"investigation_{view.status}")
            return 0
        try:
            await self.workflow_client.signal(view.workflow_id, item["kind"], item["command_id"], item["payload"])
        except Exception as error:
            self.repository.note_command_attempt(item["command_id"], type(error).__name__)
            return 0
        self.repository.mark_command_sent(item["command_id"])
        return 1

    async def dispatch_events(self, limit: int = 100) -> int:
        sent = 0
        for item in self.repository.pending_events(limit):
            try:
                await self.event_publisher.publish(
                    item["event_id"],
                    item["event_type"],
                    item["payload"],
                    item["payload_hash"],
                )
            except Exception as error:
                self.repository.note_event_publish_attempt(item["event_id"], type(error).__name__)
                continue
            self.repository.mark_event_published(item["event_id"])
            sent += 1
        return sent
