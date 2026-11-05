from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from game_test_agent.agents.investigator import Investigator
from game_test_agent.capabilities import CapabilityRegistry
from game_test_agent.config import Settings
from game_test_agent.contracts.models import (
    CapabilityDescriptor,
    InvestigationRequest,
    NextStep,
    TestObjective as Objective,
)
from game_test_agent.evidence.context import ContextBuilder
from game_test_agent.planning.action_planner import ActionPlanner


def test_json_prompt_mode_validates_plain_model_output(monkeypatch):
    from agents import Runner

    async def run(_agent, **_kwargs):
        return SimpleNamespace(
            final_output=(
                '{"kind":"request_input","objective_id":"test",'
                '"expected_observation":"build identity",'
                '"reason_summary":"build provenance is absent",'
                '"missing_fields":["build.hash"]}'
            )
        )

    monkeypatch.setattr(Runner, "run", run)

    async def scenario() -> None:
        investigator = Investigator(
            "compatible-model",
            enabled=True,
            model_api="chat_completions",
            output_mode="json_prompt",
        )
        decision = await investigator.reason_next_step(
            {"investigation": {"request": {"objective": {"objective_id": "test"}}}}
        )
        assert decision.kind == "request_input"
        assert decision.missing_fields == ["build.hash"]

    asyncio.run(scenario())


def test_json_prompt_mode_rejects_non_json_or_invalid_contract(monkeypatch):
    from agents import Runner

    async def run(_agent, **_kwargs):
        return SimpleNamespace(final_output="```json\n{}\n```")

    monkeypatch.setattr(Runner, "run", run)

    async def scenario() -> None:
        investigator = Investigator("compatible-model", enabled=True, output_mode="json_prompt")
        with pytest.raises(ValueError):
            await investigator.reason_next_step({})

    asyncio.run(scenario())


def test_invalid_model_output_mode_is_rejected():
    settings = Settings(
        database_path=Path("unused.db"),
        artifact_root=Path("unused-artifacts"),
        model_output_mode="unsafe",
    )
    with pytest.raises(ValueError, match="model output mode"):
        settings.validate()


def test_investigator_does_not_silently_strip_action_parameters():
    with pytest.raises(ValidationError, match="parameters"):
        Investigator._parse_prompt_decision(
            '{"kind":"experiment","objective_id":"test",'
            '"capability_id":"test.run","provider_id":"lab",'
            '"expected_observation":"result","reason_summary":"collect",'
            '"parameters":{"case":"smoke"}}'
        )


def test_json_object_mode_requests_provider_json_and_validates_contract(monkeypatch):
    from agents import Runner

    async def run(agent, **_kwargs):
        assert agent.model_settings.extra_body == {
            "enable_thinking": False,
            "response_format": {"type": "json_object"},
        }
        return SimpleNamespace(
            final_output=(
                '{"kind":"report","objective_id":"test",'
                '"expected_observation":"bounded result","reason_summary":"one run suffices"}'
            )
        )

    monkeypatch.setattr(Runner, "run", run)

    async def scenario() -> None:
        investigator = Investigator(
            "compatible-model",
            enabled=True,
            model_api="chat_completions",
            output_mode="json_object",
            extra_body={"enable_thinking": False},
        )
        decision = await investigator.reason_next_step({})
        assert decision.kind == "report"

    asyncio.run(scenario())


def test_json_object_mode_requires_chat_completions_and_object_extra_body():
    settings = Settings(
        database_path=Path("unused.db"),
        artifact_root=Path("unused-artifacts"),
        model_api="responses",
        model_output_mode="json_object",
    )
    with pytest.raises(ValueError, match="chat_completions"):
        settings.validate()
    settings = Settings(
        database_path=Path("unused.db"),
        artifact_root=Path("unused-artifacts"),
        model_api="chat_completions",
        model_output_mode="json_object",
        model_extra_body_json="[]",
    )
    with pytest.raises(ValueError, match="JSON object"):
        settings.validate()


def test_missing_experiment_binding_is_filled_only_when_unique():
    decision = NextStep(
        kind="experiment",
        objective_id="test",
        expected_observation="one run",
        reason_summary="run the registered case",
    )
    ready = {
        "capability_id": "test.run",
        "provider_id": "lab-a",
        "operation_kind": "experiment",
        "creates_run": True,
        "availability": "ready",
    }
    bind = Investigator._bind_unique_experiment_capability
    resolved = bind({"capabilities": [ready]}, decision)
    assert (resolved.capability_id, resolved.provider_id) == ("test.run", "lab-a")
    ambiguous = bind({"capabilities": [ready, {**ready, "provider_id": "lab-b"}]}, decision)
    assert ambiguous.capability_id is None
    assert ambiguous.provider_id is None


def test_report_null_observation_is_only_narrowly_normalized():
    parse = Investigator._parse_prompt_decision
    report = parse(
        '{"kind":"report","objective_id":"test","expected_observation":null,'
        '"reason_summary":"the requested run passed"}'
    )
    assert report.kind == "report"
    assert report.expected_observation == "Report the observed evidence and remaining gaps."
    with pytest.raises(ValidationError):
        parse(
            '{"kind":"experiment","objective_id":"test","expected_observation":null,'
            '"reason_summary":"run again"}'
        )
    with pytest.raises(ValidationError):
        parse(
            '{"kind":"report","objective_id":"test","expected_observation":null,'
            '"reason_summary":null}'
        )


def test_action_planner_json_prompt_mode_still_checks_capability_binding(repository, monkeypatch):
    from agents import Runner

    investigation, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective=Objective(objective_id="smoke", description="Run a registered case"),
        ),
        "json-prompt-planner",
    )
    registry = CapabilityRegistry(repository)
    registry.register(
        CapabilityDescriptor(
            capability_id="test.run",
            capability_version="1.0.0",
            provider_id="platform-local",
            provider_revision="1",
            tool_name="submit",
            project_ids=["game-a"],
            input_schema={"type": "object"},
            risk_level="bounded_execution",
            execution_owner="platform-local",
            resource_domain="platform-local",
        )
    )
    context = ContextBuilder(repository, registry).build(
        investigation.investigation_id, scopes=set()
    )
    decision = NextStep(
        kind="experiment",
        objective_id="smoke",
        capability_id="test.run",
        provider_id="platform-local",
        expected_observation="test verdict",
        reason_summary="run a bounded test",
    )

    async def run(_agent, **_kwargs):
        return SimpleNamespace(
            final_output=(
                '{"capability_id":"test.run","capability_version":"1.0.0",'
                '"provider_id":"platform-local","parameters":{"case":"smoke"},'
                '"expected_observation":"test verdict"}'
            )
        )

    monkeypatch.setattr(Runner, "run", run)

    async def scenario() -> None:
        proposal = await ActionPlanner(
            "compatible-model", enabled=True, output_mode="json_prompt"
        ).propose(context, decision)
        assert proposal.provider_revision == "1"
        assert proposal.parameters == {"case": "smoke"}

    asyncio.run(scenario())
