"""Background passes recover existing route requests, never start investigation work."""
import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from game_test_agent.api import create_app
from game_test_agent.events.router_recovery import RouterRecovery
from game_test_agent.storage import Repository
from game_test_agent.storage.repository import payload_hash
from game_test_agent.storage.workbench import WorkbenchStore
from test_conversation_tasks import linked, request
from test_route_evaluations import ORIGINAL, claim, service
from test_semantic_routing import assessment


def queued(repository, *, key="route-test", text=None):
    root, _ = repository.create_investigation(request(text or "CPU高负载"), key)
    sibling = linked(repository, root.investigation_id, key + "-child", text="登录崩溃")
    routing = service(repository, assessment(task=sibling.investigation_id))
    now = [100]
    routing.evaluations.clock = lambda: now[0]
    lease = claim(routing.evaluations, root.investigation_id, key=key)
    return routing, root, sibling, lease, now


def test_expired_request_recovers_without_client_retry(repository):
    async def scenario():
        routing, root, sibling, _, now = queued(repository)
        now[0] = 116
        counts = await routing.recover_due()
        assert counts == {"scanned": 1, "completed": 1, "skipped": 0, "unavailable": 0, "deferred": 0}
        receipt = routing.store.receipt(root.investigation_id, "tester", "route-test", payload_hash(ORIGINAL))
        assert receipt["route_decision"]["task_id"] == sibling.investigation_id
        assert receipt["route_decision"]["evaluation_generation"] == 2
        assert not repository.list_user_inputs(sibling.investigation_id)
        assert (await routing.recover_due())["scanned"] == 0
    asyncio.run(scenario())


def test_live_lease_not_claimed_and_cancel_backoff_respected(repository):
    async def scenario():
        routing, _, _, lease, now = queued(repository)
        assert (await routing.recover_due())["scanned"] == 0
        routing.evaluations.abandon(lease)
        now[0] = 104
        assert (await routing.recover_due())["scanned"] == 0
        now[0] = 105
        assert (await routing.recover_due())["completed"] == 1
    asyncio.run(scenario())


def test_disabled_model_makes_no_background_progress_or_calls(repository):
    async def scenario():
        routing, _, _, _, now = queued(repository)
        now[0] = 116
        assessor = routing.router.assessor
        routing.router.assessor = None
        assert (await routing.recover_due())["scanned"] == 0
        assessor.assess.assert_not_called()
    asyncio.run(scenario())


def test_archived_conversation_waits_until_restore(repository):
    async def scenario():
        routing, root, _, _, now = queued(repository)
        now[0] = 116
        store = WorkbenchStore(repository)
        store.update(root.investigation_id, "studio-a", "game-a", 0, archived=True)
        assert (await routing.recover_due())["scanned"] == 0
        store.update(root.investigation_id, "studio-a", "game-a", 1, archived=False)
        assert (await routing.recover_due())["completed"] == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("raw", ['{broken', '[]', '[' * 2000 + ']' * 2000, '{"content":"x"}', 'x' * 40001,
    json.dumps({"content": "other text", "context_investigation_id": None})])
def test_corrupt_source_is_quarantined_and_does_not_block_next_row(repository, raw):
    async def scenario():
        routing, root, _, _, now = queued(repository)
        repository._connection.execute("UPDATE conversation_route_evaluations SET original_json=?", (raw,))
        other, second, _, _, second_now = queued(repository, key="second")
        now[0] = second_now[0] = 116
        result = await routing.recover_due()
        assert result["unavailable"] == 1 and result["completed"] == 1
        row = repository._connection.execute("SELECT state,original_json FROM conversation_route_evaluations WHERE conversation_id=?", (root.investigation_id,)).fetchone()
        assert row["state"] == "unavailable" and row["original_json"] == raw
        assert routing.store.receipt(second.investigation_id, "tester", "second", payload_hash(ORIGINAL))
        # Only the intact source reached a model, never the corrupted one.
        routing.router.assessor.assess.assert_awaited_once()
    asyncio.run(scenario())


def test_batch_limit_and_source_metadata_bounds(repository):
    async def scenario():
        routing, _, _, _, now = queued(repository)
        for i in range(4):
            queued(repository, key=f"batch-{i}")
        now[0] = 116
        metadata = routing.evaluations.due(limit=2)
        assert len(metadata) == 2 and all("original_json" not in row for row in metadata)
        assert (await routing.recover_due(limit=2))["scanned"] == 2
        assert len(routing.evaluations.due(limit=16)) == 3
        for invalid in (0, 17):
            with pytest.raises(ValueError):
                routing.evaluations.due(limit=invalid)
    asyncio.run(scenario())


def test_stale_scan_cannot_take_over_fresh_generation(repository):
    async def scenario():
        routing, root, _, _, now = queued(repository)
        now[0] = 116
        candidate = routing.evaluations.due()[0]
        current = claim(routing.evaluations, root.investigation_id)
        routing.evaluations.abandon(current)
        routing.evaluations.due = Mock(return_value=[candidate])
        result = await routing.recover_due()
        assert result["skipped"] == 1
        routing.router.assessor.assess.assert_not_called()
        row = repository._connection.execute("SELECT generation,state FROM conversation_route_evaluations").fetchone()
        assert row["generation"] == 2 and row["state"] == "retryable"
    asyncio.run(scenario())


def test_stale_scan_rechecks_recent_release_in_same_generation(repository):
    async def scenario():
        routing, _, _, lease, now = queued(repository)
        now[0] = 116
        candidate = routing.evaluations.due()[0]
        routing.evaluations.abandon(lease)
        routing.evaluations.due = Mock(return_value=[candidate])
        result = await routing.recover_due()
        assert result["skipped"] == 1
        routing.router.assessor.assess.assert_not_called()
    asyncio.run(scenario())


def test_background_messages_keep_tenant_and_project_context_separate(repository):
    async def scenario():
        routing, first, _, _, now = queued(repository)
        other_request = request("另一项目的登录问题").model_copy(update={"tenant_id": "studio-b", "project_id": "game-b"})
        second, _ = repository.create_investigation(other_request, "other-scope")
        claim(routing.evaluations, second.investigation_id, key="other-scope")
        now[0] = 116
        seen = []

        async def assess(payload, scope):
            seen.append((scope, {card["task_id"] for card in payload["tasks"]}))
            return assessment("clarify", reference="")

        routing.router.assessor.assess.side_effect = assess
        assert (await routing.recover_due())["completed"] == 2
        assert len(seen) == 2
        for scope, ids in seen:
            if scope == ("studio-a", "game-a"):
                assert first.investigation_id in ids and second.investigation_id not in ids
            else:
                assert scope == ("studio-b", "game-b") and ids == {second.investigation_id}
    asyncio.run(scenario())


def test_sqlite_22_upgrade_preserves_pending_source_and_adds_scan_indexes(repository):
    routing, root, _, _, now = queued(repository)
    now[0] = 116
    before = dict(repository._connection.execute("SELECT * FROM conversation_route_evaluations").fetchone())
    with repository.transaction() as connection:
        connection.execute("DROP INDEX ix_route_evaluation_deadline")
        connection.execute("DROP INDEX ix_route_evaluation_retry")
        connection.execute("DELETE FROM schema_migrations WHERE version=23")
    upgraded = Repository(repository.path)
    try:
        assert upgraded.schema_version() == 24
        after = dict(upgraded._connection.execute("SELECT * FROM conversation_route_evaluations").fetchone())
        assert before == after
        indexes = {row["name"] for row in upgraded._connection.execute("PRAGMA index_list(conversation_route_evaluations)").fetchall()}
        assert {"ix_route_evaluation_deadline", "ix_route_evaluation_retry"} <= indexes
    finally:
        upgraded.close()


def test_error_deferral_cannot_release_new_owner(repository):
    routing, root, _, _, now = queued(repository)
    now[0] = 116
    candidate = routing.evaluations.due()[0]
    current = claim(routing.evaluations, root.investigation_id)
    routing.evaluations.defer_recovery(candidate, unavailable=True)
    row = repository._connection.execute("SELECT generation,state,owner_token FROM conversation_route_evaluations").fetchone()
    assert row["generation"] == current.generation and row["state"] == "running"
    assert row["owner_token"] == current.owner_token


def test_cancelled_pass_releases_current_generation(repository):
    async def scenario():
        routing, _, _, _, now = queued(repository)
        now[0] = 116
        routing.router.assessor.assess.side_effect = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await routing.recover_due()
        row = repository._connection.execute("SELECT generation,state FROM conversation_route_evaluations").fetchone()
        assert row["generation"] == 2 and row["state"] == "retryable"
    asyncio.run(scenario())


def test_restart_recovers_from_same_database(repository):
    async def scenario():
        _, root, sibling, _, _ = queued(repository)
        other = Repository(repository.path)
        try:
            recovered = service(other, assessment(task=sibling.investigation_id))
            recovered.evaluations.clock = lambda: 116
            assert (await recovered.recover_due())["completed"] == 1
            assert recovered.store.receipt(root.investigation_id, "tester", "route-test", payload_hash(ORIGINAL))
        finally:
            other.close()
    asyncio.run(scenario())


def test_exhausted_request_background_completes_without_model(repository):
    async def scenario():
        routing, root, _, lease, now = queued(repository)
        routing.evaluations.abandon(lease)
        for _ in range(2):
            current = claim(routing.evaluations, root.investigation_id)
            routing.evaluations.abandon(current)
        now[0] = 106
        assert (await routing.recover_due())["completed"] == 1
        routing.router.assessor.assess.assert_not_called()
        receipt = routing.store.receipt(root.investigation_id, "tester", "route-test", payload_hash(ORIGINAL))
        assert receipt["route_decision"]["basis"] == "assessment_exhausted"
    asyncio.run(scenario())


def test_transient_failure_defers_only_bad_row_and_keeps_pass_running(repository):
    async def scenario():
        routing, root, _, _, now = queued(repository)
        queued(repository, key="healthy")
        now[0] = 116
        real_route = routing.route

        async def fail_one(root_id, *args, **kwargs):
            if root_id == root.investigation_id:
                raise RuntimeError("SECRET infrastructure error")
            return await real_route(root_id, *args, **kwargs)

        routing.route = fail_one
        counts = await routing.recover_due()
        assert counts["deferred"] == 1 and counts["completed"] == 1
        assert "SECRET" not in str(counts)
        assert not routing.evaluations.due()
    asyncio.run(scenario())


def test_lifecycle_runner_retries_failed_pass_and_closes():
    async def scenario():
        called = asyncio.Event()
        fake = SimpleNamespace(recover_due=AsyncMock(side_effect=RuntimeError("SECRET")))
        runner = RouterRecovery(fake, enabled=True, poll_seconds=1)
        runner.start()
        first_task = runner.task
        runner.start()
        assert runner.task is first_task
        await asyncio.sleep(0)
        assert runner.status == "unavailable"
        fake.recover_due.side_effect = None

        async def succeeded(**kwargs):
            called.set()
            return {"scanned": 0}

        fake.recover_due.side_effect = succeeded
        await asyncio.wait_for(called.wait(), 2)
        assert runner.status == "running" and runner.last_pass == {"scanned": 0}
        await runner.close()
        assert runner.status == "stopped" and runner.task is None
    asyncio.run(scenario())


def test_app_starts_and_closes_recovery_only_when_configured(settings):
    async def scenario():
        configured = replace(settings, enable_model=True, enable_semantic_router=True)
        app = create_app(configured)
        app.state.routing_service.recover_due = AsyncMock(return_value={"scanned": 0})
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0)
            assert app.state.router_recovery.status == "running"
            app.state.routing_service.recover_due.assert_awaited_once()
        assert app.state.router_recovery.task is None
        disabled = create_app(replace(settings, database_path=settings.database_path.with_name("disabled.db")))
        async with disabled.router.lifespan_context(disabled):
            assert disabled.state.router_recovery.status == "disabled"
            assert disabled.state.router_recovery.task is None
    asyncio.run(scenario())


def test_recovery_can_be_disabled_separately_and_polling_is_finite(settings, monkeypatch):
    monkeypatch.setenv("GTA_ENABLE_ROUTER_RECOVERY", "false")
    assert not type(settings).from_env().enable_router_recovery
    for seconds in (float("nan"), float("inf"), 0):
        with pytest.raises(ValueError, match="poll interval"):
            replace(settings, dispatcher_poll_seconds=seconds).validate()
