from __future__ import annotations

import argparse
import asyncio
import importlib.util
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from game_test_agent.contracts.models import InvestigationRequest, RunResult, TestObjective as Objective
from game_test_agent.workflows.waiting import ExternalWaitStopped
from game_test_agent.workflows import InvestigationController
from test_next_generation_architecture import capability


def test_poll_sessions_cannot_reuse_read_budget(repository, monkeypatch):
    investigation, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective=Objective(objective_id="poll", description="wait for result"),
            budget={"max_tool_reads": 3},
        ),
        "poll-budget",
    )
    calls = []

    async def reconcile(action_id, *, read_reservation_id):
        repository.reserve_counter(
            investigation.investigation_id, "tool_reads", read_reservation_id, 3
        )
        calls.append(read_reservation_id)
        return RunResult(
            run_ref={"provider_id": "lab-a", "project_id": "game-a", "run_id": "r"},
            revision=1, execution_status="succeeded", test_verdict="passed",
            execution_source="simulated", provider_status="done",
        )

    async def no_sleep(_):
        pass

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    controller = object.__new__(InvestigationController)
    controller.actions = SimpleNamespace(reconcile=reconcile)
    controller.repository = SimpleNamespace(
        get_action=lambda _: (SimpleNamespace(investigation_id=investigation.investigation_id),),
        get_investigation=repository.get_investigation,
        budget_usage=repository.budget_usage,
        record_cleanup_required=lambda *_: None,
    )
    controller._operation_updates = {}

    async def run():
        for _ in range(3):
            assert await controller._wait_for_result("same-action", 0.1) is not None
        with pytest.raises(ExternalWaitStopped, match="status_read_budget"):
            await controller._wait_for_result("same-action", 0.1)

    asyncio.run(run())
    assert len(calls) == len(set(calls)) == 3


@pytest.mark.parametrize("budget,allowed", [(10, False), (20, True), (30, True)])
def test_live_preflight_checks_provider_cost_and_closes_client(
    settings, monkeypatch, budget, allowed
):
    path = Path(__file__).parents[1] / "scripts/live_workflow_smoke.py"
    spec = importlib.util.spec_from_file_location("live_smoke", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    closed = []

    class Provider:
        async def discover_capabilities(self):
            return [
                capability("test.run").model_copy(update={"estimated_cost": {"device_minutes": 10}})
            ]

        async def close(self):
            closed.append(True)

    monkeypatch.setattr(module, "McpStreamableHttpProvider", lambda _: Provider())
    settings = replace(settings, mcp_providers_json=(
        '[{"provider_id":"lab-a","url":"https://lab.test/mcp","project_ids":["game-a"]}]'
    ))
    args = argparse.Namespace(
        scenario="repeatability",
        max_real_runs=3,
        max_rounds=4,
        device_minute_budget=budget,
    )
    if allowed:
        asyncio.run(module.preflight(settings, args))
    else:
        with pytest.raises(ValueError, match="required_device_minutes=20"):
            asyncio.run(module.preflight(settings, args))
    assert closed == [True]


def test_temporal_poll_scope_distinguishes_rounds_but_preserves_retries(repository, monkeypatch):
    pytest.importorskip("temporalio")
    from game_test_agent.workflows.worker import CoreActivities
    from game_test_agent.runtime.worker_ownership import WorkerOwnership

    reservations = []

    async def reconcile(action_id, *, read_reservation_id):
        reservations.append(read_reservation_id)
        return None

    activities = object.__new__(CoreActivities)
    activities.actions = SimpleNamespace(reconcile=reconcile)
    investigation, _ = repository.create_investigation(InvestigationRequest(
        tenant_id="studio-a", project_id="game-a",
        objective=Objective(objective_id="poll", description="poll ownership")), "temporal-poll-owner")
    activities.repository = repository
    activities.ownership = WorkerOwnership(repository)
    monkeypatch.setattr(repository, "get_action", lambda action_id: (SimpleNamespace(investigation_id=investigation.investigation_id),))

    async def run():
        await activities.query_action("a", 1)
        await activities.query_action("a", 1, 1)
        await activities.query_action("a", 1, 1)
        await activities.query_action("a", 1, 2)

    asyncio.run(run())
    assert reservations[0] == "operation-poll/a/1"
    assert reservations[1] == reservations[2]
    assert len(set(reservations)) == 3
