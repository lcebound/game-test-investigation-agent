from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from game_test_agent.activities import ActionService, ReasoningService
from game_test_agent.adapters import ReferenceProviderAdapter
from game_test_agent.agents import Investigator
from game_test_agent.api import create_app
from game_test_agent.capabilities import CapabilityRegistry
from game_test_agent.contracts.models import (
    ActionSpec, ActionState, Budget, CapabilityDescriptor, EvidenceManifestItem, InvestigationRequest,
    InvestigationStatus, NextStep, RunResult,
)
from game_test_agent.evidence import EvidenceManifestIngestor
from game_test_agent.mcp_gateway import ActionDispatcher, StandardToolError
from game_test_agent.planning import ActionPlanner
from game_test_agent.policies import PolicyEngine
from game_test_agent.reports import ReportService
from game_test_agent.storage import LocalArtifactStore
from game_test_agent.workflows.controller import InvestigationController
from game_test_agent.workflows.waiting import ExternalWaitStopped


def make_controller(repository, settings, provider=None, investigator=None):
    registry = CapabilityRegistry(repository)
    registry.register(CapabilityDescriptor.model_validate({
        "capability_id": "test.run", "capability_version": "1", "provider_revision": "1",
        "provider_id": "reference-simulated", "project_ids": ["game-a"],
        "tool_name": "submit", "input_schema": {"type": "object", "additionalProperties": False},
        "risk_level": "bounded_execution", "required_scopes": ["runs:submit"],
        "execution_owner": "reference", "resource_domain": "reference",
        "estimated_cost": {"device_minutes": 1},
    }))
    provider = provider or ReferenceProviderAdapter()
    dispatcher = ActionDispatcher()
    dispatcher.register(provider)
    controller = InvestigationController(
        repository, registry,
        ReasoningService(repository, investigator or Investigator("disabled", False)),
        ActionPlanner("disabled", False),
        ActionService(repository, PolicyEngine(repository, registry), dispatcher),
        EvidenceManifestIngestor(repository, LocalArtifactStore(settings.artifact_root)),
        ReportService(repository), provider_lookup=lambda _: provider,
    )
    return controller, provider


def make_view(repository, key="reliable", **kwargs):
    return repository.create_investigation(InvestigationRequest(
        tenant_id="studio-a", project_id="game-a",
        objective={"objective_id": "smoke", "description": "Run a test"},
        constraints={"action_parameters": {}}, **kwargs,
    ), key, principal_id="tester", scopes={"runs:submit"})[0]


def make_action(view):
    return ActionSpec(
        tenant_id="studio-a", project_id="game-a", investigation_id=view.investigation_id,
        action_id="act-recover", idempotency_key=f"{view.investigation_id}/recover",
        capability_id="test.run", capability_version="1", provider_id="reference-simulated",
        parameters={}, expected_observation="Existing test result", plan_revision=1,
    )


@pytest.mark.parametrize("crash_during_dispatch", [False, True])
def test_api_startup_resumes_existing_run_without_resubmitting(repository, settings, crash_during_dispatch):
    async def run():
        controller, provider = make_controller(repository, settings)
        provider.submit = AsyncMock(wraps=provider.submit)
        view = make_view(repository)
        action = make_action(view)
        if crash_during_dispatch:
            controller.actions.policy.reserve(action, principal_id="tester", scopes={"runs:submit"})
            repository.transition_action(action.action_id, {ActionState.RESERVED}, ActionState.DISPATCHING)
            operation = await provider.submit(action)  # Commit happened before the response was saved.
        else:
            operation = await controller.actions.submit(action, principal_id="tester", scopes={"runs:submit"})
        for status in ("discovering", "planning", "acting", "waiting_external"):
            repository.transition(view.investigation_id, status)
        repository.mark_workflow_started(view.investigation_id)

        app = create_app(settings)
        fresh_repository = app.state.container.repository
        recovered, _ = make_controller(fresh_repository, settings, provider)
        app.state.container.workflows.controller = recovered
        async with app.router.lifespan_context(app):
            for _ in range(100):
                if fresh_repository.get_investigation(view.investigation_id).status == InvestigationStatus.COMPLETED:
                    break
                await asyncio.sleep(0.01)
            result = fresh_repository.get_latest_run_result(action.action_id)
            assert result.ingestion_state == "ingested"
            assert result.result.run_ref == operation.run_ref
            assert fresh_repository.get_investigation(view.investigation_id).status == InvestigationStatus.COMPLETED
            assert len(fresh_repository.list_actions(view.investigation_id)) == 1
            assert provider.submit.await_count == 1
            assert fresh_repository.get_authorization(view.investigation_id)["principal_id"] == "tester"
            assert fresh_repository.pending_cleanup_actions(view.investigation_id) == []
    asyncio.run(run())


@pytest.mark.parametrize("finish_after", [600, None])
def test_status_polling_can_wait_for_long_tests_and_preserves_read_budget(repository, settings, monkeypatch, finish_after):
    import game_test_agent.workflows.controller as module

    controller, _ = make_controller(repository, settings)
    view = make_view(repository)
    action = make_action(view)
    controller.actions.policy.reserve(action, principal_id="tester", scopes={"runs:submit"})
    epoch = datetime.now(timezone.utc)
    clock = SimpleNamespace(elapsed=0.0)

    class Clock:
        @staticmethod
        def now(_tz):
            return epoch + timedelta(seconds=clock.elapsed)

    async def wait_for(awaitable, timeout):
        if getattr(awaitable, "cr_code", None) and awaitable.cr_code.co_name == "wait":
            awaitable.close()
            clock.elapsed += timeout
            raise TimeoutError
        return await awaitable

    async def reconcile(_action_id, *, read_reservation_id):
        repository.reserve_counter(view.investigation_id, "tool_reads", read_reservation_id, 40)
        if finish_after is not None and clock.elapsed >= finish_after:
            return RunResult(
                run_ref={"provider_id": "reference-simulated", "project_id": "game-a", "run_id": "long-run"},
                revision=1, execution_status="succeeded", test_verdict="passed",
                execution_source="simulated", provider_status="done",
            )
        return None

    controller.actions.reconcile = reconcile
    monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(module.asyncio, "wait_for", wait_for)

    async def run():
        if finish_after is None:
            with pytest.raises(ExternalWaitStopped, match="wall_clock_budget_exhausted"):
                await controller._wait_for_result(action.action_id, 2)
            assert clock.elapsed >= 7199
            assert repository.pending_cleanup_actions(view.investigation_id) == [action.action_id]
        else:
            result = await controller._wait_for_result(action.action_id, 2)
            assert result.execution_status == "succeeded"
            assert clock.elapsed >= 600
        assert repository.budget_usage(view.investigation_id)["tool_reads"] <= 32
    asyncio.run(run())


def test_failed_experiment_receives_bounded_replanning_feedback(repository, settings):
    class FailedProvider(ReferenceProviderAdapter):
        async def query_operation(self, operation_id):
            result = await super().query_operation(operation_id)
            return result.model_copy(update={"execution_status": "failed", "test_verdict": "failed",
                "evidence_manifest": [EvidenceManifestItem(
                    evidence_id="fixture-log", artifact_ref="fixture-log", kind="log", mime_type="text/plain",
                    sha256=hashlib.sha256(b"fixture execution failed").hexdigest(),
                    locator={"kind": "region", "value": "whole"}, quality="simulated",
                    producer="fixture", producer_version="1",
                )]})

        async def fetch_artifact(self, _ref):
            return b"fixture execution failed", "text/plain"

    class ReplanningInvestigator:
        model_name = "fixture"
        feedback_seen = False

        async def reason_next_step(self, context):
            feedback = context["diagnostic_guidance"].get("execution_feedback", [])
            self.feedback_seen |= bool(feedback)
            return NextStep(
                kind="report" if feedback else "experiment", objective_id="smoke",
                capability_id="test.run" if not feedback else None,
                provider_id="reference-simulated" if not feedback else None,
                expected_observation="Read existing evidence before retrying",
                reason_summary="plan_feedback_handled" if feedback else "Collect evidence",
                evidence_ids=[e["evidence_id"] for e in context["evidence"]],
            )

    async def run():
        investigator = ReplanningInvestigator()
        controller, _ = make_controller(repository, settings, FailedProvider(), investigator)
        view = make_view(repository)
        report = await controller.run(view.investigation_id, principal_id="tester", scopes={"runs:submit"})
        assert report.stop_reason == "plan_feedback_handled"
        assert investigator.feedback_seen
        assert len(repository.list_actions(view.investigation_id)) == 1
        assert len(repository.list_decision_records(view.investigation_id)) == 3
    asyncio.run(run())


def test_time_budget_defaults_and_explicit_overrides_are_shared_with_ui(settings):
    async def run():
        configured = replace(settings, max_wall_seconds=14400, max_device_minutes=180)
        app = create_app(configured)
        headers = {"X-Principal-ID": "tester", "X-Tenant-ID": "studio-a", "X-Project-ID": "game-a"}
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                workspace = (await client.get('/v1/workspace', headers=headers)).json()
                assert workspace['default_budget']['max_wall_seconds'] == 14400
                response = await client.post('/v1/investigations', headers={**headers, 'Idempotency-Key': 'budget-default'}, json={
                    'tenant_id': 'studio-a', 'project_id': 'game-a',
                    'objective': {'objective_id': 'budget', 'description': 'Check defaults'},
                    'budget': {'max_real_runs': 0, 'max_wall_seconds': 900},
                })
                assert response.status_code == 202
                budget = response.json()['request']['budget']
                assert budget['max_wall_seconds'] == 900
                assert budget['max_device_minutes'] == 180
                assert budget['max_real_runs'] == 0
        assert Budget().max_wall_seconds == 7200
        assert Budget().max_device_minutes == 120
    asyncio.run(run())


@pytest.mark.parametrize("interruption", ["cancel", "unclassified_error"])
def test_interrupted_submission_is_reconciled_without_a_second_run(repository, settings, interruption):
    class InterruptedProvider(ReferenceProviderAdapter):
        def __init__(self):
            super().__init__()
            self.calls = 0
            self.committed = asyncio.Event()

        async def submit(self, action):
            self.calls += 1
            await super().submit(action)
            self.committed.set()
            if interruption == "unclassified_error":
                raise RuntimeError("adapter failed after commit")
            await asyncio.Event().wait()

    async def run():
        provider = InterruptedProvider()
        controller, _ = make_controller(repository, settings, provider)
        view = make_view(repository)
        action = make_action(view)
        task = asyncio.create_task(controller.actions.submit(action, principal_id="tester", scopes={"runs:submit"}))
        await provider.committed.wait()
        if interruption == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(StandardToolError, match="PROVIDER_OUTCOME_UNKNOWN"):
                await task
        assert repository.get_action(action.action_id)[1] == ActionState.UNKNOWN
        assert repository.pending_cleanup_actions(view.investigation_id) == [action.action_id]
        await controller.actions.submit(action, principal_id="tester", scopes={"runs:submit"})
        result = await controller.actions.reconcile(action.action_id)
        assert result.execution_status == "succeeded"
        assert provider.calls == 1
        assert repository.pending_cleanup_actions(view.investigation_id) == []
    asyncio.run(run())
