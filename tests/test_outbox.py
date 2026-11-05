from __future__ import annotations

import asyncio

from game_test_agent.contracts.models import InvestigationRequest, TestObjective as Objective
from game_test_agent.contracts.models import InvestigationStatus, Resolution
from game_test_agent.events import DurableDispatcher
from game_test_agent.reports import ReportBuilder, ReportService


class FakeWorkflowClient:
    def __init__(self) -> None:
        self.starts = []
        self.signals = []

    async def start(self, workflow_id, investigation_id):
        self.starts.append((workflow_id, investigation_id))

    async def signal(self, workflow_id, kind, command_id, payload):
        self.signals.append((workflow_id, kind, command_id, payload))


class FakePublisher:
    async def publish(self, event_id, event_type, payload, payload_hash):
        return None


class FailingWorkflowClient(FakeWorkflowClient):
    async def start(self, workflow_id, investigation_id):
        raise ConnectionError("temporal unavailable")

    async def signal(self, workflow_id, kind, command_id, payload):
        raise TimeoutError("signal timed out")


class FailingPublisher:
    async def publish(self, event_id, event_type, payload, payload_hash):
        raise ConnectionError("broker unavailable")


def test_durable_dispatcher_marks_starts_and_commands_only_after_success(repository):
    async def scenario() -> None:
        investigation, _ = repository.create_investigation(
            InvestigationRequest(
                tenant_id="studio-a",
                project_id="game-a",
                objective=Objective(objective_id="test", description="测试"),
            ),
            "req-outbox",
        )
        repository.enqueue_command(
            investigation.investigation_id, "cmd-1", "input", {"content": "背景"}
        )
        workflow = FakeWorkflowClient()
        dispatcher = DurableDispatcher(repository, workflow, FakePublisher())
        assert await dispatcher.dispatch_starts() == 1
        assert await dispatcher.dispatch_starts() == 0
        assert await dispatcher.dispatch_commands() == 1
        assert await dispatcher.dispatch_commands() == 0
        assert workflow.starts[0][1] == investigation.investigation_id
        assert workflow.signals[0][2] == "cmd-1"

    asyncio.run(scenario())


def test_dispatcher_obsoletes_command_after_investigation_becomes_terminal(repository):
    async def scenario() -> None:
        investigation, _ = repository.create_investigation(
            InvestigationRequest(
                tenant_id="studio-a",
                project_id="game-a",
                objective=Objective(objective_id="terminal", description="终态命令"),
            ),
            "req-terminal-command",
        )
        repository.enqueue_command(
            investigation.investigation_id, "cmd-late", "input", {"content": "late"}
        )
        repository.transition(investigation.investigation_id, InvestigationStatus.DISCOVERING)
        repository.transition(investigation.investigation_id, InvestigationStatus.PLANNING)
        repository.transition(investigation.investigation_id, InvestigationStatus.REPORTING)
        repository.transition(
            investigation.investigation_id,
            InvestigationStatus.COMPLETED,
            resolution=Resolution.INCONCLUSIVE,
        )
        workflow = FakeWorkflowClient()
        dispatcher = DurableDispatcher(repository, workflow, FakePublisher())

        assert await dispatcher.dispatch_commands() == 0
        assert workflow.signals == []
        assert repository.pending_commands() == []
        assert (
            repository.list_user_inputs(investigation.investigation_id)[0]["command_id"]
            == "cmd-late"
        )

    asyncio.run(scenario())


def test_dispatch_failures_are_backed_off_and_visible_in_metrics(repository):
    async def scenario() -> None:
        investigation, _ = repository.create_investigation(
            InvestigationRequest(
                tenant_id="studio-a",
                project_id="game-a",
                objective=Objective(objective_id="delivery", description="验证可靠投递"),
            ),
            "req-outbox-failure",
        )
        repository.enqueue_command(
            investigation.investigation_id, "cmd-fail", "input", {"content": "背景"}
        )
        dispatcher = DurableDispatcher(repository, FailingWorkflowClient(), FailingPublisher())

        assert await dispatcher.dispatch_starts() == 0
        assert await dispatcher.dispatch_commands() == 0
        # Backoff keeps a hot dispatcher loop from retrying immediately.
        assert repository.pending_workflow_starts() == []
        assert repository.pending_commands() == []

        report = ReportBuilder(repository).build(investigation.investigation_id, "delivery-test")
        ReportService(repository).publish(report)
        assert await dispatcher.dispatch_events() == 0
        assert repository.pending_events() == []

        metrics = repository.operational_metrics("studio-a", "game-a")
        assert metrics["retrying_workflow_starts"] == 1
        assert metrics["retrying_workflow_commands"] == 1
        assert metrics["retrying_result_events"] == 1

    asyncio.run(scenario())
