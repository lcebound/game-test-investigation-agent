"""Task addressing is bounded, scoped, retry stable and separate from execution."""
import asyncio
import json
from unittest.mock import Mock

import httpx
import pytest

from game_test_agent.api import create_app
from game_test_agent.contracts.models import ReopenRequest
from game_test_agent.events.message_router import ConversationMessageRouter
from game_test_agent.storage import NotFoundError
from game_test_agent.storage.workbench import WorkbenchStore
from test_conversation_tasks import AUTH, finish, linked, request


def resolve(repository, root, content, context=None):
    return ConversationMessageRouter(repository).resolve(root.investigation_id, content,
        "studio-a", "game-a", context_investigation_id=context.investigation_id if context else None)


@pytest.mark.parametrize("text", ['继续「GPU卡顿」：换成低画质', '关于 GPU卡顿：换成低画质',
                                 '继续任务 GPU卡顿：换成低画质', 'for "GPU卡顿": lower quality'])
def test_named_address_overrides_current_context_without_broadcast(repository, text):
    root, _ = repository.create_investigation(request("CPU高负载"), "root")
    sibling = linked(repository, root.investigation_id, text="GPU卡顿")
    result = resolve(repository, root, text, root)
    assert result["kind"] == "task" and result["task_id"] == sibling.investigation_id
    assert result["investigation_id"] == sibling.investigation_id
    assert not repository.list_user_inputs(root.investigation_id)
    assert not repository.list_user_inputs(sibling.investigation_id)


@pytest.mark.parametrize("text", ["继续", "补充一下，发生在战斗场景", "不要新建任务：补查恢复", "日志中提到了新任务：并非指令"])
def test_ordinary_followups_keep_the_current_task(repository, text):
    root, _ = repository.create_investigation(request("CPU高负载"), "root")
    sibling = linked(repository, root.investigation_id, text="GPU卡顿")
    result = resolve(repository, root, text, sibling)
    assert result["basis"] == "current_task" and result["task_id"] == sibling.investigation_id


@pytest.mark.parametrize("text", ["新任务：调查登录崩溃", "另开一个任务：调查登录崩溃", "new task: 调查登录崩溃"])
def test_explicit_new_task_returns_a_goal_without_starting_anything(repository, text):
    root, _ = repository.create_investigation(request(), "root")
    result = resolve(repository, root, text, root)
    assert result["kind"] == "new_task" and result["goal"] == "调查登录崩溃"
    assert len(repository.list_investigations("studio-a", "game-a")) == 1


@pytest.mark.parametrize("text", ['继续「卡顿」：补充', '继续「不存在」：补充', '继续「GPU卡顿：补充', "新任务："])
def test_ambiguous_or_broken_address_never_silently_falls_back(repository, text):
    root, _ = repository.create_investigation(request("CPU卡顿"), "root")
    linked(repository, root.investigation_id, text="GPU卡顿")
    assert resolve(repository, root, text, root)["kind"] == "clarify"


def test_single_task_default_and_multiple_task_ambiguity(repository):
    root, _ = repository.create_investigation(request(), "root")
    assert resolve(repository, root, "补充")['basis'] == "only_task"
    linked(repository, root.investigation_id)
    assert resolve(repository, root, "补充")["kind"] == "clarify"


def test_current_context_does_not_silently_advance_a_stale_head(repository):
    root, _ = repository.create_investigation(request("CPU卡顿"), "root")
    finish(repository, root)
    child, _ = repository.reopen_investigation(root.investigation_id,
        ReopenRequest(reason="继续核对", budget=root.request.budget, objective={"objective_id": "check", "description": "核对恢复"}),
        "followup", principal_id="tester", scopes=set())
    assert resolve(repository, root, "继续", root)["investigation_id"] == root.investigation_id
    named = resolve(repository, root, '继续「CPU卡顿」：增加对照', root)
    assert named["investigation_id"] == child.investigation_id
    by_id = resolve(repository, root, f"继续 {root.investigation_id}：增加对照", child)
    assert by_id["task_id"] == root.investigation_id and by_id["investigation_id"] == child.investigation_id


def test_task_directory_bound_cannot_turn_partial_matching_into_uniqueness(repository):
    root, _ = repository.create_investigation(request("目标 CPU卡顿"), "root")
    for i in range(21):
        linked(repository, root.investigation_id, str(i), text="其他" + str(i))
    result = resolve(repository, root, '继续「目标 CPU卡顿」：补充', root)
    assert result["kind"] == "clarify" and result["more_tasks"]
    assert resolve(repository, root, f"继续 {root.investigation_id}：补充", root)["kind"] == "task"
    assert len(json.dumps(result, ensure_ascii=False).encode()) < 8192


def test_router_never_loads_worker_events_reports_or_artifacts(repository, monkeypatch):
    root, _ = repository.create_investigation(request("CPU卡顿", constraints={"private": "SECRET"}), "root")
    sibling = linked(repository, root.investigation_id, text="GPU卡顿")
    repository.record_investigation_event(sibling.investigation_id, "private", {"raw": "SECRET"})
    monkeypatch.setattr(repository, "get_latest_report", Mock(side_effect=AssertionError("no report body")))
    monkeypatch.setattr(repository, "list_recent_events", Mock(side_effect=AssertionError("no events")))
    result = resolve(repository, root, '继续「GPU卡顿」：补充', root)
    assert result["task_id"] == sibling.investigation_id and "SECRET" not in json.dumps(result)


def test_foreign_context_is_rejected_even_for_an_explicit_new_task(repository):
    root, _ = repository.create_investigation(request(), "root")
    outsider, _ = repository.create_investigation(request(), "outside")
    with pytest.raises(NotFoundError):
        resolve(repository, root, "新任务：调查崩溃", outsider)
    guessed = resolve(repository, root, f"继续 {outsider.investigation_id}：补充", root)
    assert guessed["kind"] == "clarify" and not guessed["choices"]


@pytest.mark.parametrize("scope_header", ["X-Tenant-ID", "X-Project-ID"])
def test_route_api_rejects_foreign_scope(settings, scope_header):
    async def scenario():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            repo = app.state.container.repository
            root, _ = repo.create_investigation(request(), "root")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                result = await client.post(f"/v1/conversations/{root.investigation_id}/messages/route",
                    headers={**AUTH, scope_header: "other", "Idempotency-Key": "route"}, json={"content": "继续"})
                assert result.status_code == 404
    asyncio.run(scenario())


def test_route_receipt_is_stable_then_existing_message_api_delivers_once(settings):
    async def scenario():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            repo = app.state.container.repository
            app.state.container.workflows.start = Mock()
            root, _ = repo.create_investigation(request("CPU卡顿"), "root")
            sibling = linked(repo, root.investigation_id, text="GPU卡顿")
            path = f"/v1/conversations/{root.investigation_id}"
            headers = {**AUTH, "Idempotency-Key": "stable"}
            body = {"content": '继续「GPU卡顿」：补充低画质对照', "context_investigation_id": root.investigation_id}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                first = await client.post(path + "/messages/route", headers=headers, json=body)
                assert first.status_code == 200, first.text
                route = first.json()
                assert route["task_id"] == sibling.investigation_id and not route["duplicate"]
                assert not repo.list_user_inputs(sibling.investigation_id)
                app.state.container.workflows.start.assert_not_called()
                directed = {"content": body["content"], "expected_investigation_id": route["investigation_id"]}
                assert (await client.post(path + "/messages", headers=headers, json=directed)).status_code == 202
                assert (await client.post(path + "/messages", headers=headers, json=directed)).json()["duplicate"]
                again = await client.post(path + "/messages/route", headers=headers, json=body)
                assert again.json()["task_id"] == route["task_id"] and again.json()["duplicate"]
                assert len(repo.list_user_inputs(sibling.investigation_id)) == 1
                assert not repo.list_user_inputs(root.investigation_id)
                mismatch = await client.post(path + "/messages/route", headers=headers, json={**body, "content": "新任务：别的调查"})
                assert mismatch.status_code == 409
    asyncio.run(scenario())


def test_route_retry_keeps_original_head_and_new_request_can_resolve_the_new_head(settings):
    async def scenario():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            repo = app.state.container.repository
            app.state.container.workflows.start = Mock()
            root, _ = repo.create_investigation(request("CPU卡顿"), "root")
            path = f"/v1/conversations/{root.investigation_id}"
            headers = {**AUTH, "Idempotency-Key": "old"}
            body = {"content": '继续「CPU卡顿」：补充'}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                before = (await client.post(path + "/messages/route", headers=headers, json=body)).json()
                finish(repo, root)
                child, _ = repo.reopen_investigation(root.investigation_id, ReopenRequest(reason="继续", budget=root.request.budget), "new", principal_id="tester", scopes=set())
                replay = (await client.post(path + "/messages/route", headers=headers, json=body)).json()
                assert before["investigation_id"] == replay["investigation_id"] == root.investigation_id
                stale = await client.post(path + "/messages", headers=headers,
                    json={"content": body["content"], "expected_investigation_id": replay["investigation_id"]})
                assert stale.status_code == 409
                latest = (await client.post(path + "/messages/route", headers={**headers, "Idempotency-Key": "fresh"}, json=body)).json()
                assert latest["investigation_id"] == child.investigation_id
    asyncio.run(scenario())


def test_similar_topic_in_an_owned_followup_is_left_to_its_worker(repository):
    root, _ = repository.create_investigation(request("CPU卡顿"), "root")
    sibling = linked(repository, root.investigation_id, text="GPU卡顿")
    result = resolve(repository, root, "CPU卡顿也可能有关，请检查当前任务里的反证", sibling)
    assert result["task_id"] == sibling.investigation_id and result["basis"] == "current_task"


def test_new_task_route_retains_original_source_and_does_not_duplicate_acceptance(settings):
    async def scenario():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            repo = app.state.container.repository
            app.state.container.workflows.start = Mock()
            root, _ = repo.create_investigation(request(), "root")
            path = f"/v1/conversations/{root.investigation_id}"
            headers = {**AUTH, "Idempotency-Key": "new-request"}
            original = "新任务：调查崩溃，保留原话，不推断修复方案"
            body = {"content": original, "context_investigation_id": root.investigation_id}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                route = (await client.post(path + "/messages/route", headers=headers, json=body)).json()
                row = repo._connection.execute("SELECT response_json FROM workbench_message_receipts WHERE request_key=?", (route["route_id"],)).fetchone()
                receipt = json.loads(row[0])
                assert receipt["original_message"] == body
                assert receipt["route_decision"]["trigger_id"] == route["route_id"]
                task = request(route["goal"]).model_dump(mode="json")
                first = await client.post(path + "/tasks", headers=headers, json=task)
                again = await client.post(path + "/tasks", headers=headers, json=task)
                assert first.status_code == again.status_code == 202
                assert first.json()["task_id"] == again.json()["task_id"] and again.json()["duplicate"]
                assert len(repo.list_investigations("studio-a", "game-a")) == 2
    asyncio.run(scenario())


@pytest.mark.parametrize("content", ["补充", "新任务：登录崩溃"])
def test_old_route_decision_cannot_bypass_a_later_archive(settings, content):
    async def scenario():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            repo = app.state.container.repository
            app.state.container.workflows.start = Mock()
            root, _ = repo.create_investigation(request(), "root")
            path = f"/v1/conversations/{root.investigation_id}"
            headers = {**AUTH, "Idempotency-Key": "before-archive"}
            body = {"content": content, "context_investigation_id": root.investigation_id}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                route = (await client.post(path + "/messages/route", headers=headers, json=body)).json()
                WorkbenchStore(repo).update(root.investigation_id, "studio-a", "game-a", 0, archived=True)
                replay = await client.post(path + "/messages/route", headers=headers, json=body)
                assert replay.status_code == 200 and replay.json()["duplicate"]
                destination = "/tasks" if route["kind"] == "new_task" else "/messages"
                payload = request(route["goal"]).model_dump(mode="json") if route["kind"] == "new_task" else {
                    "content": content, "expected_investigation_id": route["investigation_id"]}
                rejected = await client.post(path + destination, headers=headers, json=payload)
                assert rejected.status_code == 409
                assert not repo.list_user_inputs(root.investigation_id)
    asyncio.run(scenario())


def test_followup_text_mentioning_another_topic_does_not_rename_its_task(repository):
    root, _ = repository.create_investigation(request("CPU卡顿"), "root")
    finish(repository, root)
    child, _ = repository.reopen_investigation(root.investigation_id,
        ReopenRequest(reason="比较 GPU", budget=root.request.budget,
            objective={"objective_id": "check", "description": "和 GPU 问题比较"}), "next")
    assert resolve(repository, root, '继续「GPU」：补充', child)["kind"] == "clarify"
