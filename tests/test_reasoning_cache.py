from __future__ import annotations

import asyncio

from game_test_agent.activities import ReasoningService
from game_test_agent.agents import Investigator
from game_test_agent.contracts.models import InvestigationRequest, TestObjective as Objective


def test_reasoning_step_is_cached(repository):
    async def scenario() -> None:
        investigation, _ = repository.create_investigation(
            InvestigationRequest(
                tenant_id="studio-a",
                project_id="game-a",
                objective=Objective(objective_id="perf", description="调查性能"),
            ),
            "req-reason",
        )
        service = ReasoningService(repository, Investigator("disabled", enabled=False))
        context = {
            "objective": {"objective_id": "perf"},
            "capabilities": [],
            "evidence": [],
        }
        first = await service.reason(investigation.investigation_id, 1, "reason-1", context)
        second = await service.reason(investigation.investigation_id, 1, "reason-1", context)
        assert first == second
        assert second.output.kind == "report"

    asyncio.run(scenario())
