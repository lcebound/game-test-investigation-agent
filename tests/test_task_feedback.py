"""Offline subscriptions, immutable batches, replay and native recall boundaries."""
import asyncio
import hashlib
import json
import time
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import Mock

import httpx
import pytest
from pydantic import ValidationError

from game_test_agent.api import create_app
from game_test_agent.contracts.models import InvestigationRequest, ReopenRequest
from game_test_agent.events.dispatchers import DurableDispatcher
from game_test_agent.events.feedback import RunFeedbackEvent, RunSubscription
from game_test_agent.events.ingress import EventIngress
from game_test_agent.events.policy import EventSourcePolicy
from game_test_agent.storage import ConflictError, Repository, canonical_json
from game_test_agent.storage.feedback import FeedbackStore
from game_test_agent.workflows.native_context import NativeMemory
from game_test_agent.workflows.native_recall import ContextRead
from native_plan_helpers import initial_plan_call
from test_events import event as anomaly
from test_native_recall import add_input, memory
from test_native_reliability import call, response, setup


RUN = {"provider_id": "lab-a", "project_id": "game-a", "run_id": "run-1"}
AUTH = {"X-Principal-ID": "tester", "X-Tenant-ID": "studio-a", "X-Project-ID": "game-a",
        "X-Scopes": "runs:read,evidence:read,events:write"}
POLICY = {("lab-a", "studio-a", "game-a"): EventSourcePolicy(
    source="lab-a", tenant_id="studio-a", project_id="game-a", scopes=[])}


def feedback(identity="one", kind="progress", **updates):
    now = datetime.now(timezone.utc).isoformat()
    raw = {"event_id": identity, "event_type": "game_test.run.feedback", "source": "lab-a",
           "tenant_id": "studio-a", "project_id": "game-a", "occurred_at": now, "published_at": now,
           "root_event_id": "root", "causation_id": "cause", "origin": "platform",
           "data": {"run_ref": RUN, "result_revision": 2, "kind": kind, "status": kind,
                    "summary": "平台状态通知", "details": {"raw": "UNTRUSTED_SECRET"}}}
    raw.update(updates)
    return RunFeedbackEvent.model_validate(raw)


def view(repository, key="task", *, project="game-a", run=RUN):
    request = InvestigationRequest(tenant_id="studio-a", project_id=project,
        objective={"objective_id": "inspect", "description": "调查运行"},
        constraints={"source_run_ref": run})
    result, _ = repository.create_investigation(request, key)
    FeedbackStore(repository).subscribe(result, RunSubscription(source="lab-a", run_ref=run))
    return result


def test_anomaly_binds_run_without_granting_additional_permissions(repository):
    investigation, _ = EventIngress(repository).accept(anomaly())
    subscriptions = FeedbackStore(repository).subscriptions(investigation.investigation_id)
    assert subscriptions[0]["run_id"] == "run-1"
    assert repository.get_authorization(investigation.investigation_id)["scopes"] == []
    with pytest.raises(ValueError, match="not configured"):
        EventIngress(repository).accept(feedback().model_dump_json().encode())
    result = EventIngress(repository, POLICY).accept(feedback().model_dump_json().encode())
    assert result["investigation_ids"] == [investigation.investigation_id]
    assert repository.get_authorization(investigation.investigation_id)["scopes"] == []


@pytest.mark.parametrize("change", [
    {"origin": "agent"}, {"occurred_at": "2026-11-02T12:00:00"},
    {"data": {"run_ref": {**RUN, "run_id": "x" * 257}, "result_revision": 1, "kind": "progress", "status": "ok"}},
])
def test_feedback_protocol_rejects_self_output_and_unbounded_identity(change):
    with pytest.raises(ValidationError):
        feedback(**change)


def test_scoped_fanout_is_idempotent_and_unmatched_feedback_creates_no_task(repository):
    first = view(repository, "one")
    second = view(repository, "two")
    foreign = view(repository, "foreign", project="other", run={**RUN, "project_id": "other"})
    unrelated = view(repository, "unrelated", run={**RUN, "run_id": "other"})
    store = FeedbackStore(repository)
    event = feedback()
    result = store.accept(event)
    assert set(result["investigation_ids"]) == {first.investigation_id, second.investigation_id}
    assert store.accept(event)["duplicate"]
    altered = event.model_copy(update={"causation_id": "changed"})
    with pytest.raises(ConflictError):
        store.accept(altered)
    assert not store.inline(foreign.investigation_id) and not store.inline(unrelated.investigation_id)
    unknown = feedback("unmatched").model_copy(update={"data": feedback().data.model_copy(update={"run_ref": feedback().data.run_ref.model_copy(update={"run_id": "no-task"})})})
    assert store.accept(unknown)["investigation_ids"] == []
    assert len(repository.list_investigations("studio-a", "game-a")) == 3


def test_subscription_requires_bound_run_and_unsubscribe_does_not_erase_history(repository):
    own = view(repository)
    store = FeedbackStore(repository)
    with pytest.raises(ValueError, match="already bound"):
        store.subscribe(own, RunSubscription(source="lab-a", run_ref={**RUN, "run_id": "other"}))
    store.accept(feedback())
    store.prepare_batches()
    subscription = store.subscriptions(own.investigation_id)[0]["subscription_id"]
    repository._connection.execute("UPDATE task_subscriptions SET active=0 WHERE subscription_id=?", (subscription,))
    assert not store.accept(feedback("later"))["investigation_ids"]
    assert store.inline(own.investigation_id)["items"]
    store.subscribe(own, RunSubscription(source="lab-a", run_ref=RUN))
    assert store.accept(feedback("resumed"))["investigation_ids"] == [own.investigation_id]


def test_burst_batches_are_bounded_and_keep_failure_recovery_and_counterevidence(repository):
    own = view(repository)
    store = FeedbackStore(repository)
    kinds = ["failed", "recovered", "counterevidence", "phase"] + ["progress"] * 57
    for index, kind in enumerate(kinds):
        store.accept(feedback(str(index), kind))
    while store.prepare_batches():
        pass
    rows = repository._connection.execute("SELECT packet_json FROM task_feedback_batches ORDER BY generation").fetchall()
    cards = []
    for row in rows:
        packet = json.loads(row["packet_json"])
        assert len(packet["items"]) <= 20 and len(canonical_json(packet).encode()) <= 8192
        cards.extend(packet["items"])
    assert len(cards) == len(kinds)
    assert len({c["read_ref"] for c in cards}) == len(kinds)
    assert len(rows) < len(kinds) / 2
    assert {c["kind"] for c in store.inline(own.investigation_id)["items"]} >= {"failed", "recovered", "counterevidence", "phase"}
    assert "UNTRUSTED_SECRET" not in canonical_json(store.inline(own.investigation_id))
    assert len(canonical_json(store.inline(own.investigation_id)).encode()) < 3072


def test_old_dispatch_ack_cannot_remove_a_new_generation_and_cancel_is_first(repository):
    async def scenario():
        own = view(repository)
        store = FeedbackStore(repository)
        store.accept(feedback())
        received = []
        async def signal(workflow, kind, identity, payload):
            received.append(identity)
            if len(received) == 1:
                store.accept(feedback("during-send", "failed"))
                store.prepare_batches()
        dispatcher = DurableDispatcher(repository, Mock(signal=signal), None)
        assert await dispatcher.dispatch_commands() == 1
        pending = repository.pending_commands()
        assert len(pending) == 1 and pending[0]["command_id"] != received[0]
        repository.enqueue_command(own.investigation_id, "cancel-now", "cancel", {"command_id": "cancel-now"})
        assert repository.pending_commands(limit=1)[0]["command_id"] == "cancel-now"
        assert await dispatcher.dispatch_commands() == 2
        assert received[1] == "cancel-now"
        assert not repository.pending_commands()
    asyncio.run(scenario())


def test_user_input_changes_form_batch_boundaries_with_stale_marking(repository):
    own = view(repository)
    store = FeedbackStore(repository)
    store.accept(feedback("before", "failed"))
    add_input(repository, own, "input-new", "改查新的环境")
    store.accept(feedback("after", "recovered"))
    assert store.prepare_batches() == 1
    assert store.inline(own.investigation_id)["input_stale"]
    assert store.prepare_batches() == 1
    inline = store.inline(own.investigation_id)
    assert not inline["input_stale"]
    assert next(c for c in inline["items"] if c["kind"] == "failed")["input_stale"]
    assert not next(c for c in inline["items"] if c["kind"] == "recovered")["input_stale"]


def test_raw_recall_is_utf8_hash_checked_paged_and_investigation_scoped(repository):
    own = view(repository)
    other = view(repository, "other", run={**RUN, "run_id": "other"})
    event = feedback().model_copy(update={"data": feedback().data.model_copy(update={"details": {"text": "中文" * 800 + "TARGET"}})})
    store = FeedbackStore(repository)
    store.accept(event)
    store.prepare_batches()
    directory = store.read(own.investigation_id, ContextRead(ref="feedbacks"))
    ref = directory["items"][0]["read_ref"]
    fragments, offset = [], 0
    while True:
        piece = store.read(own.investigation_id, ContextRead(ref=ref, byte_offset=offset, max_bytes=128))
        fragments.append(piece["result"])
        if piece["next_byte_offset"] is None:
            break
        offset = piece["next_byte_offset"]
    text = "".join(fragments)
    assert text == canonical_json(event)
    assert hashlib.sha256(text.encode()).hexdigest() == directory["items"][0]["sha256"]
    with pytest.raises(PermissionError):
        store.read(other.investigation_id, ContextRead(ref=ref))
    with pytest.raises(ValueError, match="hash"):
        store.read(own.investigation_id, ContextRead(ref=ref, expected_sha256="0" * 64))
    assert "TARGET" in store.read(own.investigation_id, ContextRead(ref=ref, query="TARGET"))["result"]
    with pytest.raises(ValueError):
        store.read(own.investigation_id, ContextRead(ref="feedbacks", offset=2**80))


def test_restart_retains_delivery_and_acceptance_receipt(settings):
    repo = Repository(settings.database_path)
    own = view(repo)
    event = feedback()
    FeedbackStore(repo).accept(event)
    repo.close()
    repo = Repository(settings.database_path)
    try:
        store = FeedbackStore(repo)
        assert store.accept(event)["duplicate"]
        assert store.prepare_batches() == 1
        assert repo.pending_commands()[0]["payload"]["event_count"] == 1
        assert store.read(own.investigation_id, ContextRead(ref="feedbacks"))["items"]
    finally:
        repo.close()


def test_native_feedback_context_and_directory_cache_follow_new_batches(repository, settings):
    async def scenario():
        own, controller, _ = setup(repository, settings, constraints={"source_run_ref": RUN})
        store = FeedbackStore(repository)
        store.subscribe(own, RunSubscription(source="lab-a", run_ref=RUN))
        store.accept(feedback())
        store.prepare_batches()
        state = memory()
        assert state.sync_inputs(repository, own.investigation_id)
        assert not state.sync_inputs(repository, own.investigation_id)
        working = json.loads(state.messages[2]["content"])
        assert working["external_run_feedback"]["items"] and not working["latest_user_inputs"]
        payload, _ = await controller._execute_call(own, call("agent_read_context", {"ref": "feedbacks"}, "dir-1"), {}, state, time.time() + 30)
        assert len(payload["items"]) == 1
        raw, progress = await controller._execute_call(own, call("agent_read_context", {"ref": payload["items"][0]["read_ref"]}, "raw-1"), {}, state, time.time() + 30)
        assert progress and raw["resource_kind"] == "run_feedback"
        reads = repository.budget_usage(own.investigation_id)["tool_reads"]
        reused, progress = await controller._execute_call(own, call("agent_read_context", {"ref": payload["items"][0]["read_ref"]}, "raw-cached"), {}, state, time.time() + 30)
        assert reused == raw and not progress
        assert repository.budget_usage(own.investigation_id)["tool_reads"] == reads
        assert not repository.list_current_evidence(own.investigation_id)
        store.accept(feedback("next", "recovered"))
        store.prepare_batches()
        later, _ = await controller._execute_call(own, call("agent_read_context", {"ref": "feedbacks"}, "dir-2"), {}, state, time.time() + 30)
        assert len(later["items"]) == 2
        state.sync_inputs(repository, own.investigation_id)
        state.save(repository, controller.artifacts, own)
        restored = NativeMemory.restore(repository, controller.artifacts, own)
        assert restored.feedback_read_ranges == state.feedback_read_ranges
    asyncio.run(scenario())


def test_native_worker_sees_feedback_and_exposes_recall_without_user_input(repository, settings):
    async def scenario():
        own, controller, _ = setup(repository, settings, constraints={"source_run_ref": RUN})
        store = FeedbackStore(repository)
        store.subscribe(own, RunSubscription(source="lab-a", run_ref=RUN))
        store.accept(feedback(kind="failed"))
        store.prepare_batches()
        async def model(messages, tools):
            working = json.loads(messages[2]["content"])
            assert working["external_run_feedback"]["items"][0]["kind"] == "failed"
            assert "agent_read_context" in {t["function"]["name"] for t in tools}
            return response(initial_plan_call(), call())
        controller._model_call = model
        result = await controller.run_step(own.investigation_id, principal_id="tester", scopes={"runs:read", "evidence:read"})
        assert result.state == "continue" and result.continuation.next_turn == 2
    asyncio.run(scenario())


def test_local_api_pump_delivers_durable_feedback_without_mq_and_checks_scope(settings):
    async def scenario():
        configured = replace(settings, event_source_policies_json=json.dumps([p.model_dump() for p in POLICY.values()]), dispatcher_poll_seconds=.1)
        app = create_app(configured)
        async with app.router.lifespan_context(app):
            repo = app.state.container.repository
            own = view(repo)
            app.state.container.workflows.start = Mock()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                event = feedback()
                assert (await client.post("/v1/events/run-feedback", headers={**AUTH, "X-Scopes": "runs:read"}, json=event.model_dump(mode="json"))).status_code == 403
                assert (await client.post("/v1/events/run-feedback", headers=AUTH, json=event.model_dump(mode="json"))).status_code == 202
                for _ in range(50):
                    if app.state.container.workflows.start.called:
                        break
                    await asyncio.sleep(.02)
                app.state.container.workflows.start.assert_called_with(own.investigation_id)
                assert not repo.pending_commands()
                path = f"/v1/investigations/{own.investigation_id}"
                refs = (await client.get(path + "/feedback", headers=AUTH)).json()["items"]
                cursor = refs[0]["read_ref"].split("/")[1]
                assert (await client.get(path + "/feedback/" + cursor, headers=AUTH)).json()["result"] == canonical_json(event)
                assert (await client.get(path + "/feedback/" + cursor, headers={**AUTH, "X-Project-ID": "other"})).status_code == 403
                subscription = (await client.get(path + "/subscriptions", headers=AUTH)).json()["items"][0]
                assert (await client.delete(path + "/subscriptions/" + subscription["subscription_id"], headers=AUTH)).status_code == 200
                assert (await client.post("/v1/events/run-feedback", headers=AUTH, json=feedback("unsubscribed").model_dump(mode="json"))).json()["investigation_ids"] == []
    asyncio.run(scenario())


def test_followup_inherits_only_active_subscription_for_its_source_run(repository):
    own = view(repository)
    store = FeedbackStore(repository)
    # An older action's run must not be copied into the next investigation implicitly.
    with repository.transaction() as connection:
        from game_test_agent.contracts.models import RunRef
        store.bind(connection, own.investigation_id, "other-source", RunRef(**{**RUN, "run_id": "older-action"}), "studio-a", "game-a")
        connection.execute("UPDATE investigations SET status='completed' WHERE investigation_id=?", (own.investigation_id,))
    child, _ = repository.reopen_investigation(own.investigation_id, ReopenRequest(reason="follow up", budget=own.request.budget), "followup")
    subscriptions = store.subscriptions(child.investigation_id)
    assert len(subscriptions) == 1 and subscriptions[0]["run_id"] == RUN["run_id"]
    result = store.accept(feedback())
    assert result["investigation_ids"] == [child.investigation_id]
    assert not store.inline(own.investigation_id)
    changed, _ = repository.reopen_investigation(own.investigation_id,
        ReopenRequest(reason="new target", budget=own.request.budget,
                      constraints={"source_run_ref": {**RUN, "run_id": "different"}}), "different-followup")
    assert not store.subscriptions(changed.investigation_id)


def test_hash_damage_rejects_batch_and_recall_but_json_whitespace_is_compatible(repository):
    own = view(repository)
    store = FeedbackStore(repository)
    event = feedback()
    accepted = store.accept(event)
    # MySQL's JSON whitespace is not part of the immutable canonical content.
    repository._connection.execute("UPDATE task_feedback_events SET payload_json=? WHERE event_key=?",
        (json.dumps(event.model_dump(mode="json"), indent=2), accepted["event_key"]))
    assert store.prepare_batches() == 1
    ref = store.read(own.investigation_id, ContextRead(ref="feedbacks"))["items"][0]["read_ref"]
    assert store.read(own.investigation_id, ContextRead(ref=ref))["result"] == canonical_json(event)
    repository._connection.execute("UPDATE task_feedback_events SET payload_json=? WHERE event_key=?",
        ("{}", accepted["event_key"]))
    with pytest.raises(ValueError, match="hash mismatch"):
        store.read(own.investigation_id, ContextRead(ref=ref))
    another = store.accept(feedback("bad-before-seal"))
    repository._connection.execute("UPDATE task_feedback_events SET payload_json=? WHERE event_key=?", ("{}", another["event_key"]))
    assert store.prepare_batches() == 1
    assert store.generation(own.investigation_id) == 2
    assert next(c for c in store.inline(own.investigation_id)["items"] if c["kind"] == "unavailable")["unavailable"]


def test_feedback_arriving_during_inference_supersedes_the_stale_tool_group(repository, settings):
    async def scenario():
        own, controller, provider = setup(repository, settings, constraints={"source_run_ref": RUN})
        store = FeedbackStore(repository)
        store.subscribe(own, RunSubscription(source="lab-a", run_ref=RUN))
        async def first_model(messages, tools):
            store.accept(feedback(kind="counterevidence"))
            store.prepare_batches()
            return response(initial_plan_call(), call())
        controller._model_call = first_model
        first = await controller.run_step(own.investigation_id, principal_id="tester", scopes={"runs:read", "evidence:read"})
        assert first.state == "continue"
        provider.call_native.assert_not_awaited()
        async def next_model(messages, tools):
            assert json.loads(messages[2]["content"])["external_run_feedback"]["items"][0]["kind"] == "counterevidence"
            return response(initial_plan_call(), call(ident="fresh"))
        controller._model_call = next_model
        second = await controller.run_step(own.investigation_id, principal_id="tester", scopes={"runs:read", "evidence:read"})
        assert second.state == "continue" and second.continuation.next_turn == 3
        provider.call_native.assert_awaited_once()
    asyncio.run(scenario())


def test_checkpoint_four_restores_old_three_without_feedback_fields(repository, settings):
    from dataclasses import asdict
    own, controller, _ = setup(repository, settings)
    state = memory()
    state.save(repository, controller.artifacts, own)
    assert repository.list_recent_events_by_type(own.investigation_id, "mcp.native.checkpoint", limit=1)[0]["data"]["version"] == 6
    old_state = asdict(state)
    old_state.pop("external_feedback")
    old_state.pop("feedback_read_ranges")
    artifact = controller.artifacts.put_bytes("studio-a", "game-a", canonical_json({"investigation_id": own.investigation_id, "state": old_state}).encode())
    repository.record_investigation_event(own.investigation_id, "mcp.native.checkpoint", {"version": 3, "artifact_ref": artifact.ref})
    restored = NativeMemory.restore(repository, controller.artifacts, own)
    assert restored.external_feedback == {} and restored.feedback_read_ranges == {}


def test_cancel_dispatch_precedes_even_a_failing_batch_builder(repository, monkeypatch):
    async def scenario():
        own = view(repository)
        repository.enqueue_command(own.investigation_id, "cancel-first", "cancel", {"command_id": "cancel-first"})
        async def signal(workflow_id, kind, command_id, payload):
            assert kind == "cancel" and command_id == "cancel-first"
        dispatcher = DurableDispatcher(repository, Mock(signal=signal), None)
        monkeypatch.setattr(FeedbackStore, "prepare_batches", Mock(side_effect=ValueError("unavailable")))
        with pytest.raises(ValueError):
            await dispatcher.dispatch_commands()
        assert not repository.pending_commands()
    asyncio.run(scenario())
