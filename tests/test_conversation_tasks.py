"""Independent task routing, transactional intake, bounded cards and UI API isolation."""

import asyncio
import json
import sqlite3
from unittest.mock import Mock

import httpx
import pytest

from game_test_agent.api import create_app
from game_test_agent.contracts.models import (
    InvestigationRequest, TestObjective as Objective,
)
from game_test_agent.events.coordinator import ConversationCoordinator
from game_test_agent.reports import ReportService
from game_test_agent.reports.builder import ReportBuilder
from game_test_agent.storage import ConflictError, Repository
from game_test_agent.storage.workbench import WorkbenchStore


AUTH = {"X-Principal-ID": "tester", "X-Tenant-ID": "studio-a", "X-Project-ID": "game-a",
        "X-Scopes": "tests:read"}


def request(text="任务 A", **kwargs):
    return InvestigationRequest(tenant_id="studio-a", project_id="game-a",
                                objective=Objective(objective_id="check", description=text), **kwargs)


def linked(repository, root, key="task", **kwargs):
    return repository.create_investigation(request(**kwargs), key, principal_id="tester",
                                            scopes={"tests:read"}, conversation_root_id=root)[0]


def finish(repository, view):
    ReportService(repository).publish(ReportBuilder(repository).build(view.investigation_id, "test"))
    # Only test state setup; production transitions remain owned by the Worker.
    with repository.transaction() as connection:
        connection.execute("UPDATE investigations SET status='completed' WHERE investigation_id=?",
                           (view.investigation_id,))


def test_task_creation_is_atomic_with_link_and_start_outbox(repository):
    root, _ = repository.create_investigation(request(), "root")
    repository._connection.execute("""CREATE TRIGGER fail_link BEFORE INSERT ON conversation_task_links
        BEGIN SELECT RAISE(ABORT, 'interrupted'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        linked(repository, root.investigation_id)
    assert len(repository.list_investigations("studio-a", "game-a")) == 1
    assert len(repository.pending_workflow_starts()) == 1
    repository._connection.execute("DROP TRIGGER fail_link")
    task = linked(repository, root.investigation_id)
    assert len(repository.pending_workflow_starts()) == 2
    assert WorkbenchStore(repository).root(task.investigation_id, "studio-a", "game-a") == task.investigation_id
    assert repository._connection.execute("SELECT COUNT(*) FROM investigation_relations").fetchone()[0] == 0


def test_mapping_survives_restart_without_inheriting_permissions_or_constraints(settings):
    repository = Repository(settings.database_path)
    root, _ = repository.create_investigation(request(constraints={"private": "secret"}), "root",
                                              scopes={"runs:submit", "agent:ops"})
    task = linked(repository, root.investigation_id)
    repository.close()
    repository = Repository(settings.database_path)
    try:
        coordinator = ConversationCoordinator(repository)
        assert coordinator.route(root.investigation_id, task.investigation_id, "studio-a", "game-a")[0] == task.investigation_id
        assert repository.get_authorization(task.investigation_id)["scopes"] == ["tests:read"]
        assert repository.get_investigation(task.investigation_id).request.constraints == {}
        assert len(WorkbenchStore(repository).list("studio-a", "game-a")) == 1
    finally:
        repository.close()


def test_linked_retry_cannot_rebind_regular_or_other_conversation(repository):
    root, _ = repository.create_investigation(request(), "root")
    other, _ = repository.create_investigation(request(), "other")
    task = linked(repository, root.investigation_id)
    assert linked(repository, root.investigation_id).investigation_id == task.investigation_id
    with pytest.raises(ConflictError, match="different conversation"):
        linked(repository, other.investigation_id)


def test_cards_are_bounded_metadata_with_scoped_report_references(repository):
    root, _ = repository.create_investigation(request("原始任务"), "root")
    for i in range(24):
        task = linked(repository, root.investigation_id, str(i), text="读日志" * 900,
                      constraints={"never_inline": "CONSTRAINT_SECRET"})
        repository.record_investigation_event(task.investigation_id, "private.tool", {"raw": "TOOL_SECRET"})
        report = ReportBuilder(repository).build(task.investigation_id, "test", summary="报告" * 2000)
        ReportService(repository).publish(report)
    coordinator = ConversationCoordinator(repository)
    page = coordinator.cards(root.investigation_id, "studio-a", "game-a")
    raw = json.dumps(page, ensure_ascii=False).encode()
    assert len(page["items"]) == 20 and page["next_offset"] == 20
    assert len(raw) < 48_000
    assert b"TOOL_SECRET" not in raw and b"CONSTRAINT_SECRET" not in raw
    for card in page["items"]:
        assert len(card["objective_excerpt"].encode()) <= 600
        if card["report"]:
            assert len(card["report"]["summary_excerpt"].encode()) <= 800
            assert card["report"]["summary_truncated"]
            report, _, digest = repository.get_report(card["investigation_id"], card["report"]["revision"])
            assert digest == card["report"]["sha256"] and report.investigation_id == card["investigation_id"]
    tail = coordinator.cards(root.investigation_id, "studio-a", "game-a", offset=20)
    assert len(tail["items"]) == 5 and tail["next_offset"] is None
    assert not ({c["task_id"] for c in page["items"]} & {c["task_id"] for c in tail["items"]})


def test_task_stream_version_tracks_siblings_but_not_other_conversations(repository):
    root, _ = repository.create_investigation(request(), "root")
    sibling = linked(repository, root.investigation_id)
    other, _ = repository.create_investigation(request(), "other")
    store = WorkbenchStore(repository)
    before = store.task_version(root.investigation_id, "studio-a", "game-a")
    repository.record_investigation_event(other.investigation_id, "test", {})
    assert before == store.task_version(root.investigation_id, "studio-a", "game-a")
    repository.record_investigation_event(sibling.investigation_id, "test", {})
    assert before != store.task_version(root.investigation_id, "studio-a", "game-a")


@pytest.mark.parametrize("header", ["X-Tenant-ID", "X-Project-ID"])
def test_task_api_rejects_foreign_scope(settings, header):
    async def scenario():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            repo = app.state.container.repository
            root, _ = repo.create_investigation(request(), "root")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                path = f"/v1/conversations/{root.investigation_id}"
                foreign = {**AUTH, header: "other"}
                assert (await client.get(path + "/tasks", headers=foreign)).status_code == 404
                assert (await client.post(path + "/tasks", headers={**foreign, "Idempotency-Key": "x"},
                                          json=request().model_dump(mode="json"))).status_code == 403
                assert len(repo.list_investigations("studio-a", "game-a")) == 1
    asyncio.run(scenario())


def test_directed_input_uses_own_task_head_and_never_broadcasts(settings):
    async def scenario():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            repo = app.state.container.repository
            app.state.container.workflows.start = Mock()
            root, _ = repo.create_investigation(request(), "root")
            sibling = linked(repo, root.investigation_id, text="任务 B")
            outsider, _ = repo.create_investigation(request(), "outsider")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                path = f"/v1/conversations/{root.investigation_id}"
                body = {"content": "只给 A", "expected_investigation_id": root.investigation_id}
                response = await client.post(path + "/messages", headers={**AUTH, "Idempotency-Key": "input"}, json=body)
                assert response.status_code == 202
                assert len(repo.list_user_inputs(root.investigation_id)) == 1
                assert not repo.list_user_inputs(sibling.investigation_id)
                assert (await client.post(path + "/messages", headers={**AUTH, "Idempotency-Key": "input"}, json=body)).json()["duplicate"]
                assert (await client.post(path + "/messages", headers={**AUTH, "Idempotency-Key": "wrong"},
                                          json={**body, "expected_investigation_id": outsider.investigation_id})).status_code == 404
                snapshot = (await client.get(path + f"/workspace?task_id={sibling.investigation_id}", headers=AUTH)).json()
                assert snapshot["task_head_id"] == sibling.investigation_id
                assert {x["investigation"]["investigation_id"] for x in snapshot["investigations"]} == {sibling.investigation_id}
                assert (await client.get(path + f"/workspace?task_id={outsider.investigation_id}", headers=AUTH)).status_code == 404
                assert (await client.get(path + f"/workspace?task_id={sibling.investigation_id}&before={root.investigation_id}", headers=AUTH)).status_code == 404
    asyncio.run(scenario())


def test_followups_are_separate_lineages_and_reject_only_their_own_stale_head(settings):
    async def scenario():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            repo = app.state.container.repository
            app.state.container.workflows.start = Mock()
            root, _ = repo.create_investigation(request("目标 A"), "root")
            sibling = linked(repo, root.investigation_id, text="目标 B")
            finish(repo, root)
            finish(repo, sibling)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                path = f"/v1/conversations/{root.investigation_id}"
                ids = []
                for i, parent in enumerate((root, sibling)):
                    body = {"content": f"继续 {i}", "expected_investigation_id": parent.investigation_id}
                    response = await client.post(path + "/messages", headers={**AUTH, "Idempotency-Key": str(i)}, json=body)
                    assert response.status_code == 202
                    child = response.json()["investigation"]
                    ids.append(child["investigation_id"])
                    assert child["request"]["constraints"]["followup_reference"]["root_objective"] == parent.request.objective.description
                    stale = await client.post(path + "/messages", headers={**AUTH, "Idempotency-Key": "stale" + str(i)}, json=body)
                    assert stale.status_code == 409
                for parent, child in zip((root, sibling), ids):
                    data = (await client.get(path + f"/workspace?task_id={parent.investigation_id}", headers=AUTH)).json()
                    assert data["task_head_id"] == child
                    assert len(data["investigations"]) == 2
                cards = (await client.get(path + "/tasks", headers=AUTH)).json()["items"]
                assert {c["investigation_id"] for c in cards} == set(ids)
    asyncio.run(scenario())


def test_task_intake_retry_after_dispatch_error_and_archive(settings):
    async def scenario():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            repo = app.state.container.repository
            root, _ = repo.create_investigation(request(), "root")
            app.state.container.workflows.start = Mock(side_effect=[RuntimeError("lost dispatch"), None, None])
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                path = f"/v1/conversations/{root.investigation_id}"
                headers = {**AUTH, "Idempotency-Key": "new"}
                body = request("独立新任务").model_dump(mode="json")
                with pytest.raises(RuntimeError, match="lost dispatch"):
                    await client.post(path + "/tasks", headers=headers, json=body)
                response = await client.post(path + "/tasks", headers=headers, json=body)
                assert response.status_code == 202 and response.json()["duplicate"]
                assert len(repo.list_investigations("studio-a", "game-a")) == 2
                assert len(repo.pending_workflow_starts()) == 2
                assert (await client.post(path + "/tasks", headers=headers,
                                          json=request("changed").model_dump(mode="json"))).status_code == 409
                assert len((await client.get("/v1/conversations", headers=AUTH)).json()["items"]) == 1
                assert len((await client.get("/v1/conversations?query=独立新任务", headers=AUTH)).json()["items"]) == 1
                await client.patch(path, headers=AUTH, json={"expected_revision": 0, "archived": True})
                assert (await client.post(path + "/tasks", headers=headers, json=body)).json()["duplicate"]
                assert (await client.post(path + "/tasks", headers={**AUTH, "Idempotency-Key": "after-archive"}, json=body)).status_code == 409
    asyncio.run(scenario())


def test_version_seventeen_upgrade_preserves_original_conversation(settings):
    repo = Repository(settings.database_path)
    root, _ = repo.create_investigation(request(), "root")
    with repo.transaction() as connection:
        connection.execute("DROP TABLE conversation_task_links")
        connection.execute("DELETE FROM schema_migrations WHERE version=18")
    repo.close()
    repo = Repository(settings.database_path)
    try:
        assert repo.schema_version() == 24
        assert len(WorkbenchStore(repo).list("studio-a", "game-a")) == 1
        task = linked(repo, root.investigation_id)
        assert WorkbenchStore(repo).get(task.investigation_id, "studio-a", "game-a")["conversation_id"] == root.investigation_id
    finally:
        repo.close()


def test_stream_notifies_sibling_changes_without_streaming_worker_payload(settings):
    async def scenario():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            repo = app.state.container.repository
            app.state.container.workflows.start = Mock()
            root, _ = repo.create_investigation(request(), "root")
            sibling = linked(repo, root.investigation_id)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                path = f"/v1/conversations/{root.investigation_id}/stream?seconds=1"
                initial = await client.get(path, headers=AUTH)
                version = initial.text.split("id: ")[1].split("\n")[0]
                repo.record_investigation_event(sibling.investigation_id, "private.raw", {"raw": "PRIVATE_WORKER_LOG"})
                update = await client.get(path + f"&after={version}", headers=AUTH)
                assert "event: update" in update.text and "PRIVATE_WORKER_LOG" not in update.text
                assert version != update.text.split("id: ")[1].split("\n")[0]
    asyncio.run(scenario())
