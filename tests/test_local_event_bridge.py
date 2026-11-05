from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from game_test_agent.api.app import create_app
from game_test_agent.events.dispatchers import DurableDispatcher
from game_test_agent.events.ingress import EventIngress
from game_test_agent.events.local_bridge import LocalEventBridge, LocalWorkflowClient
from test_events import event


def test_broker_event_starts_existing_local_owner_once(repository):
    ingress = EventIngress(repository)
    body = event()
    first, _ = ingress.accept(body)
    ingress.accept(body)
    workflows = Mock()
    dispatcher = DurableDispatcher(repository, LocalWorkflowClient(repository, workflows), Mock())
    assert asyncio.run(dispatcher.dispatch_starts()) == 1
    assert asyncio.run(dispatcher.dispatch_starts()) == 0
    workflows.start.assert_called_once_with(first.investigation_id)


def test_failed_subscription_closes_all_broker_resources(settings, repository):
    bridge = LocalEventBridge(settings, repository, Mock())
    bridge.publisher = Mock(connect=AsyncMock(), close=AsyncMock())
    first = Mock(run=AsyncMock(), close=AsyncMock())
    second = Mock(run=AsyncMock(side_effect=RuntimeError("subscription failed")), close=AsyncMock())
    bridge.consumers = [first, second]
    with pytest.raises(RuntimeError, match="subscription failed"):
        asyncio.run(bridge.start())
    first.close.assert_awaited_once()
    second.close.assert_awaited_once()
    bridge.publisher.close.assert_awaited_once()
    assert bridge._task is None


def test_api_hosts_bridge_using_same_repository_and_workflows(settings, repository, monkeypatch):
    bridge = Mock(start=AsyncMock(), close=AsyncMock())
    factory = Mock(return_value=bridge)
    monkeypatch.setattr("game_test_agent.api.app.LocalEventBridge", factory)
    app = create_app(replace(settings, enable_local_event_bridge=True), repository)
    with TestClient(app) as client:
        assert client.get("/readyz").status_code == 200
        assert factory.call_args.args[1] is app.state.container.repository
        assert factory.call_args.args[2] is app.state.container.workflows
        bridge.start.assert_awaited_once()
    bridge.close.assert_awaited_once()


def test_local_bridge_cannot_run_beside_temporal_owner(settings):
    with pytest.raises(ValueError, match="local event bridge requires"):
        replace(settings, enable_local_event_bridge=True, workflow_backend="temporal").validate()


def test_broker_outage_does_not_disable_api_or_misreport_channel_health(settings, monkeypatch):
    bridge = Mock(start=AsyncMock(side_effect=ConnectionError("offline")), close=AsyncMock())
    monkeypatch.setattr("game_test_agent.api.app.LocalEventBridge", Mock(return_value=bridge))
    app = create_app(replace(settings, enable_local_event_bridge=True))
    with TestClient(app) as client:
        assert client.get("/readyz").status_code == 200
        result = client.get("/v1/workspace", headers={
            "X-Principal-ID": "tester", "X-Tenant-ID": "studio-a", "X-Project-ID": "game-a",
        })
        assert result.status_code == 200
        assert result.json()["event_channel"] == {"enabled": True, "status": "unavailable"}
    bridge.close.assert_awaited()


def test_automatic_investigation_uses_configured_time_budget(repository):
    from game_test_agent.contracts.models import Budget
    ingress = EventIngress(repository, default_budget=Budget(max_wall_seconds=14400, max_device_minutes=240))
    view, created = ingress.accept(event())
    assert created
    assert view.request.budget.max_wall_seconds == 14400
    assert view.request.budget.max_device_minutes == 240
