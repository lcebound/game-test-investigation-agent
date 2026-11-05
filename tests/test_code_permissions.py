from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from game_test_agent.analysis.permissions import CodePermissionStore, validate_directories
from game_test_agent.api import create_app
from game_test_agent.api.auth import Principal
from game_test_agent.contracts.models import CodeExecutionPermissions, CodeExecutionRequest, InvestigationRequest
from game_test_agent.storage import ConflictError


@pytest.fixture
def proposal(repository):
    view, _ = repository.create_investigation(InvestigationRequest(
        tenant_id="studio-a", project_id="game-a",
        objective={"objective_id": "analyze", "description": "analyze files"}), "approval-test")
    request = CodeExecutionRequest(language="python", code="print('review this exact code')",
        purpose="analyze remote data", input_evidence_ids=["ev-selected"],
        permissions={"network": True, "reason": "download a public fixture"})
    store = CodePermissionStore(repository)
    record = store.propose(view.investigation_id, "decision-one", "job-one", request,
                           "sha256:"+"a"*64, {"ev-selected": "b"*64})
    principal = Principal("reviewer", "studio-a", "game-a", frozenset({"agent:analysis:approve"}))
    return store, record, principal, request


def test_permission_proposals_are_immutable_and_survive_restart(proposal, repository):
    store, record, _, request = proposal
    again = CodePermissionStore(repository).propose(record["investigation_id"], "decision-one", "job-one",
        request, "sha256:"+"a"*64, {"ev-selected": "b"*64})
    assert again == record
    assert len(repository.list_recent_events_by_type(record["investigation_id"], "analysis.permission_requested")) == 1
    for changed in [request.model_copy(update={"code": "print('changed')"}),
                    request.model_copy(update={"input_evidence_ids": ["different"]})]:
        with pytest.raises(ConflictError, match="changed"):
            store.propose(record["investigation_id"], "decision-one", "job-one", changed,
                          "sha256:"+"a"*64, {"ev-selected": "b"*64})


def test_only_scoped_approvers_can_decide_and_decisions_are_final(proposal, repository):
    store, record, principal, _ = proposal
    for other in [replace(principal, scopes=frozenset()), replace(principal, project_id="other"),
                  replace(principal, tenant_id="other")]:
        with pytest.raises(PermissionError):
            store.decide(record["approval_id"], record["request_hash"], "approve", other)
    with pytest.raises(ConflictError, match="refresh"):
        store.decide(record["approval_id"], "f"*64, "approve", principal)
    decided = store.decide(record["approval_id"], record["request_hash"], "deny", principal)
    assert decided["status"] == "denied"
    assert store.decide(record["approval_id"], record["request_hash"], "deny", principal) == decided
    with pytest.raises(ConflictError):
        store.decide(record["approval_id"], record["request_hash"], "approve", principal)
    assert len(repository.list_audit_events(record["approval_id"])) == 1


def test_expired_or_canceled_permission_requests_cannot_be_approved(proposal, repository):
    store, record, principal, _ = proposal
    repository._connection.execute("UPDATE code_permission_requests SET expires_at=? WHERE approval_id=?",
        ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(), record["approval_id"]))
    assert store.get(record["approval_id"])["status"] == "expired"
    with pytest.raises(ConflictError):
        store.decide(record["approval_id"], record["request_hash"], "approve", principal)


def test_permission_cancel_is_persistent(proposal):
    store, record, principal, _ = proposal
    store.cancel_pending(record["investigation_id"])
    assert store.get(record["approval_id"])["status"] == "canceled"
    with pytest.raises(ConflictError):
        store.decide(record["approval_id"], record["request_hash"], "approve", principal)


def test_permission_paths_cannot_escape_through_aliases_or_cover_agent_state(tmp_path):
    public = tmp_path / "public"
    private = tmp_path / "private"
    public.mkdir()
    private.mkdir()
    permissions = CodeExecutionPermissions(directories=[{"path": str(public), "access": "read"}], reason="read logs")
    normalized = validate_directories(permissions, [private])
    assert normalized.directories[0].path == str(public.resolve())
    for path in [private, tmp_path, Path(tmp_path.anchor)]:
        requested = permissions.model_copy(update={"directories": [permissions.directories[0].model_copy(update={"path": str(path)})]})
        with pytest.raises((ValueError, PermissionError)):
            validate_directories(requested, [private])
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(public, target_is_directory=True)
    except OSError:
        return  # Windows without developer mode cannot create symlinks.
    requested = permissions.model_copy(update={"directories": [permissions.directories[0].model_copy(update={"path": str(alias)})]})
    with pytest.raises(PermissionError, match="reparse"):
        validate_directories(requested, [private])


def test_external_database_and_config_directories_remain_protected(tmp_path, monkeypatch):
    from game_test_agent.analysis.windows_runner import WindowsSandboxRunner
    from game_test_agent.storage import Repository
    database_dir, config_dir = tmp_path / "isolated-db", tmp_path / "private-config"
    config_dir.mkdir()
    config_file = config_dir / "workbench.env.json"
    config_file.write_text("{}")
    monkeypatch.setenv("GTA_LOCAL_CONFIG_FILE", str(config_file))
    repository = Repository(database_dir / "agent.db")
    try:
        runner = object.__new__(WindowsSandboxRunner)
        runner.root = tmp_path / "runner"
        runner.python = Path(sys.executable)
        runner.powershell = tmp_path / "runtime/powershell.exe"
        runner.artifacts = SimpleNamespace(root=tmp_path / "artifacts")
        runner.permission_store = CodePermissionStore(repository)
        runner.approval_key_path = tmp_path / "approval-key/code-approval.key"
        runner.approval_key_path.parent.mkdir()
        for path in (database_dir, config_dir, runner.approval_key_path.parent):
            permissions = CodeExecutionPermissions(directories=[{"path":str(path),"access":"write"}], reason="read logs")
            with pytest.raises(PermissionError, match="agent state"):
                runner.normalize_permissions(permissions)
    finally:
        repository.close()


def test_permission_http_endpoints_enforce_scope_and_bind_the_review(proposal, settings, repository):
    store, record, _, _ = proposal
    async def run():
        app = create_app(settings, repository)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            auth = {"X-Principal-ID": "human", "X-Tenant-ID": "studio-a", "X-Project-ID": "game-a",
                    "X-Scopes": "agent:analysis:approve", "X-Code-Approval-Key": app.state.local_approval_key}
            url = f"/v1/investigations/{record['investigation_id']}/code-permissions"
            listing = await client.get(url, headers=auth)
            assert listing.json()["items"][0]["request"]["code"] == record["request"]["code"]
            body = {"request_hash": record["request_hash"], "decision": "approve"}
            endpoint = url + f"/{record['approval_id']}/decision"
            # Even a sandbox with localhost network cannot self-approve by forging dev headers.
            forged = {k:v for k,v in auth.items() if k != "X-Code-Approval-Key"}
            assert (await client.post(endpoint, headers=forged, json=body)).status_code == 403
            assert (await client.post(endpoint, headers={**auth,"X-Code-Approval-Key":"wrong"}, json=body)).status_code == 403
            assert (await client.post(endpoint, headers={**auth,"X-Project-ID":"other"}, json=body)).status_code == 403
            assert (await client.post(endpoint, headers={**auth,"X-Scopes":""}, json=body)).status_code == 403
            assert (await client.post(endpoint, headers=auth, json={**body,"request_hash":"f"*64})).status_code == 409
            assert (await client.post(endpoint, headers=auth, json={**body,"code":"replacement"})).status_code == 422
            accepted = await client.post(endpoint, headers=auth, json=body)
            assert accepted.status_code == 200, accepted.text
            assert accepted.json()["status"] == "approved"
            assert accepted.json()["decision"]["principal_id"] == "human"
            snapshot = await client.get(f"/v1/investigations/{record['investigation_id']}/workspace", headers=auth)
            assert snapshot.json()["code_permissions"][0]["status"] == "approved"
            assert app.state.local_approval_key not in snapshot.text
            assert app.state.local_approval_key not in (await client.get("/",headers=auth)).text
    asyncio.run(run())
    assert store.get(record["approval_id"])["status"] == "approved"
