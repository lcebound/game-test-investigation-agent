"""Question coverage, partial review and finite reports over existing ledgers."""
import asyncio
import copy
import hashlib
import json
from dataclasses import replace

import pytest

from game_test_agent.contracts.models import InvestigationRequest, InvestigationStatus
from game_test_agent.workflows.native_context import NativeMemory, report_context
from game_test_agent.workflows.native_mcp import CollectedEvidenceRead, NativeDiagnosis
from game_test_agent.workflows.native_plan import InvestigationPlanUpdate, update_native_plan
from game_test_agent.workflows.native_questions import (
    input_fingerprint, question_coverage, report_question_progress,
)
from game_test_agent.workflows.native_reasoning import select_candidate
from native_plan_helpers import initial_plan
from test_native_enhancements import collect
from test_native_recall import add_input, memory
from test_native_reliability import call, response, run, setup


def plan(count=1):
    value = initial_plan()
    value["steps"] = value["steps"][:count]
    return value


def revise(repository, view, state, steps):
    return update_native_plan(repository, view, state, json.dumps({
        "expected_revision": state.plan["revision"], "reason": "Review question evidence and remaining gaps.",
        "steps": steps}))


@pytest.mark.parametrize("count", [1, 2, 5])
def test_small_plans_are_valid_without_padding(count):
    body = plan()
    body["steps"] = [{**body["steps"][0], "step_id": f"q{i}", "status": "pending"} for i in range(count)]
    assert len(InvestigationPlanUpdate.model_validate(body).steps) == count


def test_single_question_finishes_in_small_window_with_conservative_acceptance(repository, settings):
    async def scenario():
        view, controller, provider = setup(repository, settings, rounds=4)
        controller.settings = replace(controller.settings, model_context_window_tokens=8192,
                                      model_max_output_tokens=512)
        turns = 0
        async def model(messages, tools):
            nonlocal turns
            turns += 1
            if turns == 1:
                schema = next(t["function"]["parameters"] for t in tools
                              if t["function"]["name"] == "agent_update_plan")
                assert schema["properties"]["steps"]["minItems"] == 1
                return response(call("agent_update_plan", plan(), "plan"), call())
            if turns == 2:
                eid = repository.list_current_evidence(view.investigation_id)[0].evidence_id
                return response(call("agent_update_plan", {"expected_revision": 1, "reason": "Observed original measurement.",
                    "steps": [{"step_id": "source", "status": "completed", "evidence_ids": [eid],
                               "outcome": "Observed query round-trip 47 ms."}]}, "answered"))
            working = json.loads(messages[2]["content"])
            assert working["question_coverage"]["questions"] == [{"question_id": "source", "state": "answered"}]
            note = next(n for n in working["collected_evidence_search_hits"] if "47 ms" in n["original_excerpt"])
            return response(content=json.dumps({"summary": "Recorded observation.",
                "citations": [{"evidence_id": note["evidence_id"], "quote": "47 ms"}]}))
        controller._model_call = model
        report = await run(controller, view)
        assert report.status == "completed" and turns == 3
        assert "[source]" in report.summary and "答复草稿" in report.summary
        assert report.conclusion_level == "observation" and report.resolution == "inconclusive"
        assert report.coverage.uncovered_objectives == [view.request.objective.objective_id]
        provider.call_native.assert_awaited_once()
    asyncio.run(scenario())


@pytest.mark.parametrize("bad", [
    {"counterevidence_ids": ["foreign"]}, {"source_input_ids": ["foreign"]},
    {"remaining_gaps": [""]}, {"remaining_gaps": ["x" * 301]},
    {"counterevidence_ids": ["x" * 257]},
])
def test_bad_question_links_are_rejected_atomically(repository, settings, bad):
    view, _, _ = setup(repository, settings)
    state = memory()
    update_native_plan(repository, view, state, json.dumps(plan()))
    before = copy.deepcopy(state.plan)
    with pytest.raises(ValueError):
        revise(repository, view, state, [{"step_id": "source", **bad}])
    assert state.plan == before
    assert len(repository.list_recent_events_by_type(view.investigation_id, "mcp.native.plan_updated")) == 1


def test_new_inputs_invalidate_answers_and_only_explicitly_reviewed_steps_recover(repository, settings):
    async def scenario():
        view, controller, _, state, ids = await collect(repository, settings, [{"v": 7}])
        update_native_plan(repository, view, state, json.dumps(plan(2)))
        revise(repository, view, state, [{"step_id": sid, "status": "completed", "evidence_ids": ids,
                                         "outcome": "Observed saved evidence."} for sid in ("source", "intent")])
        assert all(row["state"] == "answered" for row in question_coverage(state.plan, state.inputs, ids)["questions"])
        add_input(repository, view, "correction", "Evaluate another baseline before interpreting this result.")
        state.sync_inputs(repository, view.investigation_id)
        stale = question_coverage(state.plan, state.inputs, ids)
        assert all(row["state"] == "partial" and row["needs_review"] for row in stale["questions"])
        # Even a review that retains its draft must acknowledge the new input.
        revise(repository, view, state, [{"step_id": "source", "source_input_ids": ["correction"]}])
        current = question_coverage(state.plan, state.inputs, ids)
        assert current["questions"][0] == {"question_id": "source", "state": "answered"}
        assert current["questions"][1]["needs_review"] and current["next_question_id"] == "intent"
        state.save(repository, controller.artifacts, view)
        restored = NativeMemory.restore(repository, controller.artifacts, view)
        assert question_coverage(restored.plan, restored.inputs, ids) == current
        with pytest.raises(ValueError, match="unchanged"):
            revise(repository, view, restored, [{"step_id": "source"}])
    asyncio.run(scenario())


def test_legacy_plan_and_changed_input_content_are_not_silently_certified():
    legacy = {"revision": 1, "steps": [{**plan()["steps"][0], "status": "completed",
              "outcome": "Recorded an observation.", "evidence_ids": ["e"]}]}
    assert question_coverage(legacy, [], {"e"})["questions"][0]["state"] == "answered"
    rows = [{"command_id": "input", "payload": {"content": "original"}}]
    cards = [{"command_id": "input", "sha256": hashlib.sha256(b"original").hexdigest()}]
    assert input_fingerprint(rows) == input_fingerprint(cards)
    assert question_coverage(legacy, rows, {"e"})["questions"][0]["needs_review"]
    legacy["reviewed_inputs"] = {"source": input_fingerprint(rows)}
    assert question_coverage(legacy, rows, {"e"})["questions"][0]["state"] == "answered"
    rows[0]["payload"]["content"] = "changed"
    assert question_coverage(legacy, rows, {"e"})["questions"][0]["needs_review"]


def test_completed_status_with_gaps_or_unavailable_evidence_remains_partial():
    base = {"revision": 1, "steps": [{**plan()["steps"][0], "status": "completed",
            "evidence_ids": ["e"], "outcome": "Observation only.", "remaining_gaps": ["Recovery missing"]}]}
    assert question_coverage(base, [], {"e"})["questions"][0]["state"] == "partial"
    base["steps"][0].pop("remaining_gaps")
    unavailable = question_coverage(base, [], set())["questions"][0]
    assert unavailable["state"] == "partial" and unavailable["unavailable_evidence_ids"] == ["e"]


def test_question_counterevidence_survives_catalog_and_excerpt_compaction():
    state = memory()
    state.plan = {"revision": 1, "steps": [{**plan()["steps"][0], "counterevidence_ids": ["e0"],
                  "remaining_gaps": ["Explain contradictory result"]}]}
    state.evidence_index = {f"e{i}": {"evidence_id": f"e{i}"} for i in range(45)}
    for i in range(16):
        state.notes[f"n{i}"] = {"evidence_id": f"e{i}", "original_excerpt": "x" * 600}
    state.refresh_working_message()
    working = json.loads(state.messages[2]["content"])
    counter = next(entry for entry in working["evidence_catalog"] if entry["evidence_id"] == "e0")
    assert counter["counter_question_ids"] == ["source"] and counter["read_ref"] == "evidence/e0"
    assert any(note["evidence_id"] == "e0" for note in working["collected_evidence_search_hits"])
    reporting = json.loads(report_context(state)[2]["content"])
    assert any(entry.get("counter_question_ids") == ["source"] for entry in reporting["evidence_catalog"])


def test_answer_rewording_cannot_reset_stagnation():
    state = memory()
    state.plan = {"revision": 1, "steps": [{**plan()["steps"][0], "status": "completed",
                  "evidence_ids": ["e"], "outcome": "Initial draft"}]}
    assert state.register_progress(False) == "ledger_changed"
    for index in range(3):
        state.plan["steps"][0]["outcome"] = f"Rewritten draft {index}"
        state.register_progress(False)
    assert state.finalize


@pytest.mark.parametrize("field,value", [("question", "A different question?"),
                                         ("completion_condition", "Require independent recovery evidence")])
def test_changed_question_cannot_silently_inherit_completed_answer(repository, settings, field, value):
    async def scenario():
        view, _, _, state, ids = await collect(repository, settings, [{"v": 7}])
        update_native_plan(repository, view, state, json.dumps(plan()))
        revise(repository, view, state, [{"step_id": "source", "status": "completed",
            "evidence_ids": ids, "outcome": "Previous observation."}])
        with pytest.raises(ValueError, match="explicit status"):
            revise(repository, view, state, [{"step_id": "source", field: value}])
        revise(repository, view, state, [{"step_id": "source", field: value, "status": "in_progress",
                                         "remaining_gaps": ["Investigate revised question"]}])
        assert question_coverage(state.plan, [], ids)["questions"][0]["state"] == "partial"
    asyncio.run(scenario())


def test_question_gap_selection_uses_existing_selector_without_reading_platform(repository, settings):
    async def scenario():
        view, controller, provider, state, ids = await collect(repository, settings, [{"cleanup": False}])
        body = plan()
        body["steps"][0]["remaining_gaps"] = ["Independent recovery observation"]
        update_native_plan(repository, view, state, json.dumps(body))
        request = {"candidates": [{"candidate_id": "recovery", "tool_name": "agent_read_context",
            "arguments": {"ref": "evidence/" + ids[0], "pointer": "/cleanup"},
            "question": "What does the saved cleanup receipt establish?",
            "fills_gaps": ["Independent recovery observation"]}]}
        selected, _ = select_candidate(repository, view, state, {}, None, json.dumps(request), CollectedEvidenceRead)
        assert selected["selected_candidate"]["candidate_id"] == "recovery"
        assert selected["assessments"][0]["fills_evidence_gap"]
        state.save(repository, controller.artifacts, view)
        async def model(messages, tools):
            assert "agent_choose_next_action" in {tool["function"]["name"] for tool in tools}
            return response(call("agent_choose_next_action", request, "choose-question-gap"))
        controller._model_call = model
        assert await controller.run(view.investigation_id, principal_id="tester",
            scopes={"runs:read", "evidence:read"}, max_model_turns=1) is None
        provider.call_native.assert_awaited_once()
        assert repository.budget_usage(view.investigation_id)["tool_reads"] == 1
    asyncio.run(scenario())


def test_publication_rebuilds_question_gaps_from_authoritative_events(repository, settings):
    async def scenario():
        view, controller, _, state, ids = await collect(repository, settings, [{"v": 7}, {"v": 8}])
        update_native_plan(repository, view, state, json.dumps(plan(2)))
        revise(repository, view, state, [
            {"step_id": "source", "status": "completed", "evidence_ids": [ids[0]],
             "counterevidence_ids": [ids[1]], "outcome": "Conflicting observations.", "remaining_gaps": ["Explain discrepancy"]},
            {"step_id": "intent", "status": "blocked", "outcome": "Missing independent baseline."},
        ])
        progress = report_question_progress(repository, view)
        assert progress["questions"][0]["state"] == "partial"
        repository.transition(view.investigation_id, InvestigationStatus.DISCOVERING)
        # No NativeMemory is passed: publication and draft recovery still see gaps.
        report = controller._finish(view, NativeDiagnosis(summary="Recorded observation.",
            citations=[{"evidence_id": ids[0], "quote": "7"}]), "question-test")
        assert report.status == "completed" and report.resolution == "inconclusive"
        assert any("Explain discrepancy" in gap for gap in report.unresolved_questions)
        assert any("Missing independent baseline" in gap for gap in report.unresolved_questions)
        assert ids[1] in report.summary
        assert repository.list_recent_events_by_type(view.investigation_id, "mcp.native.question_coverage")
    asyncio.run(scenario())


def test_cross_investigation_input_link_does_not_become_a_source(repository, settings):
    view, _, _ = setup(repository, settings)
    other, _ = repository.create_investigation(InvestigationRequest(tenant_id="studio-a", project_id="game-a",
        objective={"objective_id": "other", "description": "Another task"}), "other-input-source")
    add_input(repository, other, "other-input", "Do not leak this task")
    body = plan()
    body["steps"][0]["source_input_ids"] = ["other-input"]
    with pytest.raises(ValueError, match="source_input_ids"):
        update_native_plan(repository, view, memory(), json.dumps(body))


def test_workspace_exposes_current_question_state_with_existing_scope_checks(settings):
    import httpx
    from game_test_agent.api import create_app

    async def scenario():
        app = create_app(replace(settings, mcp_native_tools=True))
        async with app.router.lifespan_context(app):
            repository = app.state.container.repository
            view, _, _, state, ids = await collect(repository, settings, [{"v": 7}, {"v": 8}])
            update_native_plan(repository, view, state, json.dumps(plan()))
            revise(repository, view, state, [{"step_id": "source", "status": "completed",
                "evidence_ids": [ids[0]], "counterevidence_ids": [ids[1]], "outcome": "Observation draft."}])
            add_input(repository, view, "followup", "Reassess the previous interpretation.")
            headers = {"X-Tenant-ID": "studio-a", "X-Project-ID": "game-a", "X-Principal-ID": "tester"}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                path = f"/v1/investigations/{view.investigation_id}/workspace"
                snapshot = await client.get(path, headers=headers)
                assert snapshot.status_code == 200
                data = snapshot.json()
                row = data["native_runtime"]["question_coverage"]["questions"][0]
                assert row["state"] == "partial" and row["needs_review"]
                assert data["native_plan"]["steps"][0]["counterevidence_ids"] == [ids[1]]
                forbidden = await client.get(path, headers={**headers, "X-Project-ID": "unrelated"})
                assert forbidden.status_code in {403, 404} and ids[1] not in forbidden.text
                revise(repository, view, state, [{"step_id": "source", "source_input_ids": ["followup"]}])
                reviewed = (await client.get(path, headers=headers)).json()
                assert reviewed["native_runtime"]["question_coverage"]["questions"][0]["state"] == "answered"
    asyncio.run(scenario())
