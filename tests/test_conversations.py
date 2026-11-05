"""Conversation behavior: lineage, concurrency, retry safety and authenticated evidence."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx

from game_test_agent.api import create_app
from game_test_agent.contracts.models import (
    EvidenceLocator,
    EvidenceRecord,
    ActionSpec,
    ActionPermit,
    InvestigationRequest,
    InvestigationStatus,
    TestObjective as Objective,
)
from game_test_agent.evidence import EvidenceService
from game_test_agent.storage import LocalArtifactStore

AUTH = {
    "X-Principal-ID": "tester",
    "X-Tenant-ID": "studio-a",
    "X-Project-ID": "game-a",
    "X-Scopes": "tests:read",
}


def request(description="验证加载表现"):
    return InvestigationRequest(
        tenant_id="studio-a",
        project_id="game-a",
        objective=Objective(objective_id="verify", description=description),
    )


async def complete(client, investigation_id):
    path = f"/v1/investigations/{investigation_id}/workspace"
    for _ in range(100):
        view = (await client.get(path, headers=AUTH)).json()
        if view["investigation"]["status"] in {"completed", "failed", "canceled"}:
            return view
        await asyncio.sleep(0.01)
    raise AssertionError("investigation did not finish")


def test_conversation_lineage_metadata_paging_and_message_retries(settings):
    async def scenario():
        app = create_app(settings)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            created = await client.post(
                "/v1/investigations",
                headers={**AUTH, "Idempotency-Key": "root"},
                json=request().model_dump(mode="json"),
            )
            root = created.json()["investigation_id"]
            original = await complete(client, root)
            message = {
                "content": "补充检查 shader warmup",
                "expected_investigation_id": root,
                "build_ref": {"id": "build-followup"},
                "acceptance_criteria": ["无崩溃"],
            }
            path = f"/v1/conversations/{root}"
            response = await client.post(
                path + "/messages", headers={**AUTH, "Idempotency-Key": "followup"}, json=message
            )
            assert response.status_code == 202
            child = response.json()["investigation"]["investigation_id"]
            assert child != root
            child_view = await complete(client, child)
            assert child_view["investigation"]["request"]["build_ref"]["id"] == "build-followup"
            assert child_view["investigation"]["request"]["objective"]["acceptance_criteria"] == [
                "无崩溃"
            ]
            reference = child_view["investigation"]["request"]["constraints"]["followup_reference"]
            assert reference["usage"] == "historical_reference"
            assert reference["report_sha256"] == original["report"]["sha256"]
            retry = await client.post(
                path + "/messages", headers={**AUTH, "Idempotency-Key": "followup"}, json=message
            )
            assert retry.json()["duplicate"] is True
            assert retry.json()["investigation"]["investigation_id"] == child
            changed = await client.post(
                path + "/messages",
                headers={**AUTH, "Idempotency-Key": "followup"},
                json={**message, "content": "different"},
            )
            assert changed.status_code == 409
            changed_build = await client.post(
                path + "/messages",
                headers={**AUTH, "Idempotency-Key": "followup"},
                json={**message, "build_ref": None},
            )
            assert changed_build.status_code == 409
            stale = await client.post(
                path + "/messages", headers={**AUTH, "Idempotency-Key": "new"}, json=message
            )
            assert stale.status_code == 409
            history = (await client.get("/v1/conversations", headers=AUTH)).json()
            assert len(history["items"]) == 1
            assert history["items"][0]["investigation_count"] == 2
            thread = (await client.get(path + "/workspace?limit=1", headers=AUTH)).json()
            assert thread["older_before"] == child
            older = (
                await client.get(path + f"/workspace?before={child}&limit=1", headers=AUTH)
            ).json()
            assert older["investigations"][0]["investigation"]["investigation_id"] == root
            assert older["older_before"] is None
            cursor = app.state.container.repository.latest_event_cursor(root)
            updated = await client.patch(
                path,
                headers=AUTH,
                json={"expected_revision": 0, "title": "图形问题", "pinned": True},
            )
            assert updated.status_code == 200 and updated.json()["revision"] == 1
            assert app.state.container.repository.latest_event_cursor(root) == cursor
            assert (
                await client.patch(
                    path, headers=AUTH, json={"expected_revision": 0, "title": "overwritten"}
                )
            ).status_code == 409
            assert (
                len(
                    (await client.get("/v1/conversations?query=warmup", headers=AUTH)).json()[
                        "items"
                    ]
                )
                == 1
            )
            assert (await client.get("/v1/conversations?query=%25", headers=AUTH)).json()[
                "items"
            ] == []
            stream = await client.get(path + "/stream?seconds=1", headers=AUTH)
            assert stream.status_code == 200 and "event: update" in stream.text
            version = stream.text.split("id: ")[1].split("\n")[0]
            heartbeat = await client.get(path + f"/stream?seconds=1&after={version}", headers=AUTH)
            assert "data: " not in heartbeat.text and ": heartbeat" in heartbeat.text
            assert (
                await client.patch(
                    path, headers=AUTH, json={"expected_revision": 1, "archived": True}
                )
            ).status_code == 200
            assert (await client.get("/v1/conversations", headers=AUTH)).json()["items"] == []
            assert (
                len(
                    (await client.get("/v1/conversations?archived=true", headers=AUTH)).json()[
                        "items"
                    ]
                )
                == 1
            )
            archived = await client.post(
                path + "/messages",
                headers={**AUTH, "Idempotency-Key": "archived"},
                json={"content": "continue", "expected_investigation_id": child},
            )
            assert archived.status_code == 409
            assert (
                await client.get(path + "/workspace", headers={**AUTH, "X-Project-ID": "other"})
            ).status_code == 404
            assert (
                await client.patch(
                    path,
                    headers={**AUTH, "X-Tenant-ID": "other"},
                    json={"expected_revision": 2, "pinned": False},
                )
            ).status_code == 404
            # Archive hides a conversation, without changing reports or investigation status.
            assert (await complete(client, root))["report"]["sha256"] == original["report"][
                "sha256"
            ]

    asyncio.run(scenario())


def test_active_input_retry_does_not_reopen_after_completion(settings):
    async def scenario():
        app = create_app(settings)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            repository = app.state.container.repository
            view, _ = repository.create_investigation(request(), "input")
            path = f"/v1/conversations/{view.investigation_id}/messages"
            message = {
                "content": "这是 DX12 环境",
                "expected_investigation_id": view.investigation_id,
            }
            rejected = await client.post(
                path,
                headers={**AUTH, "Idempotency-Key": "settings"},
                json={**message, "constraints": {}},
            )
            assert rejected.status_code == 409
            response = await client.post(
                path, headers={**AUTH, "Idempotency-Key": "input"}, json=message
            )
            assert response.status_code == 202 and response.json()["duplicate"] is False
            app.state.container.workflows.start(view.investigation_id)
            await complete(client, view.investigation_id)
            replay = await client.post(
                path, headers={**AUTH, "Idempotency-Key": "input"}, json=message
            )
            assert replay.json()["duplicate"] is True
            assert replay.json()["investigation"]["investigation_id"] == view.investigation_id
            assert len(repository.list_user_inputs(view.investigation_id)) == 1
            assert len((await client.get("/v1/conversations", headers=AUTH)).json()["items"]) == 1
            assert (await client.get("/v1/conversations", headers=AUTH)).json()["items"][0][
                "investigation_count"
            ] == 1

    asyncio.run(scenario())


def test_artifact_download_verifies_scope_membership_and_hash(settings):
    async def scenario():
        app = create_app(settings)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            repository = app.state.container.repository
            view, _ = repository.create_investigation(request(), "artifact")
            other, _ = repository.create_investigation(request(), "other")
            artifacts = LocalArtifactStore(settings.artifact_root)
            content = b"\x89PNG\r\n\x1a\nQA binary payload"
            stored = artifacts.put_bytes("studio-a", "game-a", content)
            evidence = EvidenceRecord(
                evidence_id="ev-screen",
                tenant_id="studio-a",
                project_id="game-a",
                artifact_ref=stored.ref,
                kind="screenshot",
                mime_type="image/png",
                sha256=stored.sha256,
                producer="qa",
                producer_version="1",
                collected_at=datetime.now(timezone.utc),
                locator=EvidenceLocator(kind="region", value="full frame"),
                quality="verified",
                source_type="user_upload",
                source_evidence_id="qa-upload",
            )
            EvidenceService(repository).ingest(view.investigation_id, evidence)
            path = f"/v1/investigations/{view.investigation_id}/evidence/ev-screen/download"
            response = await client.get(path, headers=AUTH)
            assert response.status_code == 200 and response.content == content
            assert response.headers["x-artifact-sha256"] == stored.sha256
            assert response.headers["x-content-type-options"] == "nosniff"
            assert "attachment" in response.headers["content-disposition"]
            assert (
                await client.get(path, headers={**AUTH, "X-Project-ID": "other"})
            ).status_code == 403
            assert (
                await client.get(
                    f"/v1/investigations/{other.investigation_id}/evidence/ev-screen/download",
                    headers=AUTH,
                )
            ).status_code == 404
            actual = next(settings.artifact_root.rglob(stored.sha256))
            actual.write_bytes(b"tampered")
            assert (await client.get(path, headers=AUTH)).status_code == 409

    asyncio.run(scenario())


def test_local_background_error_produces_observable_failed_report(settings, caplog):
    async def scenario():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            c = app.state.container
            view, _ = c.repository.create_investigation(request(), "failed")

            async def broken(_):
                raise RuntimeError("secret upstream credential")

            c.workflows.run = broken
            c.workflows.start(view.investigation_id)
            await c.workflows._tasks[view.investigation_id]
            assert (
                c.repository.get_investigation(view.investigation_id).status
                == InvestigationStatus.FAILED
            )
            report, markdown, _ = c.repository.get_latest_report(view.investigation_id)
            assert report.resolution.value == "system_error"
            assert "secret upstream credential" not in markdown

    asyncio.run(scenario())
    assert "secret upstream credential" not in caplog.text


def test_cancel_contacts_provider_once_and_keeps_unconfirmed_cleanup(settings):
    async def scenario():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            c = app.state.container
            view, _ = c.repository.create_investigation(request(), "cancel")
            action = ActionSpec(
                tenant_id="studio-a",
                project_id="game-a",
                investigation_id=view.investigation_id,
                action_id="act-cancel",
                idempotency_key="cancel/1",
                capability_id="test.run",
                capability_version="1",
                provider_id="qa",
                parameters={},
                expected_observation="观察已提交的运行",
                plan_revision=1,
            )
            permit = ActionPermit(
                permit_id="permit-cancel",
                action_id=action.action_id,
                principal_id="tester",
                request_hash="qa",
                expires_at=datetime.now(timezone.utc),
                reserved_device_minutes=0,
            )
            c.repository.reserve_action(
                action, permit, {}, 1, counter_name="real_runs", counter_limit=1
            )
            calls = []

            async def cancel(action_id):
                calls.append(action_id)
                await asyncio.sleep(0)
                return False  # External completion is not confirmed.

            c.workflows.controller = SimpleNamespace(actions=SimpleNamespace(cancel=cancel))
            assert c.workflows.request_cancel(view.investigation_id)
            task = c.workflows._tasks[view.investigation_id]
            assert c.workflows.request_cancel(view.investigation_id)
            assert c.workflows._tasks[view.investigation_id] is task
            await task
            assert calls == ["act-cancel"]
            assert (
                c.repository.get_investigation(view.investigation_id).status
                == InvestigationStatus.CANCELED
            )
            assert c.repository.pending_cleanup_actions(view.investigation_id) == ["act-cancel"]
            assert (
                c.repository.get_latest_report(view.investigation_id)[0].resolution.value
                == "canceled"
            )

    asyncio.run(scenario())
