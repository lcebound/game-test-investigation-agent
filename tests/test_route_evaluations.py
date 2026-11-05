"""Durable evaluation ownership survives request cancellation and process replacement."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from game_test_agent.api import create_app
from game_test_agent.events.router_service import ConversationRoutingService
from game_test_agent.events.semantic_router import SemanticConversationRouter
from game_test_agent.storage import ConflictError, Repository
from game_test_agent.storage.repository import payload_hash
from game_test_agent.storage.route_evaluations import RouteEvaluationBusy, RouteEvaluationStore
from test_conversation_tasks import AUTH, linked, request
from test_semantic_routing import assessment


ORIGINAL = {"content": "回到刚才那个登录问题，补充日志", "context_investigation_id": None}


def claim(store, root, *, key="route-test", original=ORIGINAL, policy="a" * 64, principal="tester"):
    with store.repository.transaction() as connection:
        store.repository._lock_investigation_row(connection, root)
        return store.claim_in_transaction(connection, root, principal, key,
            payload_hash(original), original, policy, 10)


def finish(store, lease):
    with store.repository.transaction() as connection:
        store.finish_in_transaction(connection, lease)


def test_request_is_persisted_before_external_assessment(repository):
    root, _ = repository.create_investigation(request(), "root")
    store = RouteEvaluationStore(repository, clock=lambda: 100)
    lease = claim(store, root.investigation_id)
    row = repository._connection.execute("SELECT * FROM conversation_route_evaluations").fetchone()
    assert row["original_json"] and row["payload_hash"] == payload_hash(ORIGINAL)
    assert row["state"] == "running" and row["lease_deadline"] == 115
    assert row["generation"] == 1 and row["owner_token"] == lease.owner_token
    assert not repository._connection.in_transaction


def test_separate_connections_cannot_claim_same_live_evaluation(repository):
    root, _ = repository.create_investigation(request(), "root")
    first = RouteEvaluationStore(repository, clock=lambda: 100)
    claim(first, root.investigation_id)
    other = Repository(repository.path)
    try:
        with pytest.raises(RouteEvaluationBusy):
            claim(RouteEvaluationStore(other, clock=lambda: 101), root.investigation_id)
    finally:
        other.close()


def test_request_key_payload_conflict_is_detected_before_model(repository):
    root, _ = repository.create_investigation(request(), "root")
    store = RouteEvaluationStore(repository)
    claim(store, root.investigation_id)
    with pytest.raises(ConflictError, match="different message"):
        claim(store, root.investigation_id, original={**ORIGINAL, "content": "新任务：不同问题"})


def test_expired_generation_never_finishes_or_releases_successor(repository):
    root, _ = repository.create_investigation(request(), "root")
    now = [100]
    store = RouteEvaluationStore(repository, clock=lambda: now[0])
    old = claim(store, root.investigation_id)
    now[0] = 116
    current = claim(store, root.investigation_id)
    assert current.generation == 2 and current.owner_token != old.owner_token
    with pytest.raises(RouteEvaluationBusy):
        finish(store, old)
    store.abandon(old)
    row = repository._connection.execute("SELECT * FROM conversation_route_evaluations").fetchone()
    assert row["generation"] == 2 and row["state"] == "running"
    finish(store, current)
    assert repository._connection.execute("SELECT state FROM conversation_route_evaluations").fetchone()[0] == "finished"


def test_result_after_deadline_is_rejected_even_without_new_owner(repository):
    root, _ = repository.create_investigation(request(), "root")
    now = [100]
    store = RouteEvaluationStore(repository, clock=lambda: now[0])
    lease = claim(store, root.investigation_id)
    now[0] = 115
    with pytest.raises(RouteEvaluationBusy):
        finish(store, lease)


def test_finished_without_receipt_and_corrupt_state_are_not_reassessed(repository):
    root, _ = repository.create_investigation(request(), "root")
    store = RouteEvaluationStore(repository)
    lease = claim(store, root.investigation_id)
    finish(store, lease)
    with pytest.raises(ConflictError, match="missing its receipt"):
        claim(store, root.investigation_id)
    repository._connection.execute("UPDATE conversation_route_evaluations SET state='invalid'")
    with pytest.raises(ConflictError, match="invalid state"):
        claim(store, root.investigation_id)


@pytest.mark.parametrize("change", [{"generation": 2}, {"owner_token": "forged"}, {"policy_version": "b" * 64}])
def test_all_fence_components_are_required(repository, change):
    root, _ = repository.create_investigation(request(), "root")
    store = RouteEvaluationStore(repository)
    lease = claim(store, root.investigation_id)
    with pytest.raises(RouteEvaluationBusy):
        finish(store, replace(lease, **change))


def test_cancel_then_retry_has_finite_recovery_budget(repository):
    root, _ = repository.create_investigation(request(), "root")
    store = RouteEvaluationStore(repository)
    for generation in range(1, 4):
        lease = claim(store, root.investigation_id)
        assert lease.generation == generation and not lease.exhausted
        store.abandon(lease)
    lease = claim(store, root.investigation_id)
    assert lease.exhausted


def test_principal_and_conversation_scope_have_distinct_ownership(repository):
    root, _ = repository.create_investigation(request(), "root")
    other, _ = repository.create_investigation(request(), "other")
    store = RouteEvaluationStore(repository)
    claim(store, root.investigation_id)
    claim(store, root.investigation_id, principal="another-user")
    claim(store, other.investigation_id)
    assert repository._connection.execute("SELECT COUNT(*) FROM conversation_route_evaluations").fetchone()[0] == 3


def service(repository, output):
    router = SemanticConversationRouter(repository, SimpleNamespace(assess=AsyncMock(return_value=output)))
    return ConversationRoutingService(repository, router)


def test_cancelled_request_releases_its_generation_and_retry_recovers(repository):
    async def scenario():
        root, _ = repository.create_investigation(request(), "root")
        sibling = linked(repository, root.investigation_id, text="登录崩溃")
        routing = service(repository, assessment(task=sibling.investigation_id))
        routing.router.assessor.assess.side_effect = asyncio.CancelledError()
        original = {**ORIGINAL, "context_investigation_id": root.investigation_id}
        args = (root.investigation_id, original, "studio-a", "game-a", "tester", "route-cancel", payload_hash(original))
        with pytest.raises(asyncio.CancelledError):
            await routing.route(*args)
        assert repository._connection.execute("SELECT state FROM conversation_route_evaluations").fetchone()[0] == "retryable"
        routing.router.assessor.assess.side_effect = None
        result = await routing.route(*args)
        assert result["task_id"] == sibling.investigation_id and result["evaluation_generation"] == 2
        assert "_router_snapshot" not in result and not result["duplicate"]
    asyncio.run(scenario())


def test_new_service_replays_receipt_without_assessment(repository):
    async def scenario():
        root, _ = repository.create_investigation(request(), "root")
        sibling = linked(repository, root.investigation_id, text="登录崩溃")
        first = service(repository, assessment(task=sibling.investigation_id))
        args = (root.investigation_id, ORIGINAL, "studio-a", "game-a", "tester", "route-restart", payload_hash(ORIGINAL))
        result = await first.route(*args)
        other = Repository(repository.path)
        try:
            restarted = service(other, None)
            replay = await restarted.route(*args)
            assert replay == {**result, "duplicate": True}
            restarted.router.assessor.assess.assert_not_called()
        finally:
            other.close()
    asyncio.run(scenario())


def test_actual_process_cannot_claim_another_process_live_request(repository):
    import subprocess
    import sys

    root, _ = repository.create_investigation(request(), "root")
    store = RouteEvaluationStore(repository)
    claim(store, root.investigation_id)
    script = """
import sys
sys.path.insert(0,sys.argv[3])
from game_test_agent.storage import Repository
from game_test_agent.storage.route_evaluations import RouteEvaluationBusy, RouteEvaluationStore
from game_test_agent.storage.repository import payload_hash
original = {"content": "回到刚才那个登录问题，补充日志", "context_investigation_id": None}
repo = Repository(sys.argv[1])
try:
    with repo.transaction() as connection:
        repo._lock_investigation_row(connection, sys.argv[2])
        RouteEvaluationStore(repo).claim_in_transaction(connection,sys.argv[2],"tester","route-test",payload_hash(original),original,"a"*64,10)
except RouteEvaluationBusy:
    print("busy")
else:
    raise AssertionError("second process acquired live request")
finally:
    repo.close()
"""
    from pathlib import Path
    result = subprocess.run([sys.executable, "-c", script, str(repository.path), root.investigation_id,
        str(Path(__file__).parents[1] / "src")],
        capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "busy"


def test_late_model_result_cannot_overwrite_new_generation_receipt(repository):
    async def scenario():
        root, _ = repository.create_investigation(request(), "root")
        old_target = linked(repository, root.investigation_id, "old", text="登录崩溃旧问题")
        new_target = linked(repository, root.investigation_id, "new", text="登录崩溃新问题")
        first = service(repository, None)
        first.evaluations.clock = lambda: 100
        other = Repository(repository.path)
        args = (root.investigation_id, ORIGINAL, "studio-a", "game-a", "tester", "route-late", payload_hash(ORIGINAL))
        try:
            successor = service(other, assessment(task=new_target.investigation_id))
            successor.evaluations.clock = lambda: 116

            async def old_assess(*unused):
                replacement = await successor.route(*args)
                assert replacement["evaluation_generation"] == 2
                return assessment(task=old_target.investigation_id)

            first.router.assessor.assess.side_effect = old_assess
            result = await first.route(*args)
            assert result["task_id"] == new_target.investigation_id and result["duplicate"]
            assert result["evaluation_generation"] == 2
            assert repository._connection.execute("SELECT COUNT(*) FROM workbench_message_receipts").fetchone()[0] == 1
        finally:
            other.close()
    asyncio.run(scenario())


def test_receipt_failure_rolls_back_finished_state(repository, monkeypatch):
    async def scenario():
        root, _ = repository.create_investigation(request(), "root")
        routing = service(repository, assessment("current", reference=""))
        monkeypatch.setattr(routing.store, "save_receipt_in_transaction", lambda *args: (_ for _ in ()).throw(RuntimeError("write failed")))
        with pytest.raises(RuntimeError, match="write failed"):
            await routing.route(root.investigation_id, ORIGINAL, "studio-a", "game-a", "tester", "route-rollback", payload_hash(ORIGINAL))
        assert repository._connection.execute("SELECT state FROM conversation_route_evaluations").fetchone()[0] == "retryable"
        assert repository._connection.execute("SELECT COUNT(*) FROM workbench_message_receipts").fetchone()[0] == 0
    asyncio.run(scenario())


def test_exhaustion_commits_clarification_without_another_model_call(repository):
    async def scenario():
        root, _ = repository.create_investigation(request(), "root")
        routing = service(repository, None)
        for _ in range(3):
            lease = claim(routing.evaluations, root.investigation_id)
            routing.evaluations.abandon(lease)
        result = await routing.route(root.investigation_id, ORIGINAL, "studio-a", "game-a", "tester", "route-test", payload_hash(ORIGINAL))
        assert result["basis"] == "assessment_exhausted" and result["kind"] == "clarify"
        routing.router.assessor.assess.assert_not_called()
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["current", "new_task"])
def test_all_semantic_outcomes_recheck_catalog_at_commit(repository, monkeypatch, kind):
    async def scenario():
        root, _ = repository.create_investigation(request(), "root")
        output = assessment(kind, reference="单独" if kind == "new_task" else "")
        routing = service(repository, output)
        original = {"content": "另外单独调查登录崩溃", "context_investigation_id": root.investigation_id}
        finish_original = routing.evaluations.finish_in_transaction

        def changed(connection, lease):
            finish_original(connection, lease)
            # Simulate a concurrently changed catalog in the commit boundary,
            # without nesting another database transaction.
            row = repository.get_investigation(root.investigation_id)
            connection.execute("UPDATE investigations SET status='failed' WHERE investigation_id=?", (row.investigation_id,))

        monkeypatch.setattr(routing.evaluations, "finish_in_transaction", changed)
        result = await routing.route(root.investigation_id, original, "studio-a", "game-a", "tester", "route-change", payload_hash(original))
        assert result["kind"] == "clarify" and result["basis"] == "tasks_changed"
    asyncio.run(scenario())


def test_policy_change_while_waiting_cannot_apply_previous_proposal(repository):
    async def scenario():
        root, _ = repository.create_investigation(request(), "root")
        routing = service(repository, None)

        async def assess(*args):
            routing.router.timeout_seconds += 1
            return assessment("new_task", reference="单独")

        routing.router.assessor.assess.side_effect = assess
        original = {"content": "另外单独调查登录崩溃", "context_investigation_id": root.investigation_id}
        result = await routing.route(root.investigation_id, original, "studio-a", "game-a", "tester", "route-policy", payload_hash(original))
        assert result["kind"] == "clarify" and result["basis"] == "policy_changed"
    asyncio.run(scenario())


def test_other_api_owner_returns_retryable_503_with_same_request_key(settings):
    async def scenario():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            repo = app.state.container.repository
            root, _ = repo.create_investigation(request(), "root")
            original = {**ORIGINAL, "context_investigation_id": root.investigation_id}
            import hashlib
            token = "route-" + hashlib.sha256(f"{root.investigation_id}:tester:busy".encode()).hexdigest()
            claim(app.state.routing_service.evaluations, root.investigation_id, key=token, original=original)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(f"/v1/conversations/{root.investigation_id}/messages/route",
                    headers={**AUTH, "Idempotency-Key": "busy"}, json=original)
                assert response.status_code == 503 and response.headers["Retry-After"] == "1"
                assert not repo.list_user_inputs(root.investigation_id)
                row = repo._connection.execute("SELECT * FROM conversation_route_evaluations").fetchone()
                from game_test_agent.storage.route_evaluations import RouteEvaluationLease
                app.state.routing_service.evaluations.abandon(RouteEvaluationLease(root.investigation_id,
                    "tester", token, row["generation"], row["owner_token"], row["policy_version"]))
                retry = await client.post(f"/v1/conversations/{root.investigation_id}/messages/route",
                    headers={**AUTH, "Idempotency-Key": "busy"}, json=original)
                assert retry.status_code == 200 and retry.json()["evaluation_generation"] == 2
    asyncio.run(scenario())
