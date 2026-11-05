"""Pinned history chains, explicit bindings and isolation under replay/revocation."""
import asyncio
import hashlib
import json
import time
from dataclasses import asdict
from datetime import datetime, timezone
from unittest.mock import Mock

import httpx
import pytest

from game_test_agent.api import create_app
from game_test_agent.contracts.models import (
    Claim, ContextAccessScope, EvidenceLocator, EvidenceRecord, InvestigationRequest, ReopenRequest,
)
from game_test_agent.context import ContextAccess
from game_test_agent.evidence import EvidenceService
from game_test_agent.memory.recall import HistorySearch, RecallLink, RecallService
from game_test_agent.reports import ReportService
from game_test_agent.reports.builder import ReportBuilder
from game_test_agent.storage import ConflictError, LocalArtifactStore, canonical_json
from game_test_agent.workflows.native_context import NativeMemory
from game_test_agent.workflows.native_recall import ContextRead
from native_plan_helpers import initial_plan_call
from test_native_recall import memory
from test_native_reliability import call, response, setup


SCOPES = {"runs:read", "evidence:read"}
AUTH = {"X-Principal-ID": "tester", "X-Tenant-ID": "studio-a", "X-Project-ID": "game-a",
        "X-Scopes": "runs:read,evidence:read"}
TEXT = "历史原文：恢复尚未确认。" * 80 + "HISTORY_RAW_SECRET"


def request(description="当前任务", **kwargs):
    return InvestigationRequest(tenant_id="studio-a", project_id="game-a",
        objective={"objective_id": "investigate", "description": description}, **kwargs)


def target(repository, key="target", **kwargs):
    return repository.create_investigation(request(**kwargs), key, principal_id="tester", scopes=SCOPES)[0]


def source(repository, settings, key="source", *, text="旧任务：调查恢复", evidence=True):
    view = target(repository, key, description=text, constraints={"private": "CONSTRAINT_SECRET"})
    artifacts = LocalArtifactStore(settings.artifact_root)
    ids = []
    if evidence:
        for index in range(2):
            stored = artifacts.put_bytes("studio-a", "game-a", (TEXT + str(index)).encode())
            record = EvidenceRecord(evidence_id=f"ev-{key}/{index}", tenant_id="studio-a", project_id="game-a",
                artifact_ref=stored.ref, sha256=stored.sha256, kind="log", mime_type="text/plain",
                producer="test", producer_version="1", quality="untrusted",
                collected_at=datetime.now(timezone.utc), locator=EvidenceLocator(kind="line_range", value="all"),
                source_type="user_upload", source_evidence_id=f"source-{key}-{index}")
            assert EvidenceService(repository).ingest(view.investigation_id, record).status == "created"
            ids.append(record.evidence_id)
    report = ReportBuilder(repository).build(view.investigation_id, "historical", summary="旧调查没有独立确认恢复。")
    if ids:
        excerpt = ContextAccess(repository, artifacts).read_evidence(view.investigation_id, ids[0],
            ContextAccessScope(tenant_id="studio-a", project_id="game-a"), max_bytes=4096)
        repository.record_investigation_event(view.investigation_id, "evidence.excerpt_read", {"excerpt": excerpt.model_dump(mode="json")})
        report = report.model_copy(update={"claims": [Claim(claim_id="old-observation", text="恢复尚未确认。",
            basis="evidence_excerpt", supporting_excerpt="恢复尚未确认。", evidence_ids=[ids[0]])]})
    ReportService(repository, artifacts).publish(report)
    with repository.transaction() as connection:
        connection.execute("UPDATE investigations SET status='completed' WHERE investigation_id=?", (view.investigation_id,))
    actual, _, digest = repository.get_latest_report(view.investigation_id)
    return repository.get_investigation(view.investigation_id), actual, digest, ids


def bind(service, own, old, digest):
    return service.link(own, RecallLink(source_investigation_id=old.investigation_id, report_sha256=digest))


def test_chain_reads_pinned_report_and_only_its_cited_originals(repository, settings):
    old, report, digest, ids = source(repository, settings)
    own = target(repository)
    artifacts = LocalArtifactStore(settings.artifact_root)
    service = RecallService(repository, artifacts)
    bind(service, own, old, digest)
    directory = service.read(own, ContextRead(ref="history"))
    report_page = service.read(own, ContextRead(ref=directory["items"][0]["read_ref"]))
    assert json.loads(report_page["result"])["investigation_id"] == old.investigation_id
    evidence = service.read(own, ContextRead(ref=report_page["evidence_directory_ref"]))
    assert [item["source_evidence_id"] for item in evidence["items"]] == [ids[0]]
    ref = evidence["items"][0]["read_ref"]
    fragments, offset = [], 0
    while True:
        page = service.read(own, ContextRead(ref=ref, byte_offset=offset, max_bytes=128))
        fragments.append(page["result"])
        assert page["historical"] and page["source_investigation_id"] == old.investigation_id
        if page["next_byte_offset"] is None:
            break
        offset = page["next_byte_offset"]
    assert "".join(fragments) == TEXT + "0"
    assert not repository.list_current_evidence(own.investigation_id)
    with pytest.raises(PermissionError):
        service.read(own, ContextRead(ref=f"history/{old.investigation_id}/evidence/{ids[1]}"))
    assert repository.get_investigation(own.investigation_id).request.budget == own.request.budget


def test_discovery_is_bounded_literal_metadata_and_does_not_grant_access(repository, settings):
    for i in range(9):
        source(repository, settings, str(i), evidence=False)
    own = target(repository)
    service = RecallService(repository, LocalArtifactStore(settings.artifact_root))
    page = service.search(own, HistorySearch(query="旧任务", limit=3))
    assert len(page["items"]) == 3 and page["next_cursor"]
    assert len(canonical_json(page).encode()) <= 4096
    assert all(card["access"] == "binding_required" for card in page["items"])
    assert "CONSTRAINT_SECRET" not in canonical_json(page)
    assert not service.search(own, HistorySearch(query="CONSTRAINT_SECRET"))["items"]
    next_page = service.search(own, HistorySearch(query="旧任务", before=page["next_cursor"], limit=3))
    assert not ({c["source_investigation_id"] for c in page["items"]} & {c["source_investigation_id"] for c in next_page["items"]})
    selected = page["items"][0]
    with pytest.raises(PermissionError):
        service.read(own, ContextRead(ref=f"history/{selected['source_investigation_id']}/report"))


@pytest.mark.parametrize("field", ["tenant_id", "project_id"])
def test_foreign_history_cannot_be_discovered_linked_or_read(repository, settings, field):
    old, _, digest, _ = source(repository, settings)
    own = target(repository)
    altered = own.request.model_copy(update={field: "foreign"})
    foreign, _ = repository.create_investigation(altered, "foreign")
    service = RecallService(repository, LocalArtifactStore(settings.artifact_root))
    assert not service.search(foreign, HistorySearch(query="旧任务"))["items"]
    with pytest.raises(PermissionError):
        bind(service, foreign, old, digest)
    with pytest.raises(PermissionError):
        service.read(foreign, ContextRead(ref=f"history/{old.investigation_id}/report"))


def test_initial_references_are_atomic_and_idempotent_replay_does_not_reactivate(repository, settings):
    old, _, digest, _ = source(repository, settings)
    refs = [{"source_investigation_id": old.investigation_id, "report_sha256": digest}]
    own = target(repository, constraints={"recall_references": refs})
    service = RecallService(repository)
    assert service.inline(own)["total_sources"] == 1
    service.revoke(own, old.investigation_id)
    assert target(repository, constraints={"recall_references": refs}).investigation_id == own.investigation_id
    assert service.inline(own)["total_sources"] == 0
    before = len(repository.list_investigations("studio-a", "game-a"))
    with pytest.raises(ConflictError):
        target(repository, "bad", constraints={"recall_references": [{**refs[0], "report_sha256": "0" * 64}]})
    assert len(repository.list_investigations("studio-a", "game-a")) == before


def test_pinned_report_does_not_silently_follow_a_new_report_revision(repository, settings):
    old, report, digest, _ = source(repository, settings)
    own = target(repository)
    service = RecallService(repository)
    bind(service, own, old, digest)
    updated = report.model_copy(update={"revision": report.revision + 1, "summary": "另一份历史报告"})
    ReportService(repository).publish(updated)
    assert service.search(own, HistorySearch(query="旧任务"))["items"][0]["access"] == "binding_required"
    ref = f"history/{old.investigation_id}/report"
    assert service.read(own, ContextRead(ref=ref))["report_revision"] == report.revision
    new_digest = repository.get_latest_report(old.investigation_id)[2]
    bind(service, own, old, new_digest)
    assert service.read(own, ContextRead(ref=ref))["report_revision"] == updated.revision
    assert service.search(own, HistorySearch(query="旧任务"))["items"][0]["access"] == "attached"


@pytest.mark.parametrize("damage", ["expired", "artifact", "manifest"])
def test_unavailable_or_modified_history_fails_closed(repository, settings, damage):
    old, _, digest, ids = source(repository, settings)
    own = target(repository)
    artifacts = LocalArtifactStore(settings.artifact_root)
    service = RecallService(repository, artifacts)
    bind(service, own, old, digest)
    if damage == "expired":
        record = repository.get_evidence(ids[0])
        repository._connection.execute("UPDATE evidence_records SET evidence_json=? WHERE evidence_id=?", (record.model_copy(update={"retrieval_status": "expired"}).model_dump_json(), ids[0]))
    elif damage == "artifact":
        record = repository.get_evidence(ids[0])
        next(settings.artifact_root.rglob(record.sha256)).write_bytes(b"damaged")
    else:
        repository._connection.execute("UPDATE investigation_recall_links SET manifest_json='{}' WHERE investigation_id=?", (own.investigation_id,))
    with pytest.raises((ValueError, LookupError, PermissionError, OSError)):
        service.read(own, ContextRead(ref=f"history/{old.investigation_id}/evidence/{ids[0]}"))


def test_revocation_invalidates_cached_reads_and_saved_conversation_context(repository, settings):
    async def scenario():
        old, _, digest, ids = source(repository, settings)
        own, controller, _ = setup(repository, settings)
        service = RecallService(repository, controller.artifacts)
        bind(service, own, old, digest)
        state = memory()
        state.sync_inputs(repository, own.investigation_id)
        ref = f"history/{old.investigation_id}/evidence/{ids[0]}"
        first, progress = await controller._execute_call(own, call("agent_read_context", {"ref": ref}, "history-1"), {}, state, time.time() + 30)
        assert progress and first["historical"]
        reads = repository.budget_usage(own.investigation_id)["tool_reads"]
        cached, progress = await controller._execute_call(own, call("agent_read_context", {"ref": ref}, "history-cache"), {}, state, time.time() + 30)
        assert cached == first and not progress
        assert repository.budget_usage(own.investigation_id)["tool_reads"] == reads
        state.messages.append({"role": "assistant", "content": "HISTORY_RAW_SECRET"})
        state.save(repository, controller.artifacts, own)
        service.revoke(own, old.investigation_id)
        rejected, _ = await controller._execute_call(own, call("agent_read_context", {"ref": ref}, "history-revoked"), {}, state, time.time() + 30)
        assert "result" in rejected and rejected.get("error_code")
        restored = NativeMemory.restore(repository, controller.artifacts, own)
        assert restored.sync_inputs(repository, own.investigation_id)
        assert len(restored.messages) == 3
        assert "HISTORY_RAW_SECRET" not in canonical_json(restored.messages)
        assert not repository.list_current_evidence(own.investigation_id)
    asyncio.run(scenario())


def test_followup_automatically_attaches_only_the_named_parent_report(repository, settings):
    old, _, digest, _ = source(repository, settings)
    child, _ = repository.reopen_investigation(old.investigation_id,
        ReopenRequest(reason="上次为什么没有确认恢复", budget=old.request.budget,
            constraints={"followup_reference": {"investigation_id": old.investigation_id, "report_sha256": digest}}),
        "followup", principal_id="tester", scopes=SCOPES)
    service = RecallService(repository)
    assert service.inline(child)["total_sources"] == 1
    assert service.read(child, ContextRead(ref=f"history/{old.investigation_id}/report"))["historical"]


def test_native_worker_discovers_history_on_demand_and_rechecks_mid_turn_revocation(repository, settings):
    async def scenario():
        old, _, digest, _ = source(repository, settings)
        own, controller, provider = setup(repository, settings)
        service = RecallService(repository)
        bind(service, own, old, digest)
        async def model(messages, tools):
            assert "agent_find_history" in {t["function"]["name"] for t in tools}
            assert json.loads(messages[2]["content"])["historical_sources"]["items"]
            service.revoke(own, old.investigation_id)
            return response(initial_plan_call(), call())
        controller._model_call = model
        result = await controller.run_step(own.investigation_id, principal_id="tester", scopes=SCOPES)
        assert result.state == "continue" and result.continuation.next_turn == 2
        provider.call_native.assert_not_awaited()
    asyncio.run(scenario())


def test_utf8_offsets_literal_search_hashes_and_directory_envelopes(repository, settings):
    old, _, digest, ids = source(repository, settings)
    own = target(repository)
    service = RecallService(repository, LocalArtifactStore(settings.artifact_root))
    bind(service, own, old, digest)
    ref = f"history/{old.investigation_id}/evidence/{ids[0]}"
    found = service.read(own, ContextRead(ref=ref, query="恢复尚未确认"))
    assert found["result"].startswith("恢复尚未确认")
    with pytest.raises(ValueError):
        service.read(own, ContextRead(ref=ref, byte_offset=1))
    with pytest.raises(ValueError):
        service.read(own, ContextRead(ref=ref, expected_sha256="0" * 64))
    with pytest.raises(ValueError):
        service.read(own, ContextRead(ref="history", max_bytes=128))
    assert found["sha256"] == hashlib.sha256((TEXT + "0").encode()).hexdigest()


def test_checkpoint_five_can_restore_four_without_history_state(repository, settings):
    own, controller, _ = setup(repository, settings)
    state = memory()
    old = asdict(state)
    old.pop("historical_sources")
    old.pop("history_read_ranges")
    artifact = controller.artifacts.put_bytes("studio-a", "game-a", canonical_json({"investigation_id": own.investigation_id, "state": old}).encode())
    repository.record_investigation_event(own.investigation_id, "mcp.native.checkpoint", {"version": 4, "artifact_ref": artifact.ref})
    restored = NativeMemory.restore(repository, controller.artifacts, own)
    assert restored.historical_sources == {} and restored.history_read_ranges == {}
    restored.save(repository, controller.artifacts, own)
    assert repository.list_recent_events_by_type(own.investigation_id, "mcp.native.checkpoint", limit=1)[0]["data"]["version"] == 6


def test_history_api_permissions_precreate_binding_and_scoped_originals(settings):
    async def scenario():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            repo = app.state.container.repository
            app.state.container.workflows.start = Mock()
            old, _, digest, ids = source(repo, settings)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                page = await client.get("/v1/history/search?query=旧任务", headers=AUTH)
                assert page.status_code == 200 and page.json()["items"]
                assert (await client.get("/v1/history/search?query=%20", headers=AUTH)).status_code == 422
                assert (await client.get("/v1/history/search?query=旧任务", headers={**AUTH, "X-Scopes": "tests:read"})).status_code == 403
                body = request(constraints={"recall_references": [{"source_investigation_id": old.investigation_id, "report_sha256": digest}]}).model_dump(mode="json")
                created = await client.post("/v1/investigations", headers={**AUTH, "Idempotency-Key": "with-history"}, json=body)
                assert created.status_code == 202
                own = created.json()["investigation_id"]
                path = f"/v1/investigations/{own}/recall"
                assert (await client.get(path, headers=AUTH)).json()["total_sources"] == 1
                ref = f"history/{old.investigation_id}/evidence/{ids[0]}"
                assert (await client.post(path + "/read", headers=AUTH, json={"ref": ref})).json()["historical"]
                assert (await client.post(path + "/read", headers={**AUTH, "X-Project-ID": "other"}, json={"ref": ref})).status_code == 403
                assert (await client.delete(path + "/" + old.investigation_id, headers=AUTH)).status_code == 200
                assert (await client.post(path + "/read", headers=AUTH, json={"ref": ref})).status_code == 403
                bad = await client.post("/v1/investigations", headers={**AUTH, "Idempotency-Key": "malformed"},
                    json=request(constraints={"recall_references": "invalid"}).model_dump(mode="json"))
                assert bad.status_code == 422
    asyncio.run(scenario())


def test_discovery_can_continue_past_a_page_with_no_finished_matches(repository, settings):
    old, _, _, _ = source(repository, settings, evidence=False)
    own = target(repository)
    for i in range(101):
        target(repository, f"active-{i}", description="运行中的其他调查")
    service = RecallService(repository)
    first = service.search(own, HistorySearch(query="旧任务"))
    assert not first["items"] and not first["complete"] and first["next_cursor"]
    second = service.search(own, HistorySearch(query="旧任务", before=first["next_cursor"]))
    assert second["items"][0]["source_investigation_id"] == old.investigation_id


def test_pending_report_cannot_recover_across_a_history_binding_change(repository, settings):
    old, _, digest, _ = source(repository, settings, evidence=False)
    own, controller, provider = setup(repository, settings)
    tool = asyncio.run(provider.native_tools())[0]
    payload, _ = asyncio.run(controller._execute_call(own, call(),
        {"inspect_incident": (provider, tool)}, memory(), time.time() + 30))
    state = {"investigation_id": own.investigation_id, "input_ids": [],
             "diagnosis": {"summary": "已读取当前证据", "citations": [{"evidence_id": payload["evidence_id"], "quote": "47 ms"}]}, "reason": "draft"}
    artifact = controller.artifacts.put_bytes("studio-a", "game-a", canonical_json(state).encode())
    repository.record_investigation_event(own.investigation_id, "mcp.native.diagnosis_ready", {"artifact_ref": artifact.ref})
    assert controller._pending_diagnosis(own) is not None
    bind(RecallService(repository), own, old, digest)
    assert controller._pending_diagnosis(own) is None


def test_twenty_source_bound_and_revocation_free_a_reference_slot(repository, settings):
    own = target(repository)
    service = RecallService(repository)
    sources = [source(repository, settings, str(i), evidence=False) for i in range(21)]
    for old, _, digest, _ in sources[:20]:
        bind(service, own, old, digest)
    old, _, digest, _ = sources[-1]
    with pytest.raises(ValueError, match="20"):
        bind(service, own, old, digest)
    service.revoke(own, sources[0][0].investigation_id)
    bind(service, own, old, digest)
    assert service.inline(own)["total_sources"] == 20
