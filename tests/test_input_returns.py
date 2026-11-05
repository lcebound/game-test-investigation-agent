"""Worker handback, immutable provenance, no return loops and transactional delivery."""
import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from game_test_agent.analysis.permissions import CodePermissionStore
from game_test_agent.api import create_app
from game_test_agent.contracts.models import InputCommand, ReopenRequest
from game_test_agent.events.coordinator import ConversationCoordinator
from game_test_agent.events.dispatchers import DurableDispatcher
from game_test_agent.events.input_returns import InputHandback, InputReturnDecision, InputReturnService
from game_test_agent.events.message_router import ConversationMessageRouter
from game_test_agent.storage import ConflictError, NotFoundError, Repository
from game_test_agent.storage.repository import payload_hash
from game_test_agent.storage.workbench import WorkbenchStore
from game_test_agent.workflows.native_context import NativeMemory
from native_plan_helpers import initial_plan_call
from test_conversation_overview import AUTH, approval, close, create, state as fixture_state
from test_native_recall import memory
from test_native_reliability import call, final, response, run, setup
from test_native_enhancements import collect
from test_native_step import code_service, code_request, decide as approve_code


def input_message(repository, own, content="独立问题", identity="input"):
    repository.enqueue_command(own.investigation_id, identity, "input",
        InputCommand(command_id=identity, expected_revision=own.revision, content=content))
    return InputHandback(input_id=identity, reason="This message asks about another task")


def decide(service, root, item, target=None, **kwargs):
    return service.decide(root.investigation_id, item["return_id"],
        InputReturnDecision(expected_revision=item["revision"], action="attach" if target else "dismiss",
            target_investigation_id=target.investigation_id if target else None),
        "studio-a", "game-a", "tester", **kwargs)


def test_explicit_original_address_is_forwarded_once_and_not_imported_as_evidence(repository):
    own = create(repository, text="CPU卡顿")
    target = create(repository, "gpu", root=own.investigation_id, text="GPU卡顿")
    content = '继续「GPU卡顿」：保留原文，不补写方案'
    request = input_message(repository, own, content)
    service = InputReturnService(repository)
    result = service.handback(own.investigation_id, request)
    assert result["state"] == "routed" and result["target_investigation_id"] == target.investigation_id
    assert service.handback(own.investigation_id, request)["duplicate"]
    assert not repository.list_user_inputs(own.investigation_id)
    assert repository.list_user_inputs(own.investigation_id, include_returned=True)[0]["payload"]["content"] == content
    assert [r["payload"]["content"] for r in repository.list_user_inputs(target.investigation_id)] == [content]
    assert not repository.list_current_evidence(target.investigation_id)
    assert any(c["investigation_id"] == target.investigation_id for c in repository.pending_commands())
    with pytest.raises(ConflictError):
        service.handback(own.investigation_id, request.model_copy(update={"reason": "different reason"}))


def test_unaddressed_message_stays_pending_even_with_only_one_other_task(repository):
    own = create(repository)
    target = create(repository, "other", root=own.investigation_id)
    result = InputReturnService(repository).handback(own.investigation_id, input_message(repository, own))
    assert result["state"] == "pending" and not repository.list_user_inputs(target.investigation_id)
    summary = WorkbenchStore(repository).get(own.investigation_id, "studio-a", "game-a")
    assert summary["task_summary"]["pending_input_returns"] == 1
    page = ConversationCoordinator(repository).overview(own.investigation_id, "studio-a", "game-a")
    assert "input_return" in page["attention"][0]["attention"]


@pytest.mark.parametrize("status", ["completed", "failed", "canceled", "canceling"])
def test_automatic_return_does_not_resume_ended_or_stopped_targets(repository, status):
    own = create(repository, text="CPU")
    target = create(repository, "gpu", root=own.investigation_id, text="GPU卡顿")
    fixture_state(repository, target, status)
    result = InputReturnService(repository).handback(own.investigation_id,
        input_message(repository, own, '继续「GPU卡顿」：补充'))
    assert result["state"] == "pending" and not repository.list_user_inputs(target.investigation_id)


@pytest.mark.parametrize("invalid", ["foreign_input", "not_input", "missing_input", "terminal_source"])
def test_worker_can_only_return_its_own_reached_input(repository, invalid):
    own, other = create(repository), create(repository, "other")
    request = input_message(repository, other if invalid == "foreign_input" else own)
    if invalid == "not_input":
        repository._connection.execute("UPDATE workflow_command_outbox SET kind='evidence' WHERE command_id='input'")
    elif invalid == "missing_input":
        request = request.model_copy(update={"input_id": "missing"})
    elif invalid == "terminal_source":
        fixture_state(repository, own, "completed")
    with pytest.raises((NotFoundError, ConflictError)):
        InputReturnService(repository).handback(own.investigation_id, request)
    assert repository._connection.execute("SELECT COUNT(*) FROM task_input_returns").fetchone()[0] == 0


def test_reassignment_keeps_one_origin_and_cannot_bounce_to_rejecting_task_or_its_followup(repository):
    own = create(repository, text="任务 A")
    second = create(repository, "second", root=own.investigation_id, text="任务 B")
    third = create(repository, "third", root=own.investigation_id, text="任务 C")
    service = InputReturnService(repository)
    first = service.handback(own.investigation_id, input_message(repository, own, "原文不变"))
    routed = decide(service, own, first, second)
    assert decide(service, own, first, second)["duplicate"]
    assert routed["state"] == "routed"
    next_id = repository.list_user_inputs(second.investigation_id)[0]["command_id"]
    returned = service.handback(second.investigation_id, InputHandback(input_id=next_id, reason="Not part of B"))
    close(repository, own)
    child, _ = repository.reopen_investigation(own.investigation_id,
        ReopenRequest(reason="继续 A", budget=own.request.budget), "child")
    with pytest.raises(ConflictError, match="cannot go back"):
        decide(service, own, returned, child)
    decide(service, own, returned, third)
    detail = service.detail(own.investigation_id, first["return_id"], "studio-a", "game-a")
    assert set(detail["excluded_task_ids"]) == {own.investigation_id, second.investigation_id}
    assert detail["content"] == "原文不变"
    assert repository.list_user_inputs(third.investigation_id)[0]["payload"]["content"] == "原文不变"


@pytest.mark.parametrize("change", ["foreign_conversation", "ended", "stale_head", "archived"])
def test_explicit_decisions_still_recheck_scope_head_and_state(repository, change):
    own = create(repository)
    target = create(repository, "target", root=None if change == "foreign_conversation" else own.investigation_id)
    service = InputReturnService(repository)
    returned = service.handback(own.investigation_id, input_message(repository, own))
    if change in {"ended", "stale_head"}:
        close(repository, target)
    if change == "stale_head":
        repository.reopen_investigation(target.investigation_id, ReopenRequest(reason="继续", budget=target.request.budget), "next")
    if change == "archived":
        WorkbenchStore(repository).update(own.investigation_id, "studio-a", "game-a", 0, archived=True)
    with pytest.raises((NotFoundError, ConflictError)):
        decide(service, own, returned, target)
    assert not repository.list_user_inputs(target.investigation_id)


def test_failed_delivery_transaction_keeps_input_owned_and_creates_no_return(repository):
    own = create(repository, text="CPU")
    create(repository, "gpu", root=own.investigation_id, text="GPU卡顿")
    request = input_message(repository, own, '继续「GPU卡顿」：补充')
    permission = approval(repository, own)
    repository._connection.execute("""CREATE TRIGGER stop_forwarding BEFORE INSERT ON workflow_command_outbox
        WHEN NEW.command_id LIKE 'returned-input-%' BEGIN SELECT RAISE(ABORT,'offline failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        InputReturnService(repository).handback(own.investigation_id, request)
    assert repository.list_user_inputs(own.investigation_id)
    assert repository._connection.execute("SELECT COUNT(*) FROM task_input_returns").fetchone()[0] == 0
    assert CodePermissionStore(repository).get(permission["approval_id"])["status"] == "pending"


def test_pending_permissions_are_canceled_and_older_approvals_are_context_stale(repository):
    own = create(repository)
    request = input_message(repository, own)
    permission = approval(repository, own)
    InputReturnService(repository).handback(own.investigation_id, request)
    store = CodePermissionStore(repository)
    assert store.get(permission["approval_id"])["status"] == "canceled"
    assert store.input_superseded(permission)


def test_memory_and_restart_stop_using_returned_input_but_keep_other_requirements(repository, settings):
    own, controller, _ = setup(repository, settings)
    returned = input_message(repository, own, "RETURNED_RAW_SECRET", "wrong")
    input_message(repository, own, "Keep the recovery constraint", "valid")
    state = memory()
    state.sync_inputs(repository, own.investigation_id)
    state.messages.append({"role": "assistant", "content": "RETURNED_RAW_SECRET"})
    state.cache[json.dumps(["mcp", "lab", "inspect", {}])] = {"payload": {"content": "RETURNED_RAW_SECRET"}}
    state.save(repository, controller.artifacts, own)
    InputReturnService(repository).handback(own.investigation_id, returned)
    restored = NativeMemory.restore(repository, controller.artifacts, own)
    assert restored.sync_inputs(repository, own.investigation_id)
    assert restored.input_ids == ["valid"] and not restored.cache
    assert "RETURNED_RAW_SECRET" not in json.dumps(restored.messages)
    assert "Keep the recovery constraint" in restored.messages[2]["content"]
    assert not restored.input_gaps()


@pytest.mark.parametrize("damaged", [json.dumps({"content": "changed"}), json.dumps({"content": 123}), "{broken"])
def test_pending_return_survives_repository_restart_and_damaged_source_remains_visible(settings, damaged):
    repo = Repository(settings.database_path)
    own = create(repo)
    returned = InputReturnService(repo).handback(own.investigation_id, input_message(repo, own, "exact"))
    repo.close()
    restored = Repository(settings.database_path)
    try:
        service = InputReturnService(restored)
        assert service.page(own.investigation_id, "studio-a", "game-a")["pending_count"] == 1
        restored._connection.execute("UPDATE workflow_command_outbox SET payload_json=? WHERE command_id='input'",
            (damaged,))
        page = service.page(own.investigation_id, "studio-a", "game-a")
        assert page["items"][0]["unavailable"]
        with pytest.raises(ConflictError):
            service.detail(own.investigation_id, returned["return_id"], "studio-a", "game-a")
    finally:
        restored.close()


def test_native_worker_returns_before_plan_and_closes_later_calls_without_execution(repository, settings):
    async def scenario():
        own, controller, provider = setup(repository, settings, rounds=8)
        request = input_message(repository, own, "另一个独立问题")
        turns = 0

        async def model(messages, tools):
            nonlocal turns
            turns += 1
            if turns == 1:
                assert "agent_return_input" in {t["function"]["name"] for t in tools}
                return response(call("agent_return_input", request.model_dump(), "return"), call(ident="must-not-run"))
            if turns == 2:
                assert "另一个独立问题" not in json.dumps(messages, ensure_ascii=False)
                assert "agent_return_input" not in {t["function"]["name"] for t in tools}
                return response(initial_plan_call(), call(ident="read-after-return"))
            return final(messages)

        controller._model_call = model
        report = await run(controller, own)
        assert report.stop_reason == "native_mcp_investigation_complete" and turns == 3
        assert any("退回协调层" in item for item in report.unresolved_questions)
        provider.call_native.assert_awaited_once()
        assert not repository.list_user_inputs(own.investigation_id)
    asyncio.run(scenario())


def test_expired_target_is_not_automatically_woken(repository):
    own = create(repository, text="CPU")
    target = create(repository, "gpu", root=own.investigation_id, text="GPU卡顿")
    repository._connection.execute("UPDATE investigations SET created_at=? WHERE investigation_id=?",
        ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(), target.investigation_id))
    result = InputReturnService(repository).handback(own.investigation_id,
        input_message(repository, own, '继续「GPU卡顿」：补充'))
    assert result["state"] == "pending" and not repository.list_user_inputs(target.investigation_id)


def test_destination_changed_after_assessment_leaves_the_handback_pending(repository, monkeypatch):
    own = create(repository, text="CPU")
    target = create(repository, "gpu", root=own.investigation_id, text="GPU卡顿")
    original = ConversationMessageRouter.resolve

    def stale_candidate(router, *args, **kwargs):
        result = original(router, *args, **kwargs)
        repository._connection.execute("UPDATE investigations SET status='canceled' WHERE investigation_id=?", (target.investigation_id,))
        return result

    monkeypatch.setattr(ConversationMessageRouter, "resolve", stale_candidate)
    returned = InputReturnService(repository).handback(own.investigation_id,
        input_message(repository, own, '继续「GPU卡顿」：补充'))
    assert returned["state"] == "pending" and not repository.list_user_inputs(own.investigation_id)
    assert not repository.list_user_inputs(target.investigation_id)


def test_a_preexisting_command_identity_is_not_adopted_as_a_forwarded_input(repository):
    own = create(repository, text="CPU")
    target = create(repository, "gpu", root=own.investigation_id, text="GPU卡顿")
    request = input_message(repository, own, '继续「GPU卡顿」：原文')
    trigger = "input-" + payload_hash([own.investigation_id, request.input_id])
    command_id = "returned-input-" + payload_hash([trigger, target.investigation_id])
    input_message(repository, target, "DIFFERENT SOURCE", command_id)
    returned = InputReturnService(repository).handback(own.investigation_id, request)
    assert returned["state"] == "pending"
    assert [row["payload"]["content"] for row in repository.list_user_inputs(target.investigation_id)] == ["DIFFERENT SOURCE"]


def test_dispatcher_rechecks_returned_inputs_after_taking_a_queue_snapshot(repository):
    async def scenario():
        own = create(repository)
        request = input_message(repository, own)
        queued = next(c for c in repository.pending_commands() if c["command_id"] == request.input_id)
        InputReturnService(repository).handback(own.investigation_id, request)
        client = Mock(signal=AsyncMock())
        assert await DurableDispatcher(repository, client, None)._dispatch_command(queued) == 0
        client.signal.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize("wait_for_permission", [True, False])
def test_preapproved_code_is_not_dispatched_after_a_handback(repository, settings, wait_for_permission):
    async def scenario():
        own, controller, _, _, ids = await collect(repository, settings, [{"value": 7}])
        request = input_message(repository, own)
        service = code_service(controller)
        code = code_request(ids)
        proposed = await service.execute(own.investigation_id, "old-action", code, wait_for_permission=False)
        permission = service.permissions.get(proposed["approval_id"])
        approve_code(service, permission, "approve")
        InputReturnService(repository).handback(own.investigation_id, request)
        result = await service.execute(own.investigation_id, "old-action", code, wait_for_permission=wait_for_permission)
        assert result["failure_kind"] == "permission_superseded_input"
        service.sandbox.prepare.assert_not_called()
        service.runner.submit.assert_not_awaited()
    asyncio.run(scenario())


def test_pending_queue_paginates_bounded_cards_without_losing_inputs(repository):
    own = create(repository)
    service = InputReturnService(repository)
    for i in range(23):
        service.handback(own.investigation_id, input_message(repository, own, "长原文" * 1000, f"input-{i}"))
    offset, ids = 0, []
    while True:
        page = service.page(own.investigation_id, "studio-a", "game-a", offset=offset)
        assert page["pending_count"] == 23 and len(json.dumps(page, ensure_ascii=False).encode()) < 16384
        ids.extend(row["return_id"] for row in page["items"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert len(ids) == len(set(ids)) == 23


@pytest.mark.parametrize("header", ["X-Tenant-ID", "X-Project-ID"])
def test_return_api_rejects_foreign_projects_and_does_not_start_workflows(settings, header):
    async def scenario():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            repo = app.state.container.repository
            app.state.container.workflows.start = Mock()
            own = create(repo)
            returned = InputReturnService(repo).handback(own.investigation_id, input_message(repo, own))
            path = f"/v1/conversations/{own.investigation_id}/input-returns"
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                foreign = {**AUTH, header: "other"}
                assert (await client.get(path, headers=foreign)).status_code == 404
                assert (await client.get(path + '/' + returned["return_id"], headers=foreign)).status_code == 404
                assert (await client.post(path + '/' + returned["return_id"] + '/resolve', headers=foreign,
                    json={"expected_revision": 1, "action": "dismiss"})).status_code == 404
                assert (await client.get(path, headers=AUTH)).json()["pending_count"] == 1
                outcome = await client.post(path + '/' + returned["return_id"] + '/resolve', headers=AUTH,
                    json={"expected_revision": 1, "action": "dismiss"})
                assert outcome.status_code == 200 and outcome.json()["state"] == "dismissed"
            app.state.container.workflows.start.assert_not_called()
    asyncio.run(scenario())
