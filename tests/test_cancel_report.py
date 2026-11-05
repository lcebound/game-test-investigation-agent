from __future__ import annotations

import asyncio

from game_test_agent.agents import Investigator
from game_test_agent.capabilities import CapabilityRegistry
from game_test_agent.contracts.models import (
    InvestigationRequest,
    InvestigationStatus,
    TestObjective as Objective,
)
from game_test_agent.reports import ReportService
from game_test_agent.workflows import LocalWorkflowBackend


def test_local_cancel_publishes_canceled_report(repository):
    async def scenario() -> None:
        investigation, _ = repository.create_investigation(
            InvestigationRequest(
                tenant_id="studio-a",
                project_id="game-a",
                objective=Objective(objective_id="wait", description="等待测试"),
            ),
            "cancel-1",
        )
        backend = LocalWorkflowBackend(
            repository,
            CapabilityRegistry(repository),
            Investigator("disabled", enabled=False),
            ReportService(repository),
        )
        assert backend.request_cancel(investigation.investigation_id)
        view = repository.get_investigation(investigation.investigation_id)
        report, _, _ = repository.get_report(investigation.investigation_id, 1)
        assert view.status == InvestigationStatus.CANCELED
        assert report.status == "canceled"
        assert report.resolution == "canceled"

    asyncio.run(scenario())
