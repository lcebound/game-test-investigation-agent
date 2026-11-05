"""Conversation-wide state is a bounded projection, never a Worker or approval owner."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import httpx
import pytest

from game_test_agent.analysis.permissions import CodePermissionStore
from game_test_agent.api import create_app
from game_test_agent.contracts.models import CodeExecutionRequest, InvestigationRequest, ReopenRequest, Resolution
from game_test_agent.events.coordinator import ConversationCoordinator
from game_test_agent.reports import ReportService
from game_test_agent.reports.builder import ReportBuilder
from game_test_agent.storage import NotFoundError, Repository, canonical_json
from game_test_agent.storage.workbench import WorkbenchStore


AUTH = {"X-Principal-ID": "tester", "X-Tenant-ID": "studio-a", "X-Project-ID": "game-a", "X-Scopes": "tests:read"}


def request(text="测试任务", **kwargs):
    return InvestigationRequest(tenant_id="studio-a", project_id="game-a",
        objective={"objective_id": "verify", "description": text}, **kwargs)


def create(repository, key="root", *, root=None, text="测试任务"):
    return repository.create_investigation(request(text), key, conversation_root_id=root)[0]


def state(repository, view, status, resolution=None):
    with repository.transaction() as connection:
        connection.execute("UPDATE investigations SET status=?,resolution=?,updated_at=? WHERE investigation_id=?",
            (status, resolution, datetime.now(timezone.utc).isoformat(), view.investigation_id))


def close(repository, view, status="completed", summary="有限调查结果"):
    resolution = Resolution.SYSTEM_ERROR if status == "failed" else Resolution.CANCELED if status == "canceled" else Resolution.INCONCLUSIVE
    report = ReportBuilder(repository).build(view.investigation_id, "test", status=status, resolution=resolution, summary=summary)
    ReportService(repository).publish(report)
    state(repository, view, status, report.resolution.value)


def approval(repository, view, identity="job"):
    return CodePermissionStore(repository).propose(view.investigation_id, "decision", identity,
        CodeExecutionRequest(language="python", code="print('PROGRAM_SECRET')", purpose="PERMISSION_SECRET",
            input_evidence_ids=["fixture-evidence"], permissions={"network": True, "reason": "PERMISSION_SECRET"}),
        "runtime", {})


def overview(repository, root, **kwargs):
    return ConversationCoordinator(repository).overview(root.investigation_id, "studio-a", "game-a", **kwargs)


def test_anchor_completion_does_not_hide_a_waiting_sibling(repository):
    root = create(repository)
    close(repository, root)
    sibling = create(repository, "sibling", root=root.investigation_id)
    state(repository, sibling, "waiting_input")
    summary = WorkbenchStore(repository).get(root.investigation_id, "studio-a", "game-a")
    assert summary["latest_investigation"].investigation_id == root.investigation_id
    assert summary["latest_investigation"].status == "completed"
    assert summary["investigation_count"] == 2 and summary["anchor_investigation_count"] == 1
    totals = summary["task_summary"]
    assert totals["task_count"] == 2 and totals["waiting_input_tasks"] == 1
    assert totals["state"] == "needs_attention" and not totals["all_terminal"]
    assert overview(repository, root)["attention"][0]["task_id"] == sibling.investigation_id
    assert WorkbenchStore(repository).list("studio-a", "game-a", root_id=sibling.investigation_id)[0]["conversation_id"] == root.investigation_id


def test_sibling_activity_moves_the_whole_conversation_in_history(repository):
    root = create(repository)
    sibling = create(repository, "sibling", root=root.investigation_id)
    later = create(repository, "later")
    store = WorkbenchStore(repository)
    assert store.list("studio-a", "game-a")[0]["conversation_id"] == later.investigation_id
    repository.record_investigation_event(sibling.investigation_id, "new.progress", {"private": "RAW_LOG_SECRET"})
    assert store.list("studio-a", "game-a")[0]["conversation_id"] == root.investigation_id
    assert len(store.list("studio-a", "game-a")) == 2


def test_only_current_heads_count_as_failed_but_old_cleanup_remains_visible(repository):
    root = create(repository)
    close(repository, root, "failed")
    repository.record_resource_cleanup(root.investigation_id, "old-sandbox", resource_kind="sandbox", cleanup_owner="fixture")
    child, _ = repository.reopen_investigation(root.investigation_id,
        ReopenRequest(reason="重新调查", budget=root.request.budget), "reopen")
    page = overview(repository, root)
    totals = page["task_summary"]
    assert totals["task_count"] == 1 and totals["investigation_count"] == 2
    assert totals["failed_tasks"] == 0 and totals["active_tasks"] == 1
    assert totals["pending_cleanup_resources"] == 1 and totals["attention_tasks"] == 1
    card = page["attention"][0]
    assert card["investigation_id"] == child.investigation_id
    assert card["cleanup_investigation_id"] == root.investigation_id and card["attention"] == ["cleanup"]
    prior = page["version"]
    repository.resolve_cleanup("old-sandbox")
    assert overview(repository, root)["version"] != prior
    assert overview(repository, root)["task_summary"]["attention_tasks"] == 0


def test_multiple_attention_reasons_count_one_task_and_never_claim_a_report(repository):
    root = create(repository)
    state(repository, root, "failed", "system_error")
    repository.record_resource_cleanup(root.investigation_id, "unconfirmed", resource_kind="directory", cleanup_owner="fixture")
    page = overview(repository, root)
    assert page["task_summary"]["attention_tasks"] == 1
    assert set(page["attention"][0]["attention"]) == {"execution_failed", "cleanup", "missing_report"}
    assert page["task_summary"]["all_terminal"] and page["task_summary"]["state"] == "needs_attention"
    assert not page["results"]
    goal = create(repository, "goal", root=root.investigation_id)
    state(repository, goal, "completed", "goal_failed")
    second = overview(repository, root)
    assert second["task_summary"]["attention_tasks"] == 2
    assert second["task_summary"]["goal_failed_tasks"] == second["task_summary"]["failed_tasks"] == 1
    goal_card = next(card for card in second["attention"] if card["task_id"] == goal.investigation_id)
    assert "goal_failed" in goal_card["attention"] and "execution_failed" not in goal_card["attention"]


@pytest.mark.parametrize("invalid", ["expired", "superseded", "terminal"])
def test_effectively_invalid_permissions_are_not_presented_as_awaiting_decision(repository, invalid):
    root = create(repository)
    record = approval(repository, root)
    assert overview(repository, root)["task_summary"]["approval_tasks"] == 1
    if invalid == "expired":
        repository._connection.execute("UPDATE code_permission_requests SET expires_at=? WHERE approval_id=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), record["approval_id"]))
    elif invalid == "superseded":
        repository.enqueue_command(root.investigation_id, "new-input", "input", {"content": "改查其他范围"})
    else:
        state(repository, root, "canceled", "canceled")
    cursor = repository.latest_event_cursor(root.investigation_id)
    page = overview(repository, root)
    assert page["task_summary"]["approval_tasks"] == 0
    assert repository._connection.execute("SELECT state FROM code_permission_requests WHERE approval_id=?", (record["approval_id"],)).fetchone()[0] == "pending"
    assert repository.latest_event_cursor(root.investigation_id) == cursor
    assert "PROGRAM_SECRET" not in canonical_json(page) and "PERMISSION_SECRET" not in canonical_json(page)


def test_overview_is_read_only_and_does_not_merge_worker_material(repository):
    root = create(repository)
    approval(repository, root)
    repository.enqueue_command(root.investigation_id, "input", "input", {"content": "USER_INPUT_SECRET"})
    repository.record_investigation_event(root.investigation_id, "private.raw", {"result": "RAW_LOG_SECRET"})
    events = repository.latest_event_cursor(root.investigation_id)
    starts = repository.pending_workflow_starts()
    page = overview(repository, root)
    for secret in ("USER_INPUT_SECRET", "RAW_LOG_SECRET", "PROGRAM_SECRET", "PERMISSION_SECRET"):
        assert secret not in canonical_json(page)
    assert repository.latest_event_cursor(root.investigation_id) == events
    assert repository.pending_workflow_starts() == starts
    assert repository.budget_usage(root.investigation_id).get("native_model_turns", 0) == 0


def test_overview_is_byte_bounded_and_paginates_without_losing_results(repository):
    root = create(repository, text="边界" * 1500)
    views = [root] + [create(repository, f"task-{i}", root=root.investigation_id, text="边界" * 1500) for i in range(22)]
    for view in views:
        close(repository, view, "failed", summary="长报告" * 2000)
    attention_seen, result_seen = set(), set()
    attention_offset = result_offset = 0
    for _ in range(30):
        page = overview(repository, root, attention_offset=attention_offset, result_offset=result_offset, limit=10)
        assert len(canonical_json(page).encode()) <= 16384
        for key, seen in (("attention", attention_seen), ("results", result_seen)):
            ids = {item["task_id"] for item in page[key]}
            assert not seen.intersection(ids)
            seen.update(ids)
        if page["next_attention_offset"] is None and page["next_result_offset"] is None:
            break
        attention_offset = page["next_attention_offset"] if page["next_attention_offset"] is not None else 23
        result_offset = page["next_result_offset"] if page["next_result_offset"] is not None else 23
    assert attention_seen == result_seen == {v.investigation_id for v in views}


def test_recent_results_sort_by_activity_instead_of_failure_priority(repository):
    root = create(repository)
    close(repository, root, "failed")
    newer = create(repository, "newer", root=root.investigation_id)
    close(repository, newer)
    page = overview(repository, root)
    assert page["results"][0]["task_id"] == newer.investigation_id
    assert page["task_summary"]["inconclusive_tasks"] == 1
    assert page["task_summary"]["failed_tasks"] == 1


def test_cleanup_resolution_without_an_investigation_event_changes_the_stream_version(repository):
    root = create(repository)
    close(repository, root)
    repository.record_resource_cleanup(root.investigation_id, "job", resource_kind="sandbox", cleanup_owner="fixture")
    before = overview(repository, root)
    event_cursor = repository.latest_event_cursor(root.investigation_id)
    repository.resolve_cleanup("job")
    after = overview(repository, root)
    assert repository.latest_event_cursor(root.investigation_id) == event_cursor
    assert before["version"] != after["version"]
    assert after["task_summary"]["state"] == "finished"


@pytest.mark.parametrize("header", ["X-Project-ID", "X-Tenant-ID"])
def test_overview_api_scope_and_read_only_navigation(settings, header):
    async def scenario():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            repo = app.state.container.repository
            app.state.container.workflows.start = Mock()
            root = create(repo)
            sibling = create(repo, "sibling", root=root.investigation_id)
            state(repo, sibling, "waiting_input")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                path = f"/v1/conversations/{root.investigation_id}/overview"
                own = await client.get(path, headers=AUTH)
                assert own.status_code == 200 and own.json()["task_summary"]["task_count"] == 2
                assert (await client.get(path, headers={**AUTH, header: "other"})).status_code == 404
                assert (await client.get(path + "?limit=11", headers=AUTH)).status_code == 422
                app.state.container.workflows.start.assert_not_called()
    asyncio.run(scenario())


def test_sse_reports_whole_conversation_state_and_refreshes_for_cleanup_without_events(settings):
    async def scenario():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            repo = app.state.container.repository
            app.state.container.workflows.start = Mock()
            root = create(repo)
            close(repo, root)
            sibling = create(repo, "sibling", root=root.investigation_id)
            repo.record_resource_cleanup(root.investigation_id, "old", resource_kind="sandbox", cleanup_owner="fixture")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                path = f"/v1/conversations/{root.investigation_id}/stream?seconds=1"
                first = await client.get(path, headers=AUTH)
                data = json.loads(first.text.split("data: ")[1].split("\n")[0])
                assert data["status"] == "completed" and data["status_scope"] == "anchor_investigation"
                assert data["task_summary"]["active_tasks"] == 1
                assert data["task_summary"]["attention_tasks"] == 1
                version = data["version"]
                repo.resolve_cleanup("old")
                second = await client.get(path + f"&after={version}", headers=AUTH)
                assert "event: update" in second.text
                next_data = json.loads(second.text.split("data: ")[1].split("\n")[0])
                assert next_data["task_summary"]["state"] == "running"
                assert repo.get_investigation(sibling.investigation_id).status == "accepted"
    asyncio.run(scenario())


def test_project_scope_applies_to_totals_and_task_lookup(repository):
    root = create(repository)
    foreign, _ = repository.create_investigation(request().model_copy(update={"tenant_id": "foreign"}), "foreign")
    store = WorkbenchStore(repository)
    assert len(store.list("studio-a", "game-a")) == 1
    with pytest.raises(NotFoundError):
        ConversationCoordinator(repository).overview(root.investigation_id, "foreign", "game-a")
    assert overview(repository, root)["task_summary"]["task_count"] == 1
    assert foreign.investigation_id != root.investigation_id


def test_overview_rebuilds_after_restart_without_a_second_state_copy(repository, settings):
    root = create(repository)
    sibling = create(repository, "sibling", root=root.investigation_id)
    state(repository, sibling, "waiting_input")
    repository.record_resource_cleanup(root.investigation_id, "persistent-job", resource_kind="sandbox", cleanup_owner="fixture")
    before = overview(repository, root)
    repository.close()
    restarted = Repository(settings.database_path)
    try:
        assert overview(restarted, root) == before
    finally:
        restarted.close()
