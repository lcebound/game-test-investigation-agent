from __future__ import annotations

import asyncio

import httpx

from game_test_agent.api import create_app
from game_test_agent.config import Settings
from game_test_agent.storage import Repository


def test_temporal_api_mode_leaves_start_intent_for_bridge(tmp_path):
    async def scenario() -> None:
        settings = Settings(
            database_path=tmp_path / "temporal-mode.db",
            artifact_root=tmp_path / "artifacts",
            workflow_backend="temporal",
        )
        repository = Repository(settings.database_path)
        app = create_app(settings, repository)
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/v1/investigations",
                    headers={
                        "Idempotency-Key": "temporal-1",
                        "X-Principal-ID": "tester",
                        "X-Tenant-ID": "studio-a",
                        "X-Project-ID": "game-a",
                    },
                    json={
                        "tenant_id": "studio-a",
                        "project_id": "game-a",
                        "objective": {"objective_id": "perf", "description": "验证帧耗时"},
                    },
                )
                assert response.status_code == 202
                pending = repository.pending_workflow_starts()
                assert pending[0]["investigation_id"] == response.json()["investigation_id"]

    asyncio.run(scenario())
