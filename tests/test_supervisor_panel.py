"""The supervisor reads a bounded cross-task view without leaking other projects."""

import asyncio
from unittest.mock import Mock
import httpx
from game_test_agent.api import create_app
from test_conversation_overview import AUTH, create, close


def test_activity_aggregates_sibling_tasks_and_scopes_sources(settings):
    async def scenario():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            repo = app.state.container.repository
            app.state.container.workflows.start = Mock()
            root = create(repo, text="登录闪退")
            other = create(repo, "other", root=root.investigation_id, text="战斗掉帧")
            outside = create(repo, "outside", text="PRIVATE_OTHER_CONVERSATION")
            close(repo, root, summary="登录调查仍缺少独立证据")
            close(repo, other, summary="性能调查尚无恢复验证")
            close(repo, outside, summary="PRIVATE_OTHER_REPORT")
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                result = await client.get(
                    f"/v1/conversations/{root.investigation_id}/activity", headers=AUTH
                )
                assert result.status_code == 200
                data = result.json()
                assert {item["task_id"] for item in data["items"]} == {
                    root.investigation_id,
                    other.investigation_id,
                }
                assert len([item for item in data["items"] if item["kind"] == "user"]) == 2
                assert len([item for item in data["items"] if item["kind"] == "report"]) == 2
                assert "PRIVATE_OTHER" not in result.text
                foreign = await client.get(
                    f"/v1/conversations/{root.investigation_id}/activity",
                    headers={**AUTH, "X-Project-ID": "other"},
                )
                assert foreign.status_code == 404
                config = (await client.get("/v1/workspace", headers=AUTH)).json()
                assert config["coordination"]["semantic_router"] is False
                assert config["coordination"]["sources"] == []

    asyncio.run(scenario())


def test_activity_report_excerpt_is_marked_and_raw_constraints_are_not_exposed(settings):
    async def scenario():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            repo = app.state.container.repository
            app.state.container.workflows.start = Mock()
            root = create(repo)
            close(repo, root, summary="证据不足。" * 600)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                value = (
                    await client.get(
                        f"/v1/conversations/{root.investigation_id}/activity", headers=AUTH
                    )
                ).json()
                report = next(item for item in value["items"] if item["kind"] == "report")
                assert report["truncated"] and len(report["text"]) == 2000
                assert report["sha256"] and report["report_revision"] == 1

    asyncio.run(scenario())
