from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from game_test_agent.api import create_app
from game_test_agent.contracts.models import (
    CapabilityDescriptor,
    InvestigationRequest,
    InvestigationStatus,
    NextStep,
    TestObjective as Objective,
)
from game_test_agent.mcp_gateway.gateway import StandardToolError
from game_test_agent.storage import NotFoundError


AUTH = {
    "X-Principal-ID": "tester",
    "X-Tenant-ID": "studio-a",
    "X-Project-ID": "game-a",
    "X-Scopes": "tests:read,runs:submit",
}


def request(project="game-a", description="验证目标"):
    return InvestigationRequest(
        tenant_id="studio-a",
        project_id=project,
        objective=Objective(objective_id="verify", description=description),
    )


def test_history_is_scoped_and_cursor_cannot_cross_projects(repository):
    a, _ = repository.create_investigation(request(), "a")
    b, _ = repository.create_investigation(request(), "b")
    other, _ = repository.create_investigation(request("other"), "other")
    page = repository.list_investigations("studio-a", "game-a", limit=1)
    assert len(page) == 1
    next_page = repository.list_investigations(
        "studio-a", "game-a", before=page[0].investigation_id, limit=1
    )
    assert {page[0].investigation_id, next_page[0].investigation_id} == {
        a.investigation_id,
        b.investigation_id,
    }
    assert repository.list_investigations("other-tenant", "game-a") == []
    with pytest.raises(NotFoundError):
        repository.list_investigations("studio-a", "game-a", before=other.investigation_id)


def test_workbench_real_history_snapshot_and_report(settings):
    async def scenario():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                shell = await client.get("/")
                assert shell.status_code == 200 and "TRACE" in shell.text
                assert (await client.get("/assets/workbench.js")).status_code == 200
                assert (await client.get("/v1/workspace")).status_code == 422
                config = (await client.get("/v1/workspace", headers=AUTH)).json()
                assert config["providers"] == [] and config["model"]["enabled"] is False
                created = await client.post(
                    "/v1/investigations",
                    headers={**AUTH, "Idempotency-Key": "workbench"},
                    json=request().model_dump(mode="json"),
                )
                investigation_id = created.json()["investigation_id"]
                path = f"/v1/investigations/{investigation_id}/workspace"
                for _ in range(30):
                    snapshot = (await client.get(path, headers=AUTH)).json()
                    if snapshot["report"]:
                        break
                    await asyncio.sleep(0.01)
                assert snapshot["investigation"]["status"] == "completed"
                assert snapshot["report"]["report"]["resolution"] == "inconclusive"
                assert snapshot["events"] and snapshot["plan"]
                assert snapshot["evidence"] == [] and snapshot["runs"] == []
                history = (await client.get("/v1/investigations", headers=AUTH)).json()
                assert history["items"][0]["investigation_id"] == investigation_id
                assert (
                    await client.get(path, headers={**AUTH, "X-Project-ID": "other"})
                ).status_code == 403
                other = (
                    await client.get(
                        "/v1/investigations", headers={**AUTH, "X-Project-ID": "other"}
                    )
                ).json()
                assert other["items"] == []

    asyncio.run(scenario())


def test_controller_resumes_next_round_with_user_input_and_preserves_budget(repository, settings):
    from game_test_agent.activities import ActionService, ReasoningService
    from game_test_agent.capabilities import CapabilityRegistry
    from game_test_agent.evidence import EvidenceManifestIngestor
    from game_test_agent.mcp_gateway import ActionDispatcher
    from game_test_agent.planning import ActionPlanner
    from game_test_agent.policies import PolicyEngine
    from game_test_agent.reports import ReportService
    from game_test_agent.storage import LocalArtifactStore
    from game_test_agent.workflows import InvestigationController

    class AskThenReport:
        model_name = "controlled-test-model"
        calls = 0

        async def reason_next_step(self, context):
            self.calls += 1
            if self.calls == 2:
                assert context["user_inputs"][0]["payload"]["content"] == "Windows Client"
            return NextStep(
                kind="request_input" if self.calls == 1 else "report",
                objective_id="verify",
                reason_summary="need target" if self.calls == 1 else "scope confirmed",
                expected_observation="explicit target",
                missing_fields=["target_kind"] if self.calls == 1 else [],
            )

    async def scenario():
        registry = CapabilityRegistry(repository)
        registry.register(
            CapabilityDescriptor(
                capability_id="test.run",
                capability_version="1",
                provider_id="portable",
                provider_revision="1",
                tool_name="test",
                project_ids=["game-a"],
                input_schema={"type": "object"},
                risk_level="bounded_execution",
                execution_owner="portable",
                resource_domain="portable",
            )
        )
        view, _ = repository.create_investigation(request(), "resume-rounds")
        original_budget = view.request.budget
        investigator = AskThenReport()
        controller = InvestigationController(
            repository,
            registry,
            ReasoningService(repository, investigator),
            ActionPlanner("disabled", enabled=False),
            ActionService(repository, PolicyEngine(repository, registry), ActionDispatcher()),
            EvidenceManifestIngestor(repository, LocalArtifactStore(settings.artifact_root)),
            ReportService(repository),
            provider_lookup=lambda _: None,
        )
        assert (
            await controller.run(view.investigation_id, principal_id="tester", scopes=set()) is None
        )
        current = repository.get_investigation(view.investigation_id)
        assert current.status == InvestigationStatus.WAITING_INPUT
        repository.enqueue_command(
            view.investigation_id,
            "target-input",
            "input",
            {
                "command_id": "target-input",
                "expected_revision": current.revision,
                "content": "Windows Client",
            },
        )
        report = await controller.run(view.investigation_id, principal_id="tester", scopes=set())
        assert report is not None
        assert [
            d.plan_revision for d in repository.list_decision_records(view.investigation_id)
        ] == [1, 2]
        assert repository.get_investigation(view.investigation_id).request.budget == original_budget
        assert repository.list_actions(view.investigation_id) == []

    asyncio.run(scenario())


def test_provider_selection_filters_context_and_blocks_other_platform_actions(repository):
    from game_test_agent.capabilities import CapabilityRegistry
    from game_test_agent.contracts.models import ActionSpec
    from game_test_agent.evidence import ContextBuilder
    from game_test_agent.policies import PolicyEngine

    registry = CapabilityRegistry(repository)
    for provider in ["chosen", "other"]:
        registry.register(
            CapabilityDescriptor(
                capability_id="test.run",
                capability_version="1",
                provider_id=provider,
                provider_revision="1",
                tool_name="test",
                project_ids=["game-a"],
                input_schema={"type": "object"},
                risk_level="bounded_execution",
                execution_owner=provider,
                resource_domain=provider,
            )
        )
    body = request().model_dump()
    body["constraints"] = {"provider_allowlist": ["chosen"]}
    view, _ = repository.create_investigation(InvestigationRequest.model_validate(body), "selected")
    context = ContextBuilder(repository, registry).build(view.investigation_id, scopes=set())
    assert {item.provider_id for item in context.capabilities} == {"chosen"}
    assert {item.provider_id for item in context.context_manifest.capability_summaries} == {
        "chosen"
    }
    action = ActionSpec(
        tenant_id="studio-a",
        project_id="game-a",
        investigation_id=view.investigation_id,
        action_id="forbidden",
        idempotency_key="forbidden",
        capability_id="test.run",
        capability_version="1",
        provider_id="other",
        parameters={},
        expected_observation="result",
        plan_revision=1,
    )
    with pytest.raises(PermissionError, match="allowlist"):
        PolicyEngine(repository, registry).reserve(action, principal_id="tester", scopes=set())
    assert repository.list_actions(view.investigation_id) == []


def test_platform_probe_redacts_secrets_and_catalog_normalizes(settings, monkeypatch):
    monkeypatch.setenv("TEST_PLATFORM_TOKEN", "test-secret-never-returned")
    configured = replace(
        settings,
        mcp_providers_json=json.dumps(
            [
                {
                    "provider_id": "platform-local",
                    "project_ids": ["game-a"],
                    "url": "http://127.0.0.1:8010/mcp",
                    "allow_insecure_localhost": True,
                    "tests_search_tool": "gt_tests_search",
                    "token_env": "TEST_PLATFORM_TOKEN",
                }
            ]
        ),
    )

    async def capabilities(self):
        raise StandardToolError("INTEGRATION_NOT_CONFIGURED", "test-secret-never-returned")

    async def search(self, tool_name, arguments, **kwargs):
        assert tool_name == "gt_tests_search"
        return {
            "items": [
                {
                    "test_ref": {"test_id": "case:portable"},
                    "name": "Portable test",
                    "target_kinds": ["windows_client"],
                    "platform_only": "do not leak",
                }
            ],
            "total": 1,
            "page": arguments["page"],
            "page_size": arguments["page_size"],
        }

    from game_test_agent.mcp_gateway.http_provider import McpStreamableHttpProvider

    monkeypatch.setattr(McpStreamableHttpProvider, "discover_capabilities", capabilities)
    monkeypatch.setattr(McpStreamableHttpProvider, "_call", search)

    async def scenario():
        app = create_app(configured)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                config = await client.get("/v1/workspace", headers=AUTH)
                assert "test-secret" not in config.text and "token_env" not in config.text
                assert config.json()["providers"][0]["catalog_supported"] is True
                probe = await client.post("/v1/providers/platform-local/probe", headers=AUTH)
                assert probe.json()["error_code"] == "INTEGRATION_NOT_CONFIGURED"
                assert "test-secret" not in probe.text
                no_read = {**AUTH, "X-Scopes": "runs:submit"}
                assert (
                    await client.post("/v1/providers/platform-local/probe", headers=no_read)
                ).status_code == 403
                assert (
                    await client.post(
                        "/v1/providers/platform-local/probe",
                        headers={**AUTH, "X-Project-ID": "other"},
                    )
                ).status_code == 404
                catalog = await client.get(
                    "/v1/providers/platform-local/tests?page=2&page_size=5", headers=AUTH
                )
                assert catalog.status_code == 200 and catalog.json()["page"] == 2
                assert "platform_only" not in catalog.text
                assert catalog.json()["items"][0]["test_ref"]["test_id"] == "case:portable"

    asyncio.run(scenario())


def test_local_input_wakes_waiting_investigation_once(settings):
    async def scenario():
        app = create_app(settings)
        repo = app.state.container.repository
        view, _ = repo.create_investigation(request(), "waiting")
        for status in [
            InvestigationStatus.DISCOVERING,
            InvestigationStatus.PLANNING,
            InvestigationStatus.WAITING_INPUT,
        ]:
            view = repo.transition(view.investigation_id, status)
        calls = []
        monkey_workflows = app.state.container.workflows
        monkey_workflows.start = lambda investigation_id: calls.append(investigation_id)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                body = {
                    "command_id": "input-once",
                    "expected_revision": view.revision,
                    "content": "目标是 Windows Client",
                }
                path = f"/v1/investigations/{view.investigation_id}/inputs"
                assert (await client.post(path, headers=AUTH, json=body)).status_code == 202
                duplicate = await client.post(path, headers=AUTH, json=body)
                assert duplicate.json()["duplicate"] is True
                assert calls == [view.investigation_id]
                assert repo.pending_commands() == []
                assert (
                    repo.list_user_inputs(view.investigation_id)[0]["payload"]["content"]
                    == body["content"]
                )

    asyncio.run(scenario())
