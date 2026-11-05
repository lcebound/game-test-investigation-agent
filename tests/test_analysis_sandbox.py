from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from pydantic import ValidationError

from game_test_agent.analysis import AnalysisSandboxService, SandboxPolicy
from game_test_agent.api import create_app
from game_test_agent.api.app import DerivedFactVerificationRequest
from game_test_agent.capabilities import CapabilityRegistry
from game_test_agent.contracts.models import (
    AnalysisJobResult,
    AnalysisJobSpec,
    AnalysisOutputItem,
    AnalysisProgram,
    DerivedFactCandidate,
    EvidenceLocator,
    EvidenceRecord,
    Fact,
    InvestigationRequest,
    NextStep,
    SandboxEnvironment,
    SandboxOperationRef,
    SandboxResourceLimits,
    TestObjective as Objective,
)
from game_test_agent.evidence import EvidenceService
from game_test_agent.evidence.context import ContextBuilder
from game_test_agent.runtime import InvestigationKernel
from game_test_agent.storage import ConflictError, LocalArtifactStore


IMAGE_DIGEST = "sha256:" + "a" * 64
LOCK_HASH = "b" * 64


def _evidence(repository, artifacts, investigation_id: str, evidence_id: str, content: bytes):
    stored = artifacts.put_bytes("studio-a", "game-a", content)
    record = EvidenceRecord(
        evidence_id=evidence_id,
        tenant_id="studio-a",
        project_id="game-a",
        artifact_ref=stored.ref,
        kind="analysis_code" if evidence_id == "ev-code" else "log",
        mime_type="text/x-python" if evidence_id == "ev-code" else "text/plain",
        sha256=stored.sha256,
        producer="user",
        producer_version="1",
        collected_at=datetime.now(timezone.utc),
        locator=EvidenceLocator(kind="region", value="whole"),
        quality="untrusted",
        source_type="user_upload",
        source_evidence_id=evidence_id,
    )
    assert EvidenceService(repository).ingest(investigation_id, record).status == "created"
    return record


def _setup(repository, settings):
    investigation, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective=Objective(objective_id="analyze", description="分析日志"),
        ),
        "analysis-source",
    )
    artifacts = LocalArtifactStore(settings.artifact_root)
    code = _evidence(
        repository,
        artifacts,
        investigation.investigation_id,
        "ev-code",
        b"def main(inputs, output):\n    return 0\n",
    )
    source = _evidence(
        repository,
        artifacts,
        investigation.investigation_id,
        "ev-log",
        b"frame=42\n",
    )
    policy = SandboxPolicy(
        allowed_image_digests={IMAGE_DIGEST},
        allowed_runner_ids={"isolated-runner"},
        max_jobs_per_investigation=2,
    )
    service = AnalysisSandboxService(repository, artifacts, policy)
    spec = AnalysisJobSpec(
        job_id="analysis-1",
        idempotency_key="analysis/idempotency-1",
        tenant_id="studio-a",
        project_id="game-a",
        investigation_id=investigation.investigation_id,
        objective_id="analyze",
        purpose="从已授权日志提取确定性统计",
        input_evidence_ids=[source.evidence_id],
        program=AnalysisProgram(
            language="python",
            code_evidence_id=code.evidence_id,
            code_artifact_ref=code.artifact_ref,
            code_sha256=code.sha256,
            entrypoint="main",
        ),
        environment=SandboxEnvironment(
            runner_id="isolated-runner",
            isolation_kind="microvm",
            image_digest=IMAGE_DIGEST,
            dependency_lock_hash=LOCK_HASH,
        ),
        limits=SandboxResourceLimits(output_bytes=1024),
        expected_output_schema={"type": "object"},
        created_by="investigator",
    )
    return investigation, artifacts, service, spec


def _operation(status: str = "queued", *, token: str = "fencing-token-0001"):
    return SandboxOperationRef(
        job_id="analysis-1",
        runner_id="isolated-runner",
        operation_id="sandbox-op-1",
        fencing_token=token,
        execution_status=status,
    )


def test_sandbox_control_plane_records_provenance_without_local_execution(repository, settings):
    investigation, artifacts, service, spec = _setup(repository, settings)

    prepared, created = service.prepare(spec)
    replayed, replay_created = service.prepare(spec)
    assert created and not replay_created
    assert replayed.spec_hash == prepared.spec_hash
    assert (
        prepared.spec.program.code_sha256
        == hashlib.sha256(artifacts.read_bytes(prepared.spec.program.code_artifact_ref)).hexdigest()
    )

    submitted = service.bind_operation("analysis-1", _operation())
    running_operation = _operation("running")
    running = service.note_running("analysis-1", running_operation)
    unknown = service.bind_operation("analysis-1", _operation("unknown"))
    recovered = service.bind_operation("analysis-1", running_operation)
    assert submitted.state == "submitted"
    assert running.state == "running"
    assert unknown.state == "unknown"
    assert recovered.state == "running"
    assert repository.pending_cleanup_actions(investigation.investigation_id) == []
    cleanup = repository.pending_cleanup_resources(investigation.investigation_id)
    assert cleanup[0]["resource_kind"] == "sandbox"
    assert cleanup[0]["cleanup_owner"] == "isolated-runner"

    output = artifacts.put_bytes(
        "studio-a",
        "game-a",
        b'{"metric":"frame_ms","p95":42.0,"unit":"ms"}',
    )
    started = datetime.now(timezone.utc)
    result = AnalysisJobResult(
        job_id="analysis-1",
        result_revision=1,
        execution_status="succeeded",
        exit_code=0,
        outputs=[
            AnalysisOutputItem(
                output_id="metrics",
                artifact_ref=output.ref,
                kind="analysis_json",
                mime_type="application/json",
                sha256=output.sha256,
                size_bytes=output.size,
            )
        ],
        isolation_kind="microvm",
        environment_image_digest=IMAGE_DIGEST,
        dependency_lock_hash=LOCK_HASH,
        resource_usage={"cpu_seconds": 1.2, "peak_memory_mb": 80},
        started_at=started,
        finished_at=started + timedelta(seconds=2),
    )
    snapshot, evidence = service.record_result(running_operation, result)
    replayed_snapshot, replayed_evidence = service.record_result(running_operation, result)

    assert snapshot.ingestion_state == "ingested"
    assert replayed_snapshot.result_hash == snapshot.result_hash
    assert [item.evidence_id for item in replayed_evidence] == [evidence[0].evidence_id]
    assert len(evidence) == 1
    assert evidence[0].source_type == "analysis"
    assert evidence[0].analysis_job_id == "analysis-1"
    assert evidence[0].derived_from == ["ev-code", "ev-log"]
    assert evidence[0].quality == "untrusted"
    assert "sandbox_generated_unverified" in evidence[0].limitations

    with pytest.raises(ValueError, match="exactly match"):
        service.propose_fact(
            "analysis-1",
            output_evidence_id=evidence[0].evidence_id,
            statement="篡改后的候选",
            data={"p95": 1},
        )
    candidate, candidate_created = service.propose_fact(
        "analysis-1",
        output_evidence_id=evidence[0].evidence_id,
        statement="分析输出中的 p95 为 42",
        data={"metric": "frame_ms", "p95": 42.0, "unit": "ms"},
    )
    replayed_candidate, replayed_created = service.propose_fact(
        "analysis-1",
        output_evidence_id=evidence[0].evidence_id,
        statement="分析输出中的 p95 为 42",
        data={"metric": "frame_ms", "p95": 42.0, "unit": "ms"},
    )
    assert candidate_created and candidate.status == "candidate"
    assert not replayed_created and replayed_candidate.candidate_id == candidate.candidate_id
    assert repository.list_facts(investigation.investigation_id) == []
    verified = service.verify_fact(
        candidate.candidate_id,
        accepted=True,
        verifier="analysis-json-schema-v1",
        reason="格式、数值类型和证据范围复核通过",
    )
    assert verified.status == "verified"
    assert repository.list_facts(investigation.investigation_id)[0].evidence_ids == [
        evidence[0].evidence_id
    ]

    service.confirm_cleanup("analysis-1")
    assert repository.pending_cleanup_resources(investigation.investigation_id) == []


def test_verified_fact_and_candidate_commit_atomically(repository, settings, monkeypatch):
    investigation, _artifacts, service, spec = _setup(repository, settings)
    service.prepare(spec)
    candidate = DerivedFactCandidate(
        candidate_id="atomic-candidate",
        revision=1,
        investigation_id=investigation.investigation_id,
        analysis_job_id=spec.job_id,
        statement="提取后的统计值",
        evidence_ids=["ev-log"],
    )
    repository.save_derived_fact_candidate(candidate, expected_previous_revision=0)
    with pytest.raises(ValueError, match="bounded reason"):
        service.verify_fact(
            candidate.candidate_id,
            accepted=True,
            verifier="reviewer",
            reason=" ",
        )
    with pytest.raises(ValueError, match="bounded reason"):
        service.verify_fact(
            candidate.candidate_id,
            accepted=True,
            verifier="reviewer",
            reason="x" * 4_001,
        )
    with pytest.raises(ValueError, match="atomic fact promotion"):
        repository.save_derived_fact_candidate(
            candidate.model_copy(
                update={
                    "revision": 2,
                    "status": "verified",
                    "verifier": "reviewer",
                    "verification_reason": "checked",
                }
            ),
            expected_previous_revision=1,
        )
    original_insert_event = repository._insert_event

    def fail_fact_event(connection, investigation_id, event_type, payload):
        if event_type == "fact.extracted":
            raise RuntimeError("injected event write failure")
        return original_insert_event(connection, investigation_id, event_type, payload)

    monkeypatch.setattr(repository, "_insert_event", fail_fact_event)
    with pytest.raises(RuntimeError, match="injected"):
        service.verify_fact(
            candidate.candidate_id,
            accepted=True,
            verifier="reviewer",
            reason="checked",
        )
    assert repository.get_derived_fact_candidate(candidate.candidate_id).status == "candidate"
    assert repository.list_facts(investigation.investigation_id) == []
    monkeypatch.setattr(repository, "_insert_event", original_insert_event)
    verified = service.verify_fact(
        candidate.candidate_id,
        accepted=True,
        verifier="reviewer",
        reason="checked",
    )
    assert verified.status == "verified"
    assert repository.list_facts(investigation.investigation_id) == [
        Fact(
            fact_id="fact-atomic-candidate",
            investigation_id=investigation.investigation_id,
            statement=candidate.statement,
            evidence_ids=candidate.evidence_ids,
            extractor="verified-sandbox-analysis",
            extractor_version="reviewer",
        )
    ]


def test_verifier_identity_cannot_be_supplied_in_verification_body():
    with pytest.raises(ValidationError, match="verifier"):
        DerivedFactVerificationRequest.model_validate(
            {"accepted": True, "reason": "checked", "verifier": "spoofed"}
        )


def test_sandbox_policy_is_fail_closed_for_scope_images_hashes_and_limits(repository, settings):
    _investigation, _artifacts, service, spec = _setup(repository, settings)

    with pytest.raises(PermissionError, match="image digest"):
        service.prepare(
            spec.model_copy(
                update={
                    "job_id": "bad-image",
                    "idempotency_key": "bad-image",
                    "environment": spec.environment.model_copy(
                        update={"image_digest": "sha256:" + "c" * 64}
                    ),
                }
            )
        )
    with pytest.raises(ValueError, match="memory_mb"):
        service.prepare(
            spec.model_copy(
                update={
                    "job_id": "too-large",
                    "idempotency_key": "too-large",
                    "limits": spec.limits.model_copy(update={"memory_mb": 2048}),
                }
            )
        )
    with pytest.raises(ValueError, match="program does not match"):
        service.prepare(
            spec.model_copy(
                update={
                    "job_id": "bad-hash",
                    "idempotency_key": "bad-hash",
                    "program": spec.program.model_copy(update={"code_sha256": "d" * 64}),
                }
            )
        )
    with pytest.raises(PermissionError, match="runner"):
        service.prepare(
            spec.model_copy(
                update={
                    "job_id": "bad-runner",
                    "idempotency_key": "bad-runner",
                    "environment": spec.environment.model_copy(
                        update={"runner_id": "untrusted-runner"}
                    ),
                }
            )
        )
    with pytest.raises(ValueError, match="credentials"):
        service.prepare(
            spec.model_copy(
                update={
                    "job_id": "credential-parameter",
                    "idempotency_key": "credential-parameter",
                    "program": spec.program.model_copy(
                        update={"parameters": {"nested": {"api_key": "must-not-pass"}}}
                    ),
                }
            )
        )
    with pytest.raises(PermissionError, match="scope"):
        service.prepare(
            spec.model_copy(
                update={
                    "job_id": "wrong-scope",
                    "idempotency_key": "wrong-scope",
                    "project_id": "other-game",
                }
            )
        )


def test_sandbox_rejects_stale_fencing_token_and_oversized_output(repository, settings):
    _investigation, artifacts, service, spec = _setup(repository, settings)
    service.prepare(spec)
    service.bind_operation("analysis-1", _operation())
    running = _operation("running")
    service.note_running("analysis-1", running)

    started = datetime.now(timezone.utc)
    empty_result = AnalysisJobResult(
        job_id="analysis-1",
        result_revision=1,
        execution_status="failed",
        exit_code=1,
        isolation_kind="microvm",
        environment_image_digest=IMAGE_DIGEST,
        dependency_lock_hash=LOCK_HASH,
        started_at=started,
        finished_at=started + timedelta(seconds=1),
    )
    with pytest.raises(ConflictError, match="fencing token"):
        service.record_result(_operation("running", token="stale-token-00002"), empty_result)
    with pytest.raises(ConflictError, match="attestation"):
        service.record_result(
            running,
            empty_result.model_copy(update={"environment_image_digest": "sha256:" + "c" * 64}),
        )

    too_large = artifacts.put_bytes("studio-a", "game-a", b"x" * 1025)
    oversized_result = AnalysisJobResult(
        job_id="analysis-1",
        result_revision=1,
        execution_status="succeeded",
        exit_code=0,
        outputs=[
            AnalysisOutputItem(
                output_id="too-large",
                artifact_ref=too_large.ref,
                kind="blob",
                mime_type="application/octet-stream",
                sha256=too_large.sha256,
                size_bytes=too_large.size,
            )
        ],
        isolation_kind="microvm",
        environment_image_digest=IMAGE_DIGEST,
        dependency_lock_hash=LOCK_HASH,
        started_at=started,
        finished_at=started + timedelta(seconds=1),
    )
    with pytest.raises(ValueError, match="reserved limit"):
        service.record_result(running, oversized_result)
    with pytest.raises(ConflictError, match="revisions must be consecutive"):
        service.record_result(
            running,
            empty_result.model_copy(update={"result_revision": 2}),
        )


def test_analysis_job_budget_is_deterministic(repository, settings):
    investigation, _artifacts, service, spec = _setup(repository, settings)
    service.prepare(spec)
    second = spec.model_copy(
        update={"job_id": "analysis-2", "idempotency_key": "analysis/idempotency-2"}
    )
    service.prepare(second)
    third = spec.model_copy(
        update={"job_id": "analysis-3", "idempotency_key": "analysis/idempotency-3"}
    )
    with pytest.raises(ConflictError, match="budget"):
        service.prepare(third)
    assert len(repository.list_analysis_jobs(investigation.investigation_id)) == 2


def test_analysis_api_is_scope_gated_and_exposes_cleanup_state(settings):
    async def scenario() -> None:
        configured = replace(
            settings,
            sandbox_image_digests=(IMAGE_DIGEST,),
            sandbox_runner_ids=("isolated-runner",),
        )
        app = create_app(configured)
        investigation, _artifacts, _service, spec = _setup(
            app.state.container.repository,
            configured,
        )
        transport = httpx.ASGITransport(app=app)
        auth = {
            "X-Principal-ID": "sandbox-client",
            "X-Tenant-ID": "studio-a",
            "X-Project-ID": "game-a",
        }
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                forbidden = await client.post(
                    "/v1/analysis/jobs",
                    headers=auth,
                    json=spec.model_dump(mode="json"),
                )
                assert forbidden.status_code == 403
                disallowed_image = await client.post(
                    "/v1/analysis/jobs",
                    headers={**auth, "X-Scopes": "agent:analysis:submit"},
                    json=spec.model_copy(
                        update={
                            "job_id": "api-bad-image",
                            "idempotency_key": "api-bad-image",
                            "environment": spec.environment.model_copy(
                                update={"image_digest": "sha256:" + "c" * 64}
                            ),
                        }
                    ).model_dump(mode="json"),
                )
                assert disallowed_image.status_code == 403
                prepared = await client.post(
                    "/v1/analysis/jobs",
                    headers={**auth, "X-Scopes": "agent:analysis:submit"},
                    json=spec.model_dump(mode="json"),
                )
                assert prepared.status_code == 201
                assert prepared.json()["spec"]["created_by"] == "sandbox-client"
                bound = await client.post(
                    "/v1/analysis/jobs/analysis-1/operations",
                    headers={**auth, "X-Scopes": "agent:analysis:runner"},
                    json=_operation().model_dump(mode="json"),
                )
                assert bound.status_code == 200
                assert "fencing_token" not in bound.json()["operation_ref"]
                detail = await client.get(
                    f"/v1/investigations/{investigation.investigation_id}",
                    headers=auth,
                )
                assert detail.status_code == 200
                assert detail.json()["pending_cleanup_action_ids"] == []
                assert detail.json()["pending_cleanup_resources"][0]["resource_kind"] == "sandbox"
                premature = await client.post(
                    "/v1/analysis/jobs/analysis-1/cleanup",
                    headers={**auth, "X-Scopes": "agent:analysis:runner"},
                )
                assert premature.status_code == 409

    asyncio.run(scenario())


def test_analysis_ledger_change_invalidates_a_frozen_model_decision(repository, settings):
    investigation, _artifacts, service, spec = _setup(repository, settings)
    context = ContextBuilder(repository, CapabilityRegistry(repository)).build(
        investigation.investigation_id,
        scopes=set(),
    )
    kernel = InvestigationKernel(repository)
    preparation = kernel.prepare_round(
        context,
        plan_revision=1,
        model_name="disabled-model",
        prompt_version="test",
    )
    assert preparation.snapshot is not None

    service.prepare(spec)

    with pytest.raises(ConflictError, match="authoritative investigation state changed"):
        kernel.accept_decision(
            preparation.snapshot,
            NextStep(
                kind="report",
                objective_id="analyze",
                expected_observation="发布当前结论",
                reason_summary="stale decision",
            ),
        )
