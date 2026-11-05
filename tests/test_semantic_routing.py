"""Semantic addressing is a bounded proposal, never worker execution authority."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from datetime import timedelta

import httpx
import pytest
from pydantic import ValidationError

from game_test_agent.api import create_app
from game_test_agent.contracts.models import ModelAttempt, ModelUsage
from game_test_agent.events.semantic_router import (
    RouterModelAssessor, SemanticAddress, SemanticConversationRouter,
)
from game_test_agent.models import GatewayResult, ModelProviderResponse
from game_test_agent.storage import NotFoundError
from game_test_agent.storage.workbench import WorkbenchStore
from test_conversation_tasks import AUTH, finish, linked, request


def assessment(kind="task", task=None, reference="刚才那个登录问题"):
    return GatewayResult(
        output=SemanticAddress(kind=kind, task_id=task, reference=reference),
        attempts=[ModelAttempt(attempt=1, status="valid", usage=ModelUsage(input_tokens=100, output_tokens=20))],
        raw_output_ref="sha256:test", normalization_log=[], usage=ModelUsage(input_tokens=100, output_tokens=20),
    )


def router(repository, result=None, error=None):
    assessor = SimpleNamespace(assess=AsyncMock(return_value=result, side_effect=error))
    return SemanticConversationRouter(repository, assessor), assessor


def resolve(service, root, content="回到刚才那个登录问题，补充一份日志", context=None):
    return asyncio.run(service.resolve(root.investigation_id, content, "studio-a", "game-a",
        context_investigation_id=context.investigation_id if context else None))


def test_natural_reference_uses_only_scoped_short_cards(repository, monkeypatch):
    root, _ = repository.create_investigation(request("CPU高负载", constraints={"private": "SECRET"}), "root")
    sibling = linked(repository, root.investigation_id, text="登录时崩溃")
    service, assessor = router(repository, assessment(task=sibling.investigation_id))
    for method in ("get_latest_report", "list_recent_events", "list_evidence"):
        if hasattr(repository, method):
            monkeypatch.setattr(repository, method, Mock(side_effect=AssertionError("no worker context")))
    result = resolve(service, root, context=root)
    assert result["task_id"] == sibling.investigation_id
    assert result["basis"] == "semantic_reference"
    payload, scope = assessor.assess.call_args.args
    assert scope == ("studio-a", "game-a")
    assert all(set(card) == {"task_id", "investigation_id", "objective_excerpt", "status"} for card in payload["tasks"])
    assert "SECRET" not in str(payload)
    assert result["router_assessment"] == {"attempts": 1, "input_tokens": 100, "output_tokens": 20, "estimated_cost": 0}
    assert not repository.list_user_inputs(sibling.investigation_id)


@pytest.mark.parametrize("message", ['继续「登录时崩溃」：补日志', "新任务：调查登录崩溃", '继续「不存在」：补充', '继续「引号未闭合'])
def test_explicit_or_broken_addresses_never_get_model_override(repository, message):
    root, _ = repository.create_investigation(request(), "root")
    linked(repository, root.investigation_id, text="登录时崩溃")
    service, assessor = router(repository, error=AssertionError("rules must win"))
    assert resolve(service, root, message, root)["router_version"] == "rules-v1"
    assessor.assess.assert_not_called()


@pytest.mark.parametrize("message", ["继续", "补充一下，发生在战斗场景", "这个结论不对，先看日志"])
def test_owned_followup_stays_with_worker_without_model_call(repository, message):
    root, _ = repository.create_investigation(request(), "root")
    service, assessor = router(repository)
    assert asyncio.run(service.resolve(root.investigation_id, message, "studio-a", "game-a", bound_investigation_id=root.investigation_id))["basis"] == "current_task"
    assessor.assess.assert_not_called()


def test_new_task_keeps_original_text_not_a_model_plan(repository):
    root, _ = repository.create_investigation(request(), "root")
    text = "另外单独调查一下登录闪退，先只看现有日志"
    service, _ = router(repository, assessment("new_task", reference="另外单独调查"))
    result = resolve(service, root, text, root)
    assert result["goal"] == text and result["kind"] == "new_task"
    assert len(repository.list_investigations("studio-a", "game-a")) == 1


@pytest.mark.parametrize("kind", ["current", "clarify"])
def test_current_and_uncertain_proposals(repository, kind):
    root, _ = repository.create_investigation(request(), "root")
    service, _ = router(repository, assessment(kind, reference=""))
    result = resolve(service, root, context=root)
    assert result["kind"] == ("task" if kind == "current" else "clarify")
    if kind == "current":
        assert result["investigation_id"] == root.investigation_id


@pytest.mark.parametrize("foreign,reference", [(True, "刚才那个登录问题"), (False, "用户没有说这句话")])
def test_foreign_id_and_fabricated_quote_cannot_route(repository, foreign, reference):
    root, _ = repository.create_investigation(request(), "root")
    target = repository.create_investigation(request(), "outside")[0] if foreign else linked(repository, root.investigation_id)
    service, _ = router(repository, assessment(task=target.investigation_id, reference=reference))
    assert resolve(service, root, context=root)["kind"] == "clarify"


def test_foreign_context_rejected_before_assessment(repository):
    root, _ = repository.create_investigation(request(), "root")
    outside, _ = repository.create_investigation(request(), "outside")
    service, assessor = router(repository)
    with pytest.raises(NotFoundError):
        resolve(service, root, context=outside)
    assessor.assess.assert_not_called()


def test_duplicate_titles_do_not_acquire_false_uniqueness(repository):
    root, _ = repository.create_investigation(request("登录崩溃"), "root")
    sibling = linked(repository, root.investigation_id, text="登录崩溃")
    service, _ = router(repository, assessment(task=sibling.investigation_id))
    assert resolve(service, root, context=root)["kind"] == "clarify"


def test_large_directory_is_not_a_partial_model_view(repository):
    root, _ = repository.create_investigation(request(), "root")
    for i in range(20):
        linked(repository, root.investigation_id, str(i))
    service, assessor = router(repository)
    assert resolve(service, root, context=root)["basis"] == "directory_overflow"
    assessor.assess.assert_not_called()


def test_stopped_target_is_not_resumed_by_semantic_reference(repository):
    root, _ = repository.create_investigation(request(), "root")
    sibling = linked(repository, root.investigation_id, text="登录崩溃")
    finish(repository, sibling)
    service, _ = router(repository, assessment(task=sibling.investigation_id))
    assert resolve(service, root, context=root)["kind"] == "clarify"


def test_catalog_changes_during_call_are_not_silently_rebound(repository):
    root, _ = repository.create_investigation(request(), "root")
    sibling = linked(repository, root.investigation_id, text="登录崩溃")
    service, assessor = router(repository)

    async def assess(*args):
        finish(repository, sibling)
        return assessment(task=sibling.investigation_id)

    assessor.assess.side_effect = assess
    assert resolve(service, root, context=root)["basis"] == "tasks_changed"


def test_unavailable_router_does_not_silently_send_to_current(repository):
    root, _ = repository.create_investigation(request(), "root")
    service, _ = router(repository, error=RuntimeError("SECRET credentials"))
    result = resolve(service, root, context=root)
    assert result["kind"] == "clarify" and "SECRET" not in str(result)


def test_timeout_and_cancellation_are_bounded(repository):
    root, _ = repository.create_investigation(request(), "root")
    service, assessor = router(repository)
    service.timeout_seconds = .01

    async def slow(*args):
        await asyncio.Event().wait()

    assessor.assess.side_effect = slow
    assert resolve(service, root, context=root)["basis"] == "assessment_unavailable"
    assessor.assess.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        resolve(service, root, context=root)


@pytest.mark.parametrize("payload", [
    {"kind": "task", "task_id": "inv-x", "reference": ""},
    {"kind": "current", "task_id": "inv-x"},
    {"kind": "new_task", "reference": "单独调查", "budget": 100},
    {"kind": "stop"},
])
def test_model_cannot_add_execution_fields(payload):
    with pytest.raises(ValidationError):
        SemanticAddress.model_validate(payload)


def test_gateway_budget_rejects_oversize_before_provider(settings):
    assessor = RouterModelAssessor(replace(settings, model_max_total_tokens=200), None)
    assessor.gateway._caller = Mock(side_effect=AssertionError("must not call"))
    with pytest.raises(ValueError, match="budget"):
        asyncio.run(assessor.assess({"message": "x" * 1000}, ("studio-a", "game-a")))
    assessor.gateway._caller.assert_not_called()


def test_gateway_records_malformed_output_usage_without_retry(settings):
    from game_test_agent.models import ModelCallError

    assessor = RouterModelAssessor(settings, None)
    assessor.gateway._caller = AsyncMock(return_value=ModelProviderResponse(
        content='{"kind":"stop"}', usage=ModelUsage(input_tokens=100, output_tokens=20)))
    with pytest.raises(ModelCallError) as error:
        asyncio.run(assessor.assess({"message": "回到登录问题"}, ("studio-a", "game-a")))
    assert len(error.value.attempts) == 1
    assert error.value.attempts[0].usage.output_tokens == 20
    assessor.gateway._caller.assert_awaited_once()


@pytest.mark.parametrize("api_mode", ["responses", "chat_completions"])
@pytest.mark.parametrize("truncated", [False, True])
def test_transport_is_tool_free_bounded_and_does_not_inherit_history(settings, monkeypatch, api_mode, truncated):
    import openai
    from game_test_agent.models import ModelCallError

    content = '{"kind":"current","task_id":null,"reference":""}'
    raw = SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="length" if truncated else "stop", message=SimpleNamespace(content=content))],
        status="incomplete" if truncated else "completed", output_text=content,
        usage=SimpleNamespace(prompt_tokens=90, completion_tokens=12, input_tokens=90, output_tokens=12),
    )
    call = AsyncMock(return_value=raw)
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=call)),
        responses=SimpleNamespace(create=call))
    context = AsyncMock()
    context.__aenter__.return_value = client
    factory = Mock(return_value=context)
    monkeypatch.setattr(openai, "AsyncOpenAI", factory)
    configured = replace(settings, model_api=api_mode,
        model_extra_body_json='{"max_tokens":99999,"tools":[{}],"previous_response_id":"secret","enable_thinking":false}')
    assessor = RouterModelAssessor(configured, None)
    if truncated:
        with pytest.raises(ModelCallError) as error:
            asyncio.run(assessor.assess({"message": "回到之前的问题"}, ("studio-a", "game-a")))
        assert error.value.attempts[0].usage.output_tokens == 12
    else:
        result = asyncio.run(assessor.assess({"message": "回到之前的问题"}, ("studio-a", "game-a")))
        assert result.output.kind == "current" and result.usage.input_tokens == 90
    assert factory.call_args.kwargs["max_retries"] == 0
    arguments = call.call_args.kwargs
    assert "tools" not in arguments and "previous_response_id" not in arguments
    assert arguments["extra_body"] == {"enable_thinking": False}
    assert arguments.get("max_tokens", arguments.get("max_output_tokens")) == 512
    call.assert_awaited_once()


def test_receipt_revalidation_catches_expiry_and_stopped_target(repository, monkeypatch):
    root, _ = repository.create_investigation(request(), "root")
    sibling = linked(repository, root.investigation_id, text="登录崩溃")
    service, _ = router(repository, assessment(task=sibling.investigation_id))
    decision = resolve(service, root, context=root)
    assert service.revalidate(decision, "studio-a", "game-a") == decision
    original = repository.get_investigation

    def expired(identity):
        view = original(identity)
        return view.model_copy(update={"created_at": view.created_at - timedelta(days=10)})

    monkeypatch.setattr(repository, "get_investigation", expired)
    assert service.revalidate(decision, "studio-a", "game-a")["kind"] == "clarify"
    monkeypatch.setattr(repository, "get_investigation", original)
    finish(repository, sibling)
    assert service.revalidate(decision, "studio-a", "game-a")["basis"] == "tasks_changed"


def test_failed_gateway_usage_is_retained_in_public_audit(repository):
    from game_test_agent.models import ModelCallError

    root, _ = repository.create_investigation(request(), "root")
    error = ModelCallError("SECRET provider error", [ModelAttempt(attempt=1, status="schema_error",
        error="SECRET raw output", usage=ModelUsage(input_tokens=90, output_tokens=12))])
    service, _ = router(repository, error=error)
    result = resolve(service, root, context=root)
    assert result["router_assessment"]["output_tokens"] == 12 and "SECRET" not in str(result)


def test_api_retries_coalesce_without_holding_a_transaction(settings):
    async def scenario():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            repo = app.state.container.repository
            app.state.container.workflows.start = Mock()
            root, _ = repo.create_investigation(request("CPU高负载"), "root")
            sibling = linked(repo, root.investigation_id, text="登录崩溃")
            entered, release = asyncio.Event(), asyncio.Event()

            async def assess(*args):
                assert not repo._connection.in_transaction
                entered.set()
                await release.wait()
                return assessment(task=sibling.investigation_id)

            app.state.message_router.assessor = SimpleNamespace(assess=AsyncMock(side_effect=assess))
            path = f"/v1/conversations/{root.investigation_id}/messages/route"
            body = {"content": "回到刚才那个登录问题，补充日志", "context_investigation_id": root.investigation_id}
            headers = {**AUTH, "Idempotency-Key": "semantic-retry"}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                first = asyncio.create_task(client.post(path, headers=headers, json=body))
                await asyncio.wait_for(entered.wait(), 2)
                second = asyncio.create_task(client.post(path, headers=headers, json=body))
                release.set()
                responses = await asyncio.gather(first, second)
                assert all(response.status_code == 200 for response in responses)
                assert sorted(response.json()["duplicate"] for response in responses) == [False, True]
                assert all(response.json()["task_id"] == sibling.investigation_id for response in responses)
                app.state.message_router.assessor.assess.assert_awaited_once()
                assert not repo.list_user_inputs(sibling.investigation_id)
                app.state.container.workflows.start.assert_not_called()
                conflict = await client.post(path, headers=headers, json={**body, "content": "另一条消息"})
                assert conflict.status_code == 409
                receipt = repo._connection.execute("SELECT response_json FROM workbench_message_receipts").fetchone()
                assert body["content"] in receipt["response_json"]
    asyncio.run(scenario())


def test_archive_during_model_call_rejects_receipt(settings):
    async def scenario():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            repo = app.state.container.repository
            root, _ = repo.create_investigation(request(), "root")

            async def assess(*args):
                store = WorkbenchStore(repo)
                summary = store.get(root.investigation_id, "studio-a", "game-a")
                store.update(root.investigation_id, "studio-a", "game-a", summary["revision"], archived=True)
                return assessment("current", reference="")

            app.state.message_router.assessor = SimpleNamespace(assess=assess)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(f"/v1/conversations/{root.investigation_id}/messages/route",
                    headers={**AUTH, "Idempotency-Key": "archive"}, json={"content": "回到之前问题", "context_investigation_id": root.investigation_id})
                assert response.status_code == 409
                assert repo._connection.execute("SELECT COUNT(*) FROM workbench_message_receipts").fetchone()[0] == 0
    asyncio.run(scenario())


def test_router_configuration_is_explicit_and_finite(settings, monkeypatch):
    with pytest.raises(ValueError, match="requires"):
        replace(settings, enable_semantic_router=True).validate()
    for timeout in (0, -1, float("nan"), float("inf"), 61):
        with pytest.raises(ValueError, match="router timeout"):
            replace(settings, router_timeout_seconds=timeout).validate()
    monkeypatch.setenv("GTA_ENABLE_MODEL", "true")
    monkeypatch.setenv("GTA_ENABLE_SEMANTIC_ROUTER", "true")
    monkeypatch.setenv("GTA_ROUTER_TIMEOUT_SECONDS", "5")
    configured = type(settings).from_env()
    assert configured.enable_semantic_router and configured.router_timeout_seconds == 5
