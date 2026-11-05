"""Semantic reassignment must preserve original identity, authority and rejection history."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from game_test_agent.events.input_returns import InputHandback, InputReturnDecision, InputReturnService
from game_test_agent.events.returned_input_router import ReturnedInputRouter
from game_test_agent.events.router_service import ConversationRoutingService
from game_test_agent.events.semantic_router import SemanticConversationRouter
from game_test_agent.storage import ConflictError, NotFoundError, Repository
from game_test_agent.storage.route_evaluations import RouteEvaluationBusy
from test_input_returns import input_message
from test_conversation_overview import create, state as set_state
from test_semantic_routing import assessment


TEXT = "回到刚才那个登录问题，补充这段日志"


def setup(repository, content=TEXT):
    own = create(repository, text="CPU负载异常")
    target = create(repository, "login", root=own.investigation_id, text="登录崩溃")
    returns = InputReturnService(repository)
    item = returns.handback(own.investigation_id, input_message(repository, own, content))
    assessor = SimpleNamespace(assess=AsyncMock(return_value=assessment(task=target.investigation_id)))
    router = SemanticConversationRouter(repository, assessor)
    coordinator = ReturnedInputRouter(repository, router, returns)
    return own, target, item, coordinator


async def reassess(coordinator, own, item):
    return await coordinator.reassess(own.investigation_id, item["return_id"], "studio-a", "game-a")


def test_semantic_handback_delivers_original_once_with_isolated_context(repository, monkeypatch):
    async def scenario():
        own, target, item, coordinator = setup(repository)
        authorization = repository.get_authorization(target.investigation_id)
        for method in ("get_latest_report", "list_recent_events", "list_current_evidence"):
            monkeypatch.setattr(repository, method, Mock(side_effect=AssertionError("no worker material")))
        result = await reassess(coordinator, own, item)
        assert result["kind"] == "task" and result["investigation_id"] == target.investigation_id
        payload, scope = coordinator.router.assessor.assess.call_args.args
        assert scope == ("studio-a", "game-a")
        assert payload["message"] == TEXT and payload["context_investigation_id"] is None
        assert {card["task_id"] for card in payload["tasks"]} == {target.investigation_id}
        assert payload["returned_input"]["reason_assessment"] == "unverified_worker_routing_assessment"
        assert "_router_snapshot" not in result and "_router_excluded_task_ids" not in result
        assert [entry["payload"]["content"] for entry in repository.list_user_inputs(target.investigation_id)] == [TEXT]
        assert not repository.list_user_inputs(own.investigation_id)
        assert repository.get_authorization(target.investigation_id) == authorization
        assert (await reassess(coordinator, own, item))["duplicate"]
        coordinator.router.assessor.assess.assert_awaited_once()
        assert len(repository.list_user_inputs(target.investigation_id)) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("choice", ["current", "clarify", "source_task", "foreign_task", "fake_reference", "worker_reference"])
def test_uncertain_or_unverifiable_proposals_leave_input_pending(repository, choice):
    async def scenario():
        own, target, item, coordinator = setup(repository)
        if choice in {"current", "clarify"}:
            output = assessment(choice, reference="")
        elif choice == "source_task":
            output = assessment(task=own.investigation_id)
        elif choice == "foreign_task":
            outside = create(repository, "outside", text="登录崩溃")
            output = assessment(task=outside.investigation_id)
        else:
            output = assessment(task=target.investigation_id,
                reference="This message asks about another task" if choice == "worker_reference" else "原文没有说过的话")
        coordinator.router.assessor.assess.return_value = output
        result = await reassess(coordinator, own, item)
        assert result["kind"] == "clarify"
        assert coordinator.returns._get(item["return_id"])["state"] == "pending"
        assert not repository.list_user_inputs(target.investigation_id)
        assert not coordinator.due()
        assert coordinator.returns.page(own.investigation_id, "studio-a", "game-a")["pending_count"] == 1
    asyncio.run(scenario())


def test_semantic_new_task_does_not_invent_budget_or_start_an_investigation(repository):
    async def scenario():
        own, _, item, coordinator = setup(repository, "另外单独调查登录问题")
        coordinator.router.assessor.assess.return_value = assessment("new_task", reference="另外单独调查")
        before = len(repository.list_investigations("studio-a", "game-a"))
        result = await reassess(coordinator, own, item)
        assert result["basis"] == "independent_task_requires_intake"
        assert len(repository.list_investigations("studio-a", "game-a")) == before
        assert coordinator.returns._get(item["return_id"])["state"] == "pending"
    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["completed", "failed", "canceling", "canceled"])
def test_ended_target_is_not_resumed(repository, status):
    async def scenario():
        own, target, item, coordinator = setup(repository)
        set_state(repository, target, status)
        result = await reassess(coordinator, own, item)
        assert result["kind"] == "clarify"
        assert not repository.list_user_inputs(target.investigation_id)
    asyncio.run(scenario())


def test_manual_decision_wins_over_an_inflight_model(repository):
    async def scenario():
        own, target, item, coordinator = setup(repository)
        manual = create(repository, "manual", root=own.investigation_id, text="人工指定的任务")

        async def assess(*args):
            coordinator.returns.decide(own.investigation_id, item["return_id"],
                InputReturnDecision(expected_revision=1, action="attach", target_investigation_id=manual.investigation_id),
                "studio-a", "game-a", "tester")
            return assessment(task=target.investigation_id)

        coordinator.router.assessor.assess.side_effect = assess
        assert (await reassess(coordinator, own, item))["kind"] == "superseded"
        assert len(repository.list_user_inputs(manual.investigation_id)) == 1
        assert not repository.list_user_inputs(target.investigation_id)
    asyncio.run(scenario())


def test_manual_dismissal_is_not_undone(repository):
    async def scenario():
        own, target, item, coordinator = setup(repository)

        async def assess(*args):
            coordinator.returns.decide(own.investigation_id, item["return_id"],
                InputReturnDecision(expected_revision=1, action="dismiss"), "studio-a", "game-a", "tester")
            return assessment(task=target.investigation_id)

        coordinator.router.assessor.assess.side_effect = assess
        assert (await reassess(coordinator, own, item))["kind"] == "superseded"
        assert coordinator.returns._get(item["return_id"])["state"] == "dismissed"
        assert not repository.list_user_inputs(target.investigation_id)
    asyncio.run(scenario())


def test_target_becomes_stopped_at_delivery_boundary(repository, monkeypatch):
    async def scenario():
        own, target, item, coordinator = setup(repository)
        revalidate = coordinator.router.revalidate

        def stop_before_attach(*args):
            decision = revalidate(*args)
            repository._connection.execute("UPDATE investigations SET status='canceled' WHERE investigation_id=?", (target.investigation_id,))
            return decision

        monkeypatch.setattr(coordinator.router, "revalidate", stop_before_attach)
        result = await reassess(coordinator, own, item)
        assert result["basis"] == "target_unavailable"
        assert not repository.list_user_inputs(target.investigation_id)
        assert coordinator.returns._get(item["return_id"])["state"] == "pending"
    asyncio.run(scenario())


def test_savepoint_rolls_back_partial_attach_on_target_conflict(repository, monkeypatch):
    async def scenario():
        own, target, item, coordinator = setup(repository)
        attach = coordinator.returns._attach

        def failed_attach(*args):
            attach(*args)
            raise ConflictError("simulated target conflict after write")

        monkeypatch.setattr(coordinator.returns, "_attach", failed_attach)
        assert (await reassess(coordinator, own, item))["basis"] == "target_unavailable"
        assert not repository.list_user_inputs(target.investigation_id)
        assert coordinator.returns._get(item["return_id"])["state"] == "pending"
    asyncio.run(scenario())


def test_receipt_failure_rolls_back_outbox_and_allows_retry(repository, monkeypatch):
    async def scenario():
        own, target, item, coordinator = setup(repository)
        save = coordinator.store.save_receipt_in_transaction
        monkeypatch.setattr(coordinator.store, "save_receipt_in_transaction", Mock(side_effect=RuntimeError("write failed")))
        with pytest.raises(RuntimeError):
            await reassess(coordinator, own, item)
        assert coordinator.returns._get(item["return_id"])["state"] == "pending"
        assert not repository.list_user_inputs(target.investigation_id)
        monkeypatch.setattr(coordinator.store, "save_receipt_in_transaction", save)
        result = await reassess(coordinator, own, item)
        assert result["kind"] == "task" and result["evaluation_generation"] == 2
        assert len(repository.list_user_inputs(target.investigation_id)) == 1
    asyncio.run(scenario())


def test_cancellation_uses_specialized_recovery_and_is_not_misparsed_as_chat(repository):
    async def scenario():
        own, target, item, coordinator = setup(repository)
        now = [100]
        coordinator.evaluations.clock = lambda: now[0]
        coordinator.router.assessor.assess.side_effect = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await reassess(coordinator, own, item)
        now[0] = 106
        generic = ConversationRoutingService(repository, coordinator.router)
        generic.evaluations.clock = lambda: now[0]
        assert not generic.evaluations.due()
        coordinator.router.assessor.assess.side_effect = None
        assert (await coordinator.recover_due())["routed"] == 1
        assert len(repository.list_user_inputs(target.investigation_id)) == 1
    asyncio.run(scenario())


def test_second_handback_cannot_loop_to_any_prior_worker(repository):
    async def scenario():
        own, target, item, coordinator = setup(repository)
        await reassess(coordinator, own, item)
        first = coordinator.returns._get(item["return_id"])
        second = coordinator.returns.handback(target.investigation_id,
            InputHandback(input_id=first["target_command_id"], reason="belongs elsewhere"))
        coordinator.router.assessor.assess.return_value = assessment(task=own.investigation_id)
        result = await reassess(coordinator, own, second)
        assert result["kind"] == "clarify"
        payload = coordinator.router.assessor.assess.call_args.args[0]
        assert not payload["tasks"]
        assert coordinator.returns._get(second["return_id"])["trigger_id"] == first["trigger_id"]
        assert not repository.list_user_inputs(own.investigation_id)
    asyncio.run(scenario())


@pytest.mark.parametrize("broken", [False, True, "deep_json"])
def test_long_or_changed_source_is_not_forwarded(repository, broken):
    async def scenario():
        own, target, item, coordinator = setup(repository, TEXT if broken else "x" * 4001)
        if broken:
            repository._connection.execute("UPDATE workflow_command_outbox SET payload_json=? WHERE command_id='input'",
                ('[' * 2000 + ']' * 2000 if broken == "deep_json" else '{"content":"changed original"}',))
        assert (await reassess(coordinator, own, item))["basis"] == "source_unavailable"
        coordinator.router.assessor.assess.assert_not_called()
        assert not repository.list_user_inputs(target.investigation_id)
    asyncio.run(scenario())


def test_source_tampering_during_model_call_prevents_delivery(repository):
    async def scenario():
        own, target, item, coordinator = setup(repository)

        async def assess(*args):
            repository._connection.execute("UPDATE workflow_command_outbox SET payload_json=? WHERE command_id='input'", ('{"content":"tampered"}',))
            return assessment(task=target.investigation_id)

        coordinator.router.assessor.assess.side_effect = assess
        assert (await reassess(coordinator, own, item))["basis"] == "source_unavailable"
        assert not repository.list_user_inputs(target.investigation_id)
    asyncio.run(scenario())


def test_disabled_router_leaves_no_sticky_evaluation_and_scope_is_checked(repository):
    async def scenario():
        own, _, item, coordinator = setup(repository)
        coordinator.router.assessor = None
        assert (await reassess(coordinator, own, item))["basis"] == "semantic_router_disabled"
        assert repository._connection.execute("SELECT COUNT(*) FROM conversation_route_evaluations").fetchone()[0] == 0
        with pytest.raises(NotFoundError):
            await coordinator.reassess(own.investigation_id, item["return_id"], "other", "game-a")
    asyncio.run(scenario())


def test_archived_return_is_not_scanned(repository):
    own, _, _, coordinator = setup(repository)
    coordinator.store.update(own.investigation_id, "studio-a", "game-a", 0, archived=True)
    assert not coordinator.due()
    for bad in (0, 17):
        with pytest.raises(ValueError):
            coordinator.due(limit=bad)


def test_expired_old_model_result_cannot_overwrite_successor(repository):
    async def scenario():
        own, target, item, coordinator = setup(repository)
        new_target = create(repository, "new", root=own.investigation_id, text="登录闪退对照")
        coordinator.evaluations.clock = lambda: 100
        other = Repository(repository.path)
        try:
            next_router = SemanticConversationRouter(other, SimpleNamespace(assess=AsyncMock(return_value=assessment(task=new_target.investigation_id))))
            successor = ReturnedInputRouter(other, next_router)
            successor.evaluations.clock = lambda: 116

            async def late(*args):
                assert (await reassess(successor, own, item))["investigation_id"] == new_target.investigation_id
                return assessment(task=target.investigation_id)

            coordinator.router.assessor.assess.side_effect = late
            with pytest.raises(RouteEvaluationBusy):
                await reassess(coordinator, own, item)
            assert not repository.list_user_inputs(target.investigation_id)
            assert len(repository.list_user_inputs(new_target.investigation_id)) == 1
        finally:
            other.close()
    asyncio.run(scenario())


def test_archive_during_assessment_keeps_input_recoverable(repository):
    async def scenario():
        own, target, item, coordinator = setup(repository)

        async def assess(*args):
            coordinator.store.update(own.investigation_id, "studio-a", "game-a", 0, archived=True)
            return assessment(task=target.investigation_id)

        coordinator.router.assessor.assess.side_effect = assess
        with pytest.raises(ConflictError, match="restore"):
            await reassess(coordinator, own, item)
        assert not coordinator.due()
        assert coordinator.returns._get(item["return_id"])["state"] == "pending"
        assert repository._connection.execute("SELECT state FROM conversation_route_evaluations").fetchone()[0] == "retryable"
        assert not repository.list_user_inputs(target.investigation_id)
    asyncio.run(scenario())


def test_policy_change_never_delivers_old_proposal(repository):
    async def scenario():
        own, target, item, coordinator = setup(repository)

        async def assess(*args):
            coordinator.router.timeout_seconds += 1
            return assessment(task=target.investigation_id)

        coordinator.router.assessor.assess.side_effect = assess
        assert (await reassess(coordinator, own, item))["basis"] == "policy_changed"
        assert not repository.list_user_inputs(target.investigation_id)
    asyncio.run(scenario())


def test_model_failure_is_finite_and_never_leaks_provider_error(repository):
    async def scenario():
        own, target, item, coordinator = setup(repository)
        coordinator.router.assessor.assess.side_effect = RuntimeError("SECRET provider credentials")
        result = await reassess(coordinator, own, item)
        assert result["kind"] == "clarify" and "SECRET" not in str(result)
        assert not coordinator.due()
        assert not repository.list_user_inputs(target.investigation_id)
    asyncio.run(scenario())


def test_interrupted_handback_has_finite_evaluation_generations(repository):
    async def scenario():
        own, target, item, coordinator = setup(repository)
        coordinator.router.assessor.assess.side_effect = asyncio.CancelledError()
        for _ in range(3):
            with pytest.raises(asyncio.CancelledError):
                await reassess(coordinator, own, item)
        result = await reassess(coordinator, own, item)
        assert result["basis"] == "assessment_exhausted" and result["evaluation_generation"] == 4
        assert coordinator.router.assessor.assess.await_count == 3
        assert coordinator.returns._get(item["return_id"])["state"] == "pending"
        assert not repository.list_user_inputs(target.investigation_id)
    asyncio.run(scenario())


def test_second_connection_cannot_evaluate_a_live_handback(repository):
    async def scenario():
        own, target, item, coordinator = setup(repository)
        other = Repository(repository.path)
        try:
            other_router = SemanticConversationRouter(other, SimpleNamespace(assess=AsyncMock()))
            competitor = ReturnedInputRouter(other, other_router)

            async def assess(*args):
                with pytest.raises(RouteEvaluationBusy):
                    await reassess(competitor, own, item)
                assert not competitor.due()
                other_router.assessor.assess.assert_not_called()
                return assessment(task=target.investigation_id)

            coordinator.router.assessor.assess.side_effect = assess
            assert (await reassess(coordinator, own, item))["kind"] == "task"
        finally:
            other.close()
    asyncio.run(scenario())


def test_background_lifecycle_runs_specialized_returns_after_message_recovery():
    from game_test_agent.events.router_recovery import RouterRecovery

    async def scenario():
        completed = asyncio.Event()

        async def recover_returns(**kwargs):
            completed.set()
            return {"routed": 1}

        fake = SimpleNamespace(recover_due=AsyncMock(return_value={"completed": 1}),
            returned_inputs=SimpleNamespace(recover_due=AsyncMock(side_effect=recover_returns)))
        recovery = RouterRecovery(fake, enabled=True)
        recovery.start()
        await asyncio.wait_for(completed.wait(), 2)
        await recovery.close()
        assert recovery.last_pass == {"completed": 1, "input_returns": {"routed": 1}}
        fake.returned_inputs.recover_due.assert_awaited_once_with(limit=4)
    asyncio.run(scenario())
