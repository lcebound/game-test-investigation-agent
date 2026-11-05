"""Unified intake: scoped bindings bypass inference; unowned input never uses keyword gates."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from game_test_agent.events.semantic_router import SemanticConversationRouter
from game_test_agent.events.observation_router import ObservationBatchRouter
from game_test_agent.events.policy import EventSourcePolicy
from game_test_agent.storage import NotFoundError
from test_harness_completion import observation_setup, observation
from test_semantic_routing import assessment
from test_conversation_overview import create


@pytest.mark.parametrize("message", ["把这份日志补到登录闪退任务", "转到登录闪退任务继续调查"])
def test_unowned_transfer_without_magic_words_uses_semantics(repository, message):
    async def scenario():
        root = create(repository)
        login = create(repository, "login", root=root.investigation_id, text="登录闪退")
        assessor = SimpleNamespace(
            assess=AsyncMock(
                return_value=assessment(task=login.investigation_id, reference="登录闪退任务")
            )
        )
        service = SemanticConversationRouter(repository, assessor)
        result = await service.resolve(
            root.investigation_id,
            message,
            "studio-a",
            "game-a",
            context_investigation_id=root.investigation_id,
        )
        assert result["task_id"] == login.investigation_id
        assessor.assess.assert_awaited_once()

    asyncio.run(scenario())


def test_unowned_new_request_is_not_silently_attached_to_current(repository):
    async def scenario():
        root = create(repository)
        assessor = SimpleNamespace(
            assess=AsyncMock(return_value=assessment("new_task", reference="新建一项"))
        )
        result = await SemanticConversationRouter(repository, assessor).resolve(
            root.investigation_id,
            "新建一项场景加载调查",
            "studio-a",
            "game-a",
            context_investigation_id=root.investigation_id,
        )
        assert result["kind"] == "new_task"

    asyncio.run(scenario())


def test_relative_reference_without_history_cannot_be_guessed(repository):
    async def scenario():
        root = create(repository)
        assessor = SimpleNamespace(assess=AsyncMock())
        service = SemanticConversationRouter(repository, assessor)
        assert (
            await service.resolve(
                root.investigation_id,
                "上一个任务继续",
                "studio-a",
                "game-a",
                context_investigation_id=root.investigation_id,
            )
        )["kind"] == "clarify"
        assessor.assess.assert_not_awaited()

    asyncio.run(scenario())


def test_binding_is_scope_checked_even_when_body_suggests_other_task(repository):
    async def scenario():
        root = create(repository)
        other = create(repository, "other")
        service = SemanticConversationRouter(repository, SimpleNamespace(assess=AsyncMock()))
        with pytest.raises(NotFoundError):
            await service.resolve(
                root.investigation_id,
                "新任务：其他目标",
                "studio-a",
                "game-a",
                bound_investigation_id=other.investigation_id,
            )

    asyncio.run(scenario())


def test_observation_thread_owner_receives_followups_without_reassessment(repository):
    async def scenario():
        root, target, router = observation_setup(repository)
        router.accept(observation(root))
        assert (await router.recover_due())["routed"] == 1
        router.router.assessor.assess.side_effect = AssertionError("owner must bypass reassessment")
        next_event = observation(root, 1, "counterevidence")
        next_event["summary"] = "采样又变了，没有重复任务名称"
        router.accept(next_event)
        assert (await router.recover_due())["routed"] == 1
        router.router.assessor.assess.assert_awaited_once()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "enabled,scopes,expected",
    [
        (False, ["runs:read", "evidence:read"], 0),
        (True, [], 0),
        (True, ["runs:read", "evidence:read", "runs:submit"], 1),
    ],
)
def test_unowned_failure_can_start_only_host_authorized_readonly_task(
    repository, enabled, scopes, expected
):
    async def scenario():
        root = create(repository)
        policy = EventSourcePolicy(
            source="monitor",
            tenant_id="studio-a",
            project_id="game-a",
            scopes=scopes,
            allow_observation_investigations=enabled,
        )
        router = ObservationBatchRouter(
            repository,
            SemanticConversationRouter(repository),
            {("monitor", "studio-a", "game-a"): policy},
        )
        router.accept(observation(root))
        result = await router.recover_due()
        assert result["routed"] == expected
        heads = router.router.rules.store.task_heads(
            root.investigation_id, "studio-a", "game-a", limit=20
        )
        assert len(heads) == 1 + expected
        if expected:
            new = next(
                repository.get_investigation(row["investigation_id"])
                for row in heads
                if row["investigation_id"] != root.investigation_id
            )
            assert new.request.budget.max_real_runs == 0
            assert "runs:submit" not in repository.get_authorization(new.investigation_id)["scopes"]
            assert new.status == "accepted"
            router.accept(observation(root, 1, "failed"))
            await router.recover_due()
            assert (
                len(
                    router.router.rules.store.task_heads(
                        root.investigation_id, "studio-a", "game-a", limit=20
                    )
                )
                == 2
            )

    asyncio.run(scenario())


def test_auto_task_cap_and_stopped_thread_never_spawn_replacements(repository):
    async def scenario():
        root = create(repository)
        policy = EventSourcePolicy(
            source="monitor",
            tenant_id="studio-a",
            project_id="game-a",
            scopes=["runs:read", "evidence:read"],
            allow_observation_investigations=True,
            max_observation_tasks=1,
        )
        router = ObservationBatchRouter(
            repository,
            SemanticConversationRouter(repository),
            {("monitor", "studio-a", "game-a"): policy},
        )
        router.accept(observation(root))
        assert (await router.recover_due())["routed"] == 1
        owners = router._owners(
            root.investigation_id, "monitor", "thread-one", "studio-a", "game-a"
        )
        with repository.transaction() as connection:
            connection.execute(
                "UPDATE investigations SET status='canceled' WHERE investigation_id=?",
                (owners[0]["investigation_id"],),
            )
        router.accept(observation(root, 1))
        assert (await router.recover_due())["routed"] == 0
        another = observation(root, 2)
        another["thread_key"] = "second-thread"
        router.accept(another)
        assert (await router.recover_due())["routed"] == 0
        assert (
            len(
                router.router.rules.store.task_heads(
                    root.investigation_id, "studio-a", "game-a", limit=20
                )
            )
            == 2
        )

    asyncio.run(scenario())


def test_auto_creation_and_delivery_roll_back_together(repository, monkeypatch):
    async def scenario():
        root = create(repository)
        policy = EventSourcePolicy(
            source="monitor",
            tenant_id="studio-a",
            project_id="game-a",
            scopes=["runs:read", "evidence:read"],
            allow_observation_investigations=True,
        )
        router = ObservationBatchRouter(
            repository,
            SemanticConversationRouter(repository),
            {("monitor", "studio-a", "game-a"): policy},
        )
        router.accept(observation(root))

        def fail(*args):
            raise RuntimeError("delivery failed")

        monkeypatch.setattr(router.feedback, "bind", fail)
        with pytest.raises(RuntimeError, match="delivery failed"):
            await router.recover_due()
        assert (
            len(
                router.router.rules.store.task_heads(
                    root.investigation_id, "studio-a", "game-a", limit=20
                )
            )
            == 1
        )
        assert (
            repository._connection.execute(
                "SELECT COUNT(*) FROM task_feedback_deliveries"
            ).fetchone()[0]
            == 0
        )

    asyncio.run(scenario())


def test_reply_binding_survives_recovery_and_old_payload_still_valid():
    from game_test_agent.events.router_service import ConversationRoutingService
    from game_test_agent.storage import canonical_json
    from game_test_agent.storage.repository import payload_hash

    for original in [
        {"content": "继续", "context_investigation_id": None},
        {
            "content": "继续",
            "context_investigation_id": None,
            "bound_investigation_id": "inv-" + "a" * 32,
        },
    ]:
        assert (
            ConversationRoutingService._recovery_original(
                canonical_json(original), payload_hash(original)
            )
            == original
        )
