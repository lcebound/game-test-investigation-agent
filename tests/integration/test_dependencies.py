"""Real MySQL/S3/Temporal boundaries with deterministic model responses."""
import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from game_test_agent.config import Settings
from game_test_agent.contracts.models import EvidenceRecord, InvestigationRequest
from game_test_agent.evidence.service import EvidenceService
from game_test_agent.storage import ConflictError


def investigation(repository, key):
    return repository.create_investigation(InvestigationRequest(tenant_id="studio-a", project_id="game-a",
        objective={"objective_id": "diagnose", "description": "Inspect frame performance evidence"},
        budget={"max_rounds": 6, "max_wall_seconds": 120}), key,
        principal_id="dependency-test", scopes={"runs:read", "evidence:read"})[0]


def test_mysql_migrations_rollback_idempotency_and_reopen(live_repository):
    repo, connect = live_repository
    key = "dependency-" + uuid4().hex
    view = investigation(repo, key)
    assert investigation(repo, key).investigation_id == view.investigation_id
    repo.reserve_native_read(view.investigation_id, "read-1", "a" * 64, 1)
    with pytest.raises(ConflictError, match="CALL_ID_REUSED"):
        repo.reserve_native_read(view.investigation_id, "read-1", "a" * 64, 1)
    with pytest.raises(RuntimeError, match="rollback"):
        with repo.transaction() as transaction:
            transaction.execute("UPDATE investigations SET stop_reason=? WHERE investigation_id=?",
                                ("must-roll-back", view.investigation_id))
            raise RuntimeError("rollback")
    reopened = connect()
    try:
        assert reopened.get_investigation(view.investigation_id).stop_reason is None
        assert reopened.budget_usage(view.investigation_id)["tool_reads"] == 1
        assert view.investigation_id in {r["investigation_id"] for r in reopened.pending_workflow_starts()}
        repo.transition(view.investigation_id, "discovering")
        assert reopened.get_investigation(view.investigation_id).status == "discovering"
    finally:
        reopened.close()
    # Long Unicode identifiers must remain distinct beyond an index prefix.
    for suffix in ("甲", "乙"):
        record = EvidenceRecord(evidence_id="mysql-" + uuid4().hex, tenant_id="studio-a", project_id="game-a",
            run_ref={"provider_id": "p" * 128, "project_id": "game-a", "run_id": "运" * 256},
            artifact_ref="artifact://studio-a/game-a/" + "a" * 64,
            kind="identity", mime_type="application/json", sha256="a" * 64,
            producer="integration", producer_version="1", quality="partial",
            collected_at=datetime.now(timezone.utc), locator={"kind": "region", "value": "whole"},
            source_type="run", source_evidence_id="源" * 255 + suffix)
        assert repo.add_evidence(view.investigation_id, record).status == "created"
        assert repo.add_evidence(view.investigation_id, record).status == "replayed"


def test_s3_real_roundtrip_encryption_scope_and_corruption(live_artifacts):
    store, client = live_artifacts
    from urllib.parse import unquote, urlparse
    raw = ("UTF-8 evidence 原文 " + uuid4().hex).encode()
    stored = store.put_bytes("studio-a", "game-a", raw)
    assert store.read_bytes(stored.ref) == raw
    assert store.put_bytes("studio-a", "game-a", raw).ref == stored.ref
    assert not store.validate_scope(stored.ref, "studio-a", "other-project")
    key = unquote(urlparse(stored.ref).path.lstrip("/"))
    assert client.head_object(Bucket=store.bucket, Key=key)["ServerSideEncryption"] == "AES256"
    client.put_object(Bucket=store.bucket, Key=key, Body=b"changed-content", ServerSideEncryption="AES256")
    with pytest.raises(IOError):
        store.read_bytes(stored.ref)


def test_temporal_native_worker_restart_replay_and_cancel(live_repository, live_artifacts, dependency_env, tmp_path):
    from temporalio import activity
    from temporalio.client import Client
    from temporalio.exceptions import WorkflowAlreadyStartedError
    from temporalio.worker import Worker, Replayer
    from game_test_agent.workflows.temporal import InvestigationWorkflow, CleanupWorkflow
    from game_test_agent.workflows.worker import CoreActivities
    from native_plan_helpers import initial_plan_call
    from test_native_reliability import call, response
    repo, connect = live_repository
    artifacts, _ = live_artifacts

    async def scenario():
        client = await Client.connect(dependency_env["GTA_TEST_TEMPORAL_ADDRESS"])
        task_queue = "dependency-" + uuid4().hex
        settings = Settings(database_path=tmp_path / "unused.db", artifact_root=tmp_path / "unused",
            enable_model=True, mcp_native_tools=True, workflow_backend="temporal")
        committed = asyncio.Event()
        owners = []
        def owner(repository):
            instance = CoreActivities(repository, settings)
            # The real object store is injected into the shared native controller.
            instance.native.artifacts = artifacts
            instance.native.reporter.artifacts = artifacts
            instance.native.diagnostics.recipes.artifacts = artifacts
            owners.append(instance)
            return instance
        first = owner(repo)
        view = investigation(repo, "restart-" + uuid4().hex)
        raw = json.dumps([{"frame_ms": value} for value in [10, 20, 30]]).encode()
        stored = artifacts.put_bytes("studio-a", "game-a", raw)
        evidence_id = "frames-" + uuid4().hex
        EvidenceService(repo).ingest(view.investigation_id, EvidenceRecord(evidence_id=evidence_id,
            tenant_id="studio-a", project_id="game-a", artifact_ref=stored.ref,
            kind="frames", mime_type="application/json", sha256=hashlib.sha256(raw).hexdigest(),
            producer="integration", producer_version="1", quality="partial",
            collected_at=datetime.now(timezone.utc), locator={"kind": "region", "value": "whole"},
            source_type="user_upload", source_evidence_id=evidence_id))
        async def first_model(*args):
            return response(initial_plan_call(), call("agent_analyze_evidence",
                {"recipe": "frame_time", "evidence_id": evidence_id}, "diagnose-1"))
        first.native._model_call = first_model

        @activity.defn(name="native_investigation_step")
        async def interrupt_after_commit(investigation_id: str, worker_steps: bool = False):
            result = await first.native_investigation_step(investigation_id, worker_steps)
            committed.set()
            # Lose the activity acknowledgement after its durable round checkpoint.
            await asyncio.Future()
            return result

        handle = None
        restarted_repo = None
        try:
            async with Worker(client, task_queue=task_queue, workflows=[InvestigationWorkflow, CleanupWorkflow],
                activities=[first.load_control_state, interrupt_after_commit],
                graceful_shutdown_timeout=timedelta(milliseconds=100)):
                handle = await client.start_workflow(InvestigationWorkflow.run, view.investigation_id,
                    id=view.workflow_id, task_queue=task_queue, execution_timeout=timedelta(seconds=100))
                with pytest.raises(WorkflowAlreadyStartedError):
                    await client.start_workflow(InvestigationWorkflow.run, view.investigation_id,
                        id=view.workflow_id, task_queue=task_queue)
                await asyncio.wait_for(committed.wait(), timeout=30)
            restarted_repo = connect()
            second = owner(restarted_repo)
            async def final_model(*args):
                derived = next(e for e in restarted_repo.list_current_evidence(view.investigation_id)
                               if e.producer == "analysis-recipe")
                quote = json.loads(artifacts.read_bytes(derived.artifact_ref))["summary"]
                return response(content=json.dumps({"summary": quote,
                    "citations": [{"evidence_id": derived.evidence_id, "quote": quote}]}))
            second.native._model_call = final_model
            async with Worker(client, task_queue=task_queue, workflows=[InvestigationWorkflow, CleanupWorkflow],
                activities=[second.load_control_state, second.native_investigation_step,
                            second.cancel_investigation, second.reconcile_cleanup],
                graceful_shutdown_timeout=timedelta(milliseconds=100)):
                assert await asyncio.wait_for(handle.result(), timeout=40) == "completed"
                report = restarted_repo.get_latest_report(view.investigation_id)[0]
                assert restarted_repo.budget_usage(view.investigation_id).get("tool_reads", 0) == 1, report.stop_reason
                assert restarted_repo.get_latest_report(view.investigation_id)[0].claims
                await Replayer(workflows=[InvestigationWorkflow, CleanupWorkflow]).replay_workflow(
                    await handle.fetch_history())

                waiting = asyncio.Event()
                async def blocked_model(*args):
                    waiting.set()
                    await asyncio.Future()
                second.native._model_call = blocked_model
                canceled = investigation(restarted_repo, "cancel-" + uuid4().hex)
                canceled_evidence_id = "cancel-frames-" + uuid4().hex
                EvidenceService(restarted_repo).ingest(canceled.investigation_id,
                    restarted_repo.get_evidence(evidence_id).model_copy(update={
                        "evidence_id": canceled_evidence_id, "source_evidence_id": canceled_evidence_id}))
                canceled_handle = await client.start_workflow(InvestigationWorkflow.run, canceled.investigation_id,
                    id=canceled.workflow_id, task_queue=task_queue, execution_timeout=timedelta(seconds=60))
                await asyncio.wait_for(waiting.wait(), timeout=20)
                await canceled_handle.signal(InvestigationWorkflow.cancel, "cancel-command")
                await canceled_handle.signal(InvestigationWorkflow.cancel, "cancel-command")
                assert await asyncio.wait_for(canceled_handle.result(), timeout=20) == "canceled"
                assert restarted_repo.get_investigation(canceled.investigation_id).status == "canceled"
                snapshot = await canceled_handle.query(InvestigationWorkflow.state_snapshot)
                assert snapshot["processed_signal_count"] == 1
                await Replayer(workflows=[InvestigationWorkflow, CleanupWorkflow]).replay_workflow(
                    await canceled_handle.fetch_history())
        finally:
            for instance in owners:
                await instance.native.close()
                await instance.native_providers.close()
                await instance.providers.close()
            if restarted_repo is not None:
                restarted_repo.close()
    asyncio.run(scenario())
