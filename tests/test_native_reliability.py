import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import httpx
from mcp import types
from native_plan_helpers import initial_plan, initial_plan_call

from game_test_agent.contracts.models import InputCommand, InvestigationRequest
from game_test_agent.reports import ReportService
from game_test_agent.storage import LocalArtifactStore
from game_test_agent.workflows.native_context import NativeMemory, fit_native_context
from game_test_agent.workflows.native_mcp import NativeMcpController


TEXT = "Measured query round-trip: 47 ms; independent recovery has not been verified."


def call(name="inspect_incident", arguments=None, ident="read-1"):
    args = arguments if isinstance(arguments, str) else json.dumps(arguments or {"id": "pressure"})
    item = SimpleNamespace(id=ident, function=SimpleNamespace(name=name, arguments=args))
    item.model_dump = lambda **_: {
        "id": ident,
        "type": "function",
        "function": {"name": name, "arguments": args},
    }
    return item


def response(*calls, content=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=list(calls), content=content))],
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
    )


def final(messages, summary="Query response exceeded the declared threshold.", **extra):
    evidence = next(
        json.loads(m["content"])["evidence_id"]
        for m in reversed(messages)
        if m["role"] == "tool" and "evidence_id" in json.loads(m["content"])
    )
    return response(
        content=json.dumps(
            {
                "summary": summary,
                "citations": [{"evidence_id": evidence, "quote": "47 ms"}],
                **extra,
            }
        )
    )


def setup(repository, settings, *, rounds=8, constraints=None):
    view, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective={
                "objective_id": "diagnose",
                "description": "Investigate a failure using original MCP evidence.",
            },
            budget={"max_rounds": rounds},
            constraints=constraints or {},
        ),
        "reliability-case",
    )
    tool = types.Tool(
        name="inspect_incident",
        input_schema={
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    )
    provider = SimpleNamespace(
        provider_id="lab",
        config=SimpleNamespace(project_ids=["game-a"], max_response_bytes=20000),
        native_tools=AsyncMock(return_value=[tool]),
        call_native=AsyncMock(
            return_value=types.CallToolResult(content=[types.TextContent(type="text", text=TEXT)])
        ),
    )
    controller = NativeMcpController(
        replace(settings, enable_model=True),
        repository,
        SimpleNamespace(providers={"lab": provider}),
        LocalArtifactStore(settings.artifact_root),
        ReportService(repository),
    )
    return view, controller, provider


async def run(controller, view):
    return await controller.run(
        view.investigation_id, principal_id="tester", scopes={"runs:read", "evidence:read"}
    )


@pytest.mark.parametrize("error_kind", ["malformed_json", "schema_error", "timeout"])
def test_bad_tool_calls_return_repair_feedback_without_losing_the_investigation(
    repository, settings, error_kind
):
    async def scenario():
        view, controller, provider = setup(repository, settings, rounds=3)
        if error_kind != "malformed_json":
            provider.call_native.side_effect = [
                ValueError("must-not-leak-credential")
                if error_kind == "schema_error"
                else TimeoutError(),
                provider.call_native.return_value,
            ]
        turns = 0

        async def model(messages, tools):
            nonlocal turns
            turns += 1
            if turns == 1:
                return response(
                    initial_plan_call(),
                    call(arguments="{bad" if error_kind == "malformed_json" else {"bad": True}),
                )
            if turns == 2:
                feedback = messages[-1]["content"]
                assert "error_code" in feedback and "must-not-leak-credential" not in feedback
                return response(call(ident="corrected"))
            return final(messages)

        controller._model_call = model
        report = await run(controller, view)
        assert report.stop_reason == "native_mcp_investigation_complete"
        assert report.claims[0].verification_status == "verified"
        assert repository.list_recent_events_by_type(view.investigation_id, "mcp.native.tool_error")

    asyncio.run(scenario())


@pytest.mark.parametrize("can_repair", [False, True])
def test_fabricated_citations_are_rejected_and_never_published(repository, settings, can_repair):
    async def scenario():
        view, controller, _ = setup(repository, settings, rounds=3 if can_repair else 2)
        turns = 0

        async def model(messages, tools):
            nonlocal turns
            turns += 1
            if turns == 1:
                return response(initial_plan_call(), call())
            if turns == 2:
                bad = final(messages, summary="UNSUPPORTED CLAIM: fixed the game")
                body = json.loads(bad.choices[0].message.content)
                body["citations"][0]["quote"] = "fabricated original quote"
                return response(content=json.dumps(body))
            assert any("report_validation_errors" in (m.get("content") or "") for m in messages)
            return final(messages)

        controller._model_call = model
        report = await run(controller, view)
        assert "UNSUPPORTED CLAIM" not in report.summary
        assert report.stop_reason == (
            "native_mcp_investigation_complete" if can_repair else "native_report_validation_failed"
        )
        assert repository.list_recent_events_by_type(
            view.investigation_id, "mcp.native.report_rejected"
        )

    asyncio.run(scenario())


def test_user_correction_supersedes_an_inflight_final_answer(repository, settings):
    async def scenario():
        view, controller, _ = setup(repository, settings, rounds=3)
        turns = 0

        async def model(messages, tools):
            nonlocal turns
            turns += 1
            if turns == 1:
                return response(initial_plan_call(), call())
            if turns == 2:
                repository.enqueue_command(
                    view.investigation_id,
                    "correction",
                    "input",
                    InputCommand(
                        command_id="correction",
                        expected_revision=repository.get_investigation(
                            view.investigation_id
                        ).revision,
                        content="Latency during injection is expected; investigate recovery instead.",
                    ),
                )
                controller.notify_event(view.investigation_id)
                return final(messages, summary="STALE ANSWER: game defect confirmed")
            assert "investigate recovery instead" in messages[2]["content"]
            return final(messages, summary="Recovery still needs independent verification.")

        controller._model_call = model
        report = await run(controller, view)
        assert "STALE ANSWER" not in report.summary
        assert "Recovery still needs" in report.summary
        assert (
            repository.list_recent_events_by_type(
                view.investigation_id, "mcp.native.input_applied"
            )[0]["data"]["command_id"]
            == "correction"
        )
        assert repository.list_recent_events_by_type(
            view.investigation_id, "mcp.native.response_superseded"
        )

    asyncio.run(scenario())


def test_restart_restores_tool_history_and_reuses_completed_reads(repository, settings):
    async def scenario():
        view, controller, provider = setup(repository, settings, rounds=5)
        turns = 0

        async def interrupted(messages, tools):
            nonlocal turns
            turns += 1
            if turns == 1:
                return response(initial_plan_call(), call())
            raise asyncio.CancelledError()

        controller._model_call = interrupted
        with pytest.raises(asyncio.CancelledError):
            await run(controller, view)
        # A process can stop after committing a plan event but before the next checkpoint.
        latest_plan = repository.list_recent_events_by_type(
            view.investigation_id, "mcp.native.plan_updated", limit=1
        )[0]["data"]
        latest_plan["revision"] = 2
        latest_plan["reason"] = "Refocus on independently observed recovery."
        repository.record_investigation_event(
            view.investigation_id, "mcp.native.plan_updated", latest_plan
        )
        restored = NativeMcpController(
            controller.settings,
            repository,
            controller.providers,
            controller.artifacts,
            controller.reports,
        )
        resumed_turns = 0
        restored.code_execution = SimpleNamespace(
            description=lambda _: {"enabled": True},
            resume_investigation=AsyncMock(return_value=[{
                "job_id": "recovered-approved-job", "execution_status": "succeeded", "exit_code": 0,
                "failure_kind": None, "stderr": "", "outputs": [], "output_evidence_ids": [],
            }]),
        )

        async def after_restart(messages, tools):
            nonlocal resumed_turns
            resumed_turns += 1
            assert json.loads(messages[2]["content"])["investigation_plan"]["revision"] == 2
            notes = json.loads(messages[2]["content"])["collected_evidence_search_hits"]
            assert any(n.get("job_id") == "recovered-approved-job" and n.get("execution_status") == "succeeded" for n in notes)
            assert any(m["role"] == "tool" and "47 ms" in m["content"] for m in messages)
            return (
                response(call(ident="repeated-after-restart"))
                if resumed_turns == 1
                else final(messages)
            )

        restored._model_call = after_restart
        report = await run(restored, view)
        assert report.stop_reason == "native_mcp_investigation_complete"
        provider.call_native.assert_awaited_once()
        assert repository.list_recent_events_by_type(view.investigation_id, "mcp.native.resumed")

    asyncio.run(scenario())


def test_duplicate_evidence_searches_converge_without_spending_all_rounds(repository, settings):
    async def scenario():
        view, controller, provider = setup(repository, settings, rounds=10)
        turns = 0

        async def model(messages, tools):
            nonlocal turns
            turns += 1
            if turns == 1:
                return response(initial_plan_call(), call())
            if any(t["function"]["name"] == "agent_read_collected_evidence" for t in tools):
                eid = next(
                    e.evidence_id for e in repository.list_current_evidence(view.investigation_id)
                )
                return response(
                    call(
                        "agent_read_collected_evidence",
                        {"evidence_id": eid, "query": "47 ms"},
                        f"search-{turns}",
                    )
                )
            return final(messages)

        controller._model_call = model
        report = await run(controller, view)
        assert report.stop_reason == "native_mcp_investigation_complete"
        # One bounded replanning opportunity precedes final reporting.
        assert turns == 6
        assert repository.budget_usage(view.investigation_id)["tool_reads"] == 2
        provider.call_native.assert_awaited_once()

    asyncio.run(scenario())


def test_interrupted_report_publication_recovers_the_validated_draft(repository, settings):
    async def scenario():
        view, controller, provider = setup(repository, settings, rounds=3)
        turns = 0

        async def model(messages, tools):
            nonlocal turns
            turns += 1
            return response(initial_plan_call(), call()) if turns == 1 else final(messages)

        controller._model_call = model
        controller.reports = SimpleNamespace(
            publish=lambda report: (_ for _ in ()).throw(asyncio.CancelledError())
        )
        with pytest.raises(asyncio.CancelledError):
            await run(controller, view)
        assert repository.get_investigation(view.investigation_id).status == "reporting"
        resumed = NativeMcpController(
            controller.settings,
            repository,
            controller.providers,
            controller.artifacts,
            ReportService(repository),
        )
        resumed._model_call = AsyncMock(
            side_effect=AssertionError("must reuse the validated draft")
        )
        report = await run(resumed, view)
        assert report.stop_reason == "native_mcp_investigation_complete"
        assert report.claims[0].verification_status == "verified"
        provider.call_native.assert_awaited_once()

    asyncio.run(scenario())


def test_checkpoint_cannot_be_attached_to_another_investigation(repository, settings):
    view, controller, _ = setup(repository, settings)
    memory = NativeMemory(messages=[{"role": "user", "content": "private context"}])
    memory.save(repository, controller.artifacts, view)
    checkpoint = repository.list_recent_events_by_type(
        view.investigation_id, "mcp.native.checkpoint"
    )[0]["data"]
    other, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective={"objective_id": "other", "description": "Other investigation"},
        ),
        "other",
    )
    repository.record_investigation_event(
        other.investigation_id, "mcp.native.checkpoint", checkpoint
    )
    with pytest.raises(PermissionError):
        NativeMemory.restore(repository, controller.artifacts, other)


def test_long_search_query_does_not_cause_an_endless_compaction_loop():
    query = "q" * 1000
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "request"},
        {"role": "user", "content": "notes"},
        {"role": "assistant", "tool_calls": [{"id": "c"}]},
        {
            "role": "tool",
            "tool_call_id": "c",
            "content": json.dumps(
                {"result": "x" * 2000 + query + "y" * 3000, "query": query, "matched": True}
            ),
        },
    ]
    assert fit_native_context(messages, [], 3000)
    assert len(json.dumps({"messages": messages, "tools": []}).encode()) <= 3000
    for message in messages:
        if message["role"] == "tool":
            assert query in json.loads(message["content"])["result"]


@pytest.mark.parametrize("permanent", [False, True])
def test_model_timeouts_have_bounded_recovery(repository, settings, permanent):
    async def scenario():
        view, controller, _ = setup(repository, settings, rounds=4)
        turns = 0

        async def model(messages, tools):
            nonlocal turns
            turns += 1
            if turns == 1 or permanent:
                raise TimeoutError("upstream-secret-must-not-leak")
            return response(initial_plan_call(), call()) if turns == 2 else final(messages)

        controller._model_call = model
        report = await run(controller, view)
        assert report.stop_reason == (
            "native_model_unavailable" if permanent else "native_mcp_investigation_complete"
        )
        assert turns == (2 if permanent else 3)
        assert "upstream-secret" not in report.summary
        assert repository.list_recent_events_by_type(
            view.investigation_id, "mcp.native.model_error"
        )

    asyncio.run(scenario())


@pytest.mark.parametrize("declared", [False, True])
def test_experiment_interpretation_requires_declared_expectations(repository, settings, declared):
    async def scenario():
        constraints = (
            {
                "experiment_expectations": {
                    "expected_effects": [
                        "Transient response latency may increase during injection."
                    ],
                    "violation_conditions": ["The fault remains active after the recovery window."],
                }
            }
            if declared
            else {}
        )
        view, controller, _ = setup(repository, settings, constraints=constraints)
        turns = 0

        async def model(messages, tools):
            nonlocal turns
            turns += 1
            return (
                response(initial_plan_call(), call())
                if turns == 1
                else final(
                    messages,
                    interpretation="expected_effect",
                    interpretation_basis="The observed change matches the declared injection effect.",
                    hypotheses=["Other concurrent workload has not been excluded."],
                )
            )

        controller._model_call = model
        report = await run(controller, view)
        assessment = repository.list_recent_events_by_type(
            view.investigation_id, "mcp.native.assessment"
        )[0]["data"]
        assert assessment["interpretation"] == "unknown"
        assert assessment["model_interpretation"] == "expected_effect"
        if declared:
            assert any("尚未绑定可执行验收规则" in item for item in report.unresolved_questions)
        assert report.conclusion_level == "observation"
        assert any("待验证假设" in q for q in report.unresolved_questions)
        assert any("不代表原因推断" in q for q in report.unresolved_questions)

    asyncio.run(scenario())


def test_workspace_usage_includes_calls_outside_the_latest_event_page(settings):
    from game_test_agent.api import create_app

    async def scenario():
        app = create_app(replace(settings, mcp_native_tools=True))
        async with app.router.lifespan_context(app):
            repository = app.state.container.repository
            view, _ = repository.create_investigation(
                InvestigationRequest(
                    tenant_id="studio-a",
                    project_id="game-a",
                    objective={
                        "objective_id": "usage",
                        "description": "Review cumulative model usage",
                    },
                ),
                "usage",
            )
            for i in range(125):
                if i < 2:
                    repository.record_investigation_event(
                        view.investigation_id,
                        "mcp.native.plan_updated",
                        {
                            "revision": i + 1,
                            "reason": "Evidence changed the next step.",
                            "steps": initial_plan()["steps"],
                        },
                    )
                repository.record_investigation_event(
                    view.investigation_id,
                    "mcp.native.model_completed",
                    {
                        "turn": i + 1,
                        "input_tokens": 10,
                        "output_tokens": 2,
                        "duration_ms": 3,
                    },
                )
            repository.record_investigation_event(
                view.investigation_id, "mcp.native.tool_reused", {}
            )
            headers = {
                "X-Tenant-ID": "studio-a",
                "X-Project-ID": "game-a",
                "X-Principal-ID": "tester",
            }
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                result = await client.get(
                    f"/v1/investigations/{view.investigation_id}/workspace", headers=headers
                )
            assert result.status_code == 200
            body = result.json()
            assert len(body["events"]) == 100
            assert body["native_runtime"]["model_responses"] == 125
            assert body["native_runtime"]["input_tokens"] == 1250
            assert body["native_runtime"]["cache_hits"] == 1
            assert body["native_plan"]["revision"] == 2
            assert [e["data"]["revision"] for e in body["native_plan_history"]] == [1, 2]
            assert not any(e["event_type"] == "mcp.native.plan_updated" for e in body["events"])

    asyncio.run(scenario())


def test_native_read_requires_a_valid_initial_plan_but_can_share_its_model_turn(
    repository, settings
):
    async def scenario():
        view, controller, provider = setup(repository, settings, rounds=4)
        turns = 0

        async def model(messages, tools):
            nonlocal turns
            turns += 1
            if turns == 1:
                return response(call())
            if turns == 2:
                assert json.loads(messages[-1]["content"])["error_code"] == "PLAN_REQUIRED"
                provider.call_native.assert_not_awaited()
                assert not repository.budget_usage(view.investigation_id).get("tool_reads")
                return response(initial_plan_call(), call(ident="after-plan"))
            return final(messages)

        controller._model_call = model
        report = await run(controller, view)
        assert report.stop_reason == "native_mcp_investigation_complete"
        provider.call_native.assert_awaited_once()
        assert repository.budget_usage(view.investigation_id)["tool_reads"] == 1
        assert (
            len(
                repository.list_recent_events_by_type(
                    view.investigation_id, "mcp.native.plan_updated"
                )
            )
            == 1
        )

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["empty", "precompleted"])
def test_invalid_initial_plan_never_unlocks_platform_reads(repository, settings, kind):
    async def scenario():
        view, controller, provider = setup(repository, settings, rounds=3)
        turns = 0

        async def model(messages, tools):
            nonlocal turns
            turns += 1
            if turns == 1:
                plan = initial_plan()
                if kind == "empty":
                    plan["steps"] = []
                else:
                    plan["steps"][0].update(
                        status="completed", evidence_ids=["fabricated"], outcome="Already verified"
                    )
                return response(call("agent_update_plan", plan, "bad-initial"), call())
            if turns == 2:
                provider.call_native.assert_not_awaited()
                assert json.loads(messages[2]["content"])["investigation_plan"] is None
                return response(initial_plan_call(), call(ident="after-valid-plan"))
            return final(messages)

        controller._model_call = model
        report = await run(controller, view)
        assert report.stop_reason == "native_mcp_investigation_complete"
        provider.call_native.assert_awaited_once()

    asyncio.run(scenario())


def test_reporting_phase_cannot_execute_an_additional_mcp_read(repository, settings):
    async def scenario():
        view, controller, provider = setup(repository, settings, rounds=2)
        turns = 0

        async def model(messages, tools):
            nonlocal turns
            turns += 1
            if turns == 1:
                return response(initial_plan_call(), call())
            assert {t["function"]["name"] for t in tools} in ({"agent_submit_report"}, {"agent_update_plan", "agent_submit_report"})
            return response(call(ident="forbidden-final-read"))

        controller._model_call = model
        report = await run(controller, view)
        assert report.stop_reason == "native_report_validation_failed"
        provider.call_native.assert_awaited_once()
        assert repository.budget_usage(view.investigation_id)["tool_reads"] == 1

    asyncio.run(scenario())


def test_plan_revision_links_evidence_explains_gaps_and_survives_checkpoint(repository, settings):
    async def scenario():
        view, controller, provider = setup(repository, settings, rounds=6)
        turns = 0

        async def model(messages, tools):
            nonlocal turns
            turns += 1
            if turns == 1:
                return response(initial_plan_call(), call())
            if turns < 4:
                return response(call(arguments={"id": f"snapshot-{turns}"}, ident=f"read-{turns}"))
            assert {t["function"]["name"] for t in tools} in ({"agent_submit_report"}, {"agent_update_plan", "agent_submit_report"})
            plan = initial_plan()
            plan["expected_revision"] = 1
            plan["reason"] = (
                "Observed the failed query; baseline and independent recovery remain unavailable."
            )
            eid = repository.list_current_evidence(view.investigation_id)[0].evidence_id
            plan["steps"][0].update(
                status="completed", evidence_ids=[eid], outcome="Query round-trip was 47 ms."
            )
            for step in plan["steps"][1:]:
                step.update(
                    status="blocked",
                    outcome="The available evidence does not independently verify this question.",
                )
            # Revisions need only changed fields; the stored questions/conditions survive.
            plan["steps"] = [
                {
                    key: value
                    for key, value in step.items()
                    if key in {"step_id", "status", "evidence_ids", "outcome"}
                }
                for step in plan["steps"]
            ]
            schema = next(
                t["function"]["parameters"]
                for t in tools
                if t["function"]["name"] == "agent_update_plan"
            )
            assert schema["$defs"]["InvestigationStepPatch"]["required"] == ["step_id"]
            report = json.loads(final(messages).choices[0].message.content)
            return response(
                call("agent_update_plan", plan, "revise"),
                call("agent_submit_report", report, "report"),
            )

        controller._model_call = model
        report = await run(controller, view)
        assert report.stop_reason == "native_mcp_investigation_complete"
        assert turns == 4
        memory = NativeMemory.restore(repository, controller.artifacts, view)
        assert memory.plan["revision"] == 2
        assert memory.plan["steps"][0]["question"] == initial_plan()["steps"][0]["question"]
        assert [step["status"] for step in memory.plan["steps"]] == [
            "completed",
            "blocked",
            "blocked",
        ]
        assert "47 ms" in memory.messages[2]["content"]
        assert provider.call_native.await_count == 3
        assert repository.budget_usage(view.investigation_id)["tool_reads"] == 3

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "kind",
    [
        "stale",
        "duplicate",
        "fake_evidence",
        "unexplained_block",
        "two_active",
        "unchanged",
        "null_field",
    ],
)
def test_plan_invalid_updates_leave_the_previous_revision_intact(repository, settings, kind):
    async def scenario():
        view, controller, _ = setup(repository, settings, rounds=4)
        turns = 0

        async def model(messages, tools):
            nonlocal turns
            turns += 1
            if turns == 1:
                return response(initial_plan_call(), call())
            if turns == 2:
                plan = initial_plan()
                plan["expected_revision"] = 1
                if kind == "stale":
                    plan["expected_revision"] = 7
                elif kind == "duplicate":
                    plan["steps"][1]["step_id"] = "source"
                elif kind == "fake_evidence":
                    plan["steps"][0].update(
                        status="completed",
                        evidence_ids=["other-investigation-evidence"],
                        outcome="Unsupported",
                    )
                elif kind == "unexplained_block":
                    plan["steps"][0]["status"] = "blocked"
                elif kind == "two_active":
                    plan["steps"][1]["status"] = "in_progress"
                elif kind == "null_field":
                    plan["steps"] = [{"step_id": "source", "question": None}]
                return response(call("agent_update_plan", plan, "invalid-plan"))
            assert (
                json.loads(next(m["content"] for m in reversed(messages) if m["role"] == "tool"))[
                    "error_code"
                ]
                == "INVALID_PLAN"
            )
            if kind == "null_field":
                assert "steps.0.question" in str(messages)
            assert json.loads(messages[2]["content"])["investigation_plan"]["revision"] == 1
            return final(messages)

        controller._model_call = model
        report = await run(controller, view)
        assert report.stop_reason == "native_mcp_investigation_complete"
        assert repository.list_recent_events_by_type(
            view.investigation_id, "mcp.native.plan_rejected"
        )
        assert NativeMemory.restore(repository, controller.artifacts, view).plan["revision"] == 1

    asyncio.run(scenario())


def test_explicit_report_tool_finishes_as_soon_as_cited_evidence_is_sufficient(
    repository, settings
):
    async def scenario():
        view, controller, provider = setup(repository, settings, rounds=12)
        turns = 0

        async def model(messages, tools):
            nonlocal turns
            turns += 1
            assert any(t["function"]["name"] == "agent_submit_report" for t in tools)
            if turns == 1:
                return response(initial_plan_call(), call())
            report = json.loads(final(messages).choices[0].message.content)
            return response(call("agent_submit_report", report, "submit"))

        controller._model_call = model
        report = await run(controller, view)
        assert report.stop_reason == "native_mcp_investigation_complete"
        assert turns == 2
        assert report.claims[0].verification_status == "verified"
        provider.call_native.assert_awaited_once()

    asyncio.run(scenario())


def test_report_rounds_are_reserved_for_fixing_invalid_citations(repository, settings):
    async def scenario():
        view, controller, provider = setup(repository, settings, rounds=6)

        async def snapshots(tool, args):
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=args["id"] + TEXT)]
            )

        provider.call_native.side_effect = snapshots
        turns = 0

        async def model(messages, tools):
            nonlocal turns
            turns += 1
            if turns <= 3:
                assert tools
                return response(
                    *([initial_plan_call()] if turns == 1 else []),
                    call(arguments={"id": f"snapshot-{turns}"}, ident=f"call-{turns}"),
                )
            assert {t["function"]["name"] for t in tools} in ({"agent_submit_report"}, {"agent_update_plan", "agent_submit_report"})
            if turns == 4:
                body = json.loads(final(messages).choices[0].message.content)
                body["citations"][0]["quote"] = "Measured ... 47 ms"
                return response(content=json.dumps(body))
            return final(messages)

        controller._model_call = model
        report = await run(controller, view)
        assert turns == 5
        assert report.stop_reason == "native_mcp_investigation_complete"
        assert provider.call_native.await_count == 3
        assert report.claims[0].verification_status == "verified"

    asyncio.run(scenario())
