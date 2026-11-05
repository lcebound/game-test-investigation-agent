"""Regression cases from the complete harness review; no external processes."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from mcp import types
import pytest

from game_test_agent.storage import LocalArtifactStore
from game_test_agent.reports import ReportService
from game_test_agent.workflows.native_context import NativeMemory
from game_test_agent.workflows.native_mcp import NativeMcpController
from game_test_agent.runtime.execution import ExecutionLimits
from test_harness_completion import operations_setup, observation_setup, observation


def controller_setup(repository, settings):
    view, provider, tool, _ = operations_setup(repository)
    artifacts = LocalArtifactStore(settings.artifact_root)
    controller = NativeMcpController(
        settings,
        repository,
        SimpleNamespace(providers={"jobs": provider}),
        artifacts,
        ReportService(repository, artifacts),
    )
    memory = NativeMemory(
        messages=[
            {"role": "system", "content": "review"},
            {"role": "user", "content": "review"},
            {"role": "user", "content": "{}"},
        ]
    )
    return view, provider, tool, controller, memory


@pytest.mark.parametrize("second_status", ["completed", "failed"])
def test_independent_equal_argument_results_are_each_ingested_once(
    repository, settings, second_status
):
    async def scenario():
        view, provider, tool, controller, memory = controller_setup(repository, settings)
        provider.call_native.side_effect = [
            types.CallToolResult(
                content=[], structured_content={"operation_id": identity, "status": "running"}
            )
            for identity in ("one", "two")
        ]
        provider.call_operation_control.side_effect = [
            types.CallToolResult(
                content=[],
                structured_content={
                    "operation_id": identity,
                    "status": status,
                    "result": {"measurement": value},
                },
            )
            for identity, value, status in [("one", 47, "completed"), ("two", 99, second_status)]
        ]
        for identity in ("first", "second"):
            call = SimpleNamespace(
                id=identity, function=SimpleNamespace(name="scan", arguments="{}")
            )
            await controller._execute_call_unbounded(
                view, call, {"scan": (provider, tool)}, memory, time.time() + 30
            )
        await controller.native_operations.resume(
            view, {"scan": (provider, tool)}, memory, time.time() + 30
        )
        assert not controller.native_operations.pending(view.investigation_id)
        evidence = repository.list_current_evidence(view.investigation_id)
        assert {
            json.loads(controller.artifacts.read_bytes(e.artifact_ref))["measurement"]
            for e in evidence
        } == {47, 99}
        restored = NativeMemory.restore(repository, controller.artifacts, view)
        await controller.native_operations.resume(
            view, {"scan": (provider, tool)}, restored, time.time() + 30
        )
        assert len(repository.list_current_evidence(view.investigation_id)) == 2
        assert provider.call_native.await_count == 2
        assert provider.call_operation_control.await_count == 2

    asyncio.run(scenario())


def test_cleanup_rotates_unavailable_providers(repository):
    async def scenario():
        view, provider, tool, jobs = operations_setup(repository)
        for index in range(5):
            await jobs.execute(view, str(index), provider, tool, {})
        with repository.transaction() as connection:
            connection.execute(
                "UPDATE native_tool_jobs SET provider_id='missing' WHERE call_id IN ('0','1','2','3')"
            )
        provider.call_operation_control.return_value = types.CallToolResult(
            content=[], structured_content={"status": "canceled"}
        )
        await jobs.cancel_investigation(view.investigation_id)
        provider.call_operation_control.assert_not_awaited()
        await jobs.cancel_investigation(view.investigation_id)
        provider.call_operation_control.assert_awaited_once()
        assert len(repository.pending_cleanup_resources(view.investigation_id)) == 4

    asyncio.run(scenario())


def test_expired_resume_sends_no_query_or_ingestion(repository):
    async def scenario():
        view, provider, tool, jobs = operations_setup(repository)
        await jobs.execute(view, "first", provider, tool, {})
        memory = NativeMemory()
        memory.save = lambda *args: None
        await jobs.resume(view, {"scan": (provider, tool)}, memory, time.time() - 1)
        provider.call_operation_control.assert_not_awaited()
        jobs.controller._execute_call_unbounded.assert_not_awaited()
        assert memory.finalize

    asyncio.run(scenario())


def test_resume_obeys_tool_capacity_without_consuming_poll_budget(repository):
    async def scenario():
        view, provider, tool, jobs = operations_setup(repository)
        await jobs.execute(view, "first", provider, tool, {})
        limits = jobs.controller.execution_limits = ExecutionLimits(tools=1, max_waiters=0)
        memory = NativeMemory()
        memory.save = lambda *args: None
        before = repository.budget_usage(view.investigation_id).get("tool_reads", 0)
        async with limits.slot("tools"):
            await jobs.resume(view, {"scan": (provider, tool)}, memory, time.time() + 30)
        provider.call_operation_control.assert_not_awaited()
        assert repository.budget_usage(view.investigation_id).get("tool_reads", 0) == before

    asyncio.run(scenario())


def test_local_terminal_cleanup_resumes_without_rerunning_investigation(repository):
    from test_worker_ownership import backend
    from test_conversation_overview import close

    async def scenario():
        view, provider, tool, jobs = operations_setup(repository)
        await jobs.execute(view, "first", provider, tool, {})
        close(repository, view)
        provider.call_operation_control.return_value = types.CallToolResult(
            content=[], structured_content={"status": "canceled"}
        )
        controller = SimpleNamespace(native_operations=jobs, run_step=AsyncMock())
        runner = backend(repository, controller)
        await runner.reconcile_terminal_cleanup()
        assert not repository.pending_cleanup_resources(view.investigation_id)
        assert repository.get_investigation(view.investigation_id).status == "completed"
        controller.run_step.assert_not_awaited()

    asyncio.run(scenario())


def test_retry_uses_new_batch_identity_and_keeps_original_event(settings):
    import httpx
    from game_test_agent.api import create_app
    from test_conversation_overview import AUTH

    async def scenario():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            repo = app.state.container.repository
            root, target, router = observation_setup(repo)
            app.state.routing_service.observations = router
            event = observation(root)
            key = router.accept(event)["event_key"]
            router.router.assessor.assess.return_value.output.kind = "clarify"
            router.router.assessor.assess.return_value.output.task_id = None
            router.router.assessor.assess.return_value.output.reference = ""
            await router.recover_due()
            old = repo._connection.execute(
                "SELECT * FROM coordination_observations WHERE event_key=?", (key,)
            ).fetchone()
            assert old["state"] == "unresolved"
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                path = f"/v1/conversations/{root.investigation_id}/observations/{key}/retry"
                assert (await client.post(path, headers=AUTH)).status_code == 403
                assert (
                    await client.post(path, headers={**AUTH, "X-Scopes": "events:write"})
                ).status_code == 200
                assert (
                    await client.post(path, headers={**AUTH, "X-Scopes": "events:write"})
                ).json()["duplicate"]
                from test_semantic_routing import assessment

                router.router.assessor.assess.return_value = assessment(
                    task=target.investigation_id, reference="登录崩溃"
                )
                await router.recover_due()
                new = repo._connection.execute(
                    "SELECT * FROM coordination_observations WHERE event_key=?", (key,)
                ).fetchone()
                assert new["state"] == "routed" and new["batch_id"] != old["batch_id"]
                assert (
                    new["payload_json"] == old["payload_json"]
                    and new["payload_hash"] == old["payload_hash"]
                )
                response = await client.get(
                    f"/v1/investigations/{target.investigation_id}/operations", headers=AUTH
                )
                assert response.status_code == 200 and response.json()["items"] == []
                assert (
                    await client.get(
                        f"/v1/investigations/{target.investigation_id}/operations",
                        headers={**AUTH, "X-Project-ID": "other"},
                    )
                ).status_code == 404

    asyncio.run(scenario())


def test_synthesis_reuses_exact_source_versions_without_repeating_model_call(repository):
    from game_test_agent.memory.synthesis import SynthesisService, SynthesisRequest
    from test_conversation_overview import create, close

    async def scenario():
        root = create(repository)
        other = create(repository, "second", root=root.investigation_id)
        close(repository, root)
        close(repository, other)
        service = SynthesisService(repository)
        request = SynthesisRequest(task_ids=[root.investigation_id, other.investigation_id])
        first = await service.synthesize(root.investigation_id, request, "studio-a", "game-a")
        second = await service.synthesize(root.investigation_id, request, "studio-a", "game-a")
        assert first == second
        events = repository.list_recent_events_by_type(
            root.investigation_id, "conversation.synthesis_completed", limit=10
        )
        assert len(events) == 1
        metrics = repository.operational_metrics("studio-a", "game-a")
        assert metrics["coordination_events"]["conversation.synthesis_completed"] == 1
        assert repository.operational_metrics("other", "other")["coordination_events"] == {}

    asyncio.run(scenario())


def test_model_eval_requires_real_citations_and_pinned_semantic_labels():
    from game_test_agent.evals.harness_live import compare_results, apply_adjudications

    rows = [
        {
            "case_id": "one",
            "variant": "compact",
            "model": "model",
            "fixture_sha256": "fixture",
            "report_sha256": "report",
            "tokens": 10,
            "seconds": 1,
            "passed": True,
        }
    ]
    assert not compare_results(rows, {"rows": [{**rows[0], "model": "different"}]})["matched_rows"]
    with pytest.raises(ValueError):
        apply_adjudications(
            rows, {"one/compact": {"report_sha256": "changed", "fixture_sha256": "fixture"}}
        )
    apply_adjudications(rows, {})
    assert rows[0]["semantic_adjudication"] is None
    apply_adjudications(
        rows,
        {
            "one/compact": {
                "report_sha256": "report",
                "fixture_sha256": "fixture",
                "reviewer": "gold-reviewer",
                "question_coverage": 0.5,
                "unsupported_causal_claims": 1,
            }
        },
    )
    assert rows[0]["semantic_adjudication"]["question_coverage"] == 0.5


def test_last_model_turn_is_reserved_for_report_not_plan_edits(repository, settings):
    from test_native_reliability import setup, response, call
    from native_plan_helpers import initial_plan_call

    async def scenario():
        view, controller, _ = setup(repository, settings, rounds=6)
        turns = [0]

        async def model(messages, tools):
            turns[0] += 1
            if turns[0] == 1:
                report_tool = next(
                    item for item in tools if item["function"]["name"] == "agent_submit_report"
                )
                assert "single_run" not in json.dumps(report_tool["function"]["parameters"])
                assert "cross_run" not in json.dumps(report_tool["function"]["parameters"])
                return response(initial_plan_call())
            if turns[0] == 2:
                return response(call())
            if turns[0] < 6:
                return response(initial_plan_call())
            assert {item["function"]["name"] for item in tools} == {"agent_submit_report"}
            evidence = repository.list_current_evidence(view.investigation_id)[0].evidence_id
            return response(
                content=json.dumps(
                    {
                        "summary": "Measured 47 ms; cause remains unknown.",
                        "citations": [{"evidence_id": evidence, "quote": "47 ms"}],
                    }
                )
            )

        controller._model_call = model
        await controller.run(
            view.investigation_id, principal_id="tester", scopes={"runs:read", "evidence:read"}
        )
        assert repository.get_latest_report(view.investigation_id)[0].claims
        assert turns[0] == 6
        await controller.close()

    asyncio.run(scenario())


def test_ingested_result_recovers_after_lost_collection_ack(repository, settings, monkeypatch):
    async def scenario():
        view, provider, tool, controller, memory = controller_setup(repository, settings)
        await controller.native_operations.execute(view, "first", provider, tool, {})
        original = controller.native_operations._update

        def interrupted(row, state, **kwargs):
            if state == "collected":
                raise RuntimeError("lost collection ack")
            return original(row, state, **kwargs)

        monkeypatch.setattr(controller.native_operations, "_update", interrupted)
        with pytest.raises(RuntimeError, match="lost collection ack"):
            await controller.native_operations.resume(
                view, {"scan": (provider, tool)}, memory, time.time() + 30
            )
        monkeypatch.setattr(controller.native_operations, "_update", original)
        await controller.native_operations.resume(
            view, {"scan": (provider, tool)}, memory, time.time() + 30
        )
        assert not controller.native_operations.pending(view.investigation_id)
        assert len(repository.list_current_evidence(view.investigation_id)) == 1
        provider.call_native.assert_awaited_once()
        provider.call_operation_control.assert_awaited_once()

    asyncio.run(scenario())
