from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx
from game_test_agent.api import create_app
from game_test_agent.contracts.models import (
    EvidenceLocator,
    EvidenceRecord,
    Fact,
    InvestigationRequest,
    TestObjective as Objective,
)
from game_test_agent.evidence import EvidenceService
from game_test_agent.memory import InvestigationSummaryService


def test_api_runs_local_workflow_to_bounded_report(settings):
    async def scenario() -> None:
        app = create_app(settings)
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                auth = {
                    "X-Principal-ID": "tester",
                    "X-Tenant-ID": "studio-a",
                    "X-Project-ID": "game-a",
                }
                response = await client.post(
                    "/v1/investigations",
                    headers={**auth, "Idempotency-Key": "demo-1"},
                    json={
                        "tenant_id": "studio-a",
                        "project_id": "game-a",
                        "objective": {"objective_id": "perf", "description": "验证帧耗时"},
                    },
                )
                assert response.status_code == 202
                investigation_id = response.json()["investigation_id"]
                for _ in range(20):
                    current = await client.get(
                        f"/v1/investigations/{investigation_id}", headers=auth
                    )
                    if current.json()["status"] == "completed":
                        break
                    await asyncio.sleep(0.01)
                assert current.json()["status"] == "completed"
                assert current.json()["budget_usage"]["action_count"] == 0
                assert current.json()["evidence_gaps"] == ["no_evidence_ingested"]
                assert current.json()["external_runs"] == []
                report = await client.get(
                    f"/v1/investigations/{investigation_id}/reports/1", headers=auth
                )
                assert report.status_code == 200
                assert report.json()["report"]["resolution"] == "inconclusive"
                assert "证据" in report.json()["markdown"]
                canceled = await client.post(
                    f"/v1/investigations/{investigation_id}/cancel",
                    headers=auth,
                    json={"command_id": "cancel-after-completion"},
                )
                assert canceled.status_code == 202
                assert canceled.json()["already_terminal"] is True
                assert app.state.container.repository.pending_commands() == []

    asyncio.run(scenario())


def test_api_rejects_missing_or_cross_project_principal(settings):
    async def scenario() -> None:
        app = create_app(settings)
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                body = {
                    "tenant_id": "studio-a",
                    "project_id": "game-a",
                    "objective": {"objective_id": "perf", "description": "验证帧耗时"},
                }
                missing = await client.post(
                    "/v1/investigations",
                    headers={"Idempotency-Key": "missing-auth"},
                    json=body,
                )
                assert missing.status_code == 422
                mismatch = await client.post(
                    "/v1/investigations",
                    headers={
                        "Idempotency-Key": "wrong-project",
                        "X-Principal-ID": "tester",
                        "X-Tenant-ID": "studio-a",
                        "X-Project-ID": "other-game",
                    },
                    json=body,
                )
                assert mismatch.status_code == 403

    asyncio.run(scenario())


def test_api_registers_and_reads_immutable_project_pack(settings):
    async def scenario() -> None:
        app = create_app(settings)
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                auth = {
                    "X-Principal-ID": "qa-lead",
                    "X-Tenant-ID": "studio-a",
                    "X-Project-ID": "game-a",
                    "X-Scopes": "agent:project-pack:write",
                }
                pack = {
                    "pack_id": "game-a-default",
                    "version": "2026.09",
                    "tenant_id": "studio-a",
                    "project_id": "game-a",
                    "terminology": {"frame": "rendered frame"},
                    "reviewed_by": "qa-lead",
                }
                created = await client.post("/v1/project-packs", headers=auth, json=pack)
                assert created.status_code == 201
                replay = await client.post("/v1/project-packs", headers=auth, json=pack)
                assert replay.status_code == 200
                assert replay.headers["Idempotent-Replay"] == "true"
                fetched = await client.get("/v1/project-packs/2026.09", headers=auth)
                assert fetched.status_code == 200
                assert fetched.json()["terminology"] == {"frame": "rendered frame"}
                listed = await client.get("/v1/project-packs", headers=auth)
                assert len(listed.json()) == 1

                changed = {**pack, "terminology": {"frame": "changed"}}
                conflict = await client.post("/v1/project-packs", headers=auth, json=changed)
                assert conflict.status_code == 409

    asyncio.run(scenario())


def test_api_exposes_scoped_evidence_search_and_bounded_read(settings):
    async def scenario() -> None:
        app = create_app(settings)
        repository = app.state.container.repository
        investigation, _ = repository.create_investigation(
            InvestigationRequest(
                tenant_id="studio-a",
                project_id="game-a",
                objective=Objective(objective_id="evidence", description="读取证据"),
            ),
            "api-evidence",
        )
        content = b"bounded evidence content"
        stored = app.state.container.context_access.artifacts.put_bytes(
            "studio-a", "game-a", content
        )
        EvidenceService(repository).ingest(
            investigation.investigation_id,
            EvidenceRecord(
                evidence_id="ev-api",
                tenant_id="studio-a",
                project_id="game-a",
                artifact_ref=stored.ref,
                kind="log",
                mime_type="text/plain",
                sha256=stored.sha256,
                producer="portable-provider",
                producer_version="1",
                collected_at=datetime.now(timezone.utc),
                locator=EvidenceLocator(kind="line_range", value="1"),
                quality="verified",
                source_type="user_upload",
                source_evidence_id="api-upload",
            ),
        )
        # This read-only fixture is waiting for user input and must not auto-run on startup.
        for state in ("discovering", "planning", "waiting_input"):
            repository.transition(investigation.investigation_id, state)
        summary = InvestigationSummaryService(repository).refresh(investigation.investigation_id)
        transport = httpx.ASGITransport(app=app)
        auth = {
            "X-Principal-ID": "tester",
            "X-Tenant-ID": "studio-a",
            "X-Project-ID": "game-a",
        }
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                search = await client.post(
                    f"/v1/investigations/{investigation.investigation_id}/evidence/search",
                    headers=auth,
                    params={"query": "portable"},
                    json={"kind": "log"},
                )
                assert search.status_code == 200
                assert search.json()[0]["evidence"]["evidence_id"] == "ev-api"
                read = await client.get(
                    f"/v1/investigations/{investigation.investigation_id}/evidence/ev-api",
                    headers=auth,
                    params={"max_bytes": 7},
                )
                assert read.status_code == 200
                assert read.json()["text"] == "bounded"
                assert read.json()["next_byte_offset"] == 7
                summary_response = await client.get(
                    f"/v1/investigations/{investigation.investigation_id}/context-summary",
                    headers=auth,
                )
                assert summary_response.status_code == 200
                assert summary_response.json()["summary_id"] == summary.summary_id

    asyncio.run(scenario())


def test_api_exposes_compilation_manifest_provenance_and_audit(settings):
    async def scenario() -> None:
        app = create_app(settings)
        repository = app.state.container.repository
        investigation, _ = repository.create_investigation(
            InvestigationRequest(
                tenant_id="studio-a",
                project_id="game-a",
                objective=Objective(
                    objective_id="inspect",
                    description="inspect architecture projections",
                    acceptance_criteria=["produce a bounded result"],
                ),
            ),
            "api-architecture-projections",
        )
        repository.add_fact(
            Fact(
                fact_id="fact-api-source",
                investigation_id=investigation.investigation_id,
                statement="fact with source",
                evidence_ids=["ev-api-source"],
                extractor="test",
                extractor_version="1",
            )
        )
        auth = {
            "X-Principal-ID": "tester",
            "X-Tenant-ID": "studio-a",
            "X-Project-ID": "game-a",
        }
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                manifest = await client.get(
                    f"/v1/investigations/{investigation.investigation_id}/context-manifest",
                    headers=auth,
                    params={"step_id": "inspect"},
                )
                assert manifest.status_code == 200
                assert manifest.json()["step_id"] == "inspect"

                compilation = await client.get(
                    f"/v1/investigations/{investigation.investigation_id}/objective-compilation",
                    headers=auth,
                )
                assert compilation.status_code == 200
                assert compilation.json()["objective_ir"]["objective_id"] == "inspect"
                assert compilation.json()["selected_candidate_id"]

                provenance = await client.get(
                    f"/v1/investigations/{investigation.investigation_id}/provenance/fact-api-source",
                    headers=auth,
                )
                assert provenance.status_code == 200
                assert provenance.json()["source_ids"] == ["ev-api-source"]

                integrity = await client.get(
                    f"/v1/investigations/{investigation.investigation_id}/audit/integrity",
                    headers=auth,
                )
                assert integrity.status_code == 200
                assert integrity.json()["complete"] is True

    asyncio.run(scenario())
