from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Protocol

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError as JsonSchemaValidationError

from game_test_agent.contracts.models import (
    AnalysisJobRecord,
    AnalysisJobResult,
    AnalysisJobSpec,
    AnalysisJobState,
    AnalysisResultSnapshot,
    DerivedFactCandidate,
    EvidenceLocator,
    EvidenceRecord,
    Fact,
    SandboxOperationRef,
)
from game_test_agent.evidence import EvidenceService
from game_test_agent.storage import (
    ArtifactStore,
    ConflictError,
    Repository,
    canonical_json,
    payload_hash,
)


def _operation_identity(operation: SandboxOperationRef) -> tuple[str, str, str, str]:
    return (
        operation.job_id,
        operation.runner_id,
        operation.operation_id,
        operation.fencing_token,
    )


class SandboxRunner(Protocol):
    """External isolation boundary. Implementations own execution and credentials."""

    async def submit(self, spec: AnalysisJobSpec) -> SandboxOperationRef: ...

    async def query(self, operation: SandboxOperationRef) -> AnalysisJobResult | None: ...

    async def cancel(self, operation: SandboxOperationRef) -> bool: ...


class SandboxPolicy:
    """Fail-closed admission policy for an externally isolated analysis runner."""

    def __init__(
        self,
        *,
        allowed_image_digests: set[str],
        allowed_runner_ids: set[str],
        allowed_runtime_digests: set[str] | None = None,
        max_jobs_per_investigation: int = 4,
        max_cpu_seconds: int = 120,
        max_memory_mb: int = 1024,
        max_process_count: int = 16,
        max_disk_mb: int = 512,
        max_wall_seconds: int = 180,
        max_output_bytes: int = 32 * 1024 * 1024,
        max_program_bytes: int = 1024 * 1024,
    ) -> None:
        if not (allowed_image_digests or allowed_runtime_digests) or not allowed_runner_ids:
            raise ValueError("sandbox policy requires explicit image and runner allowlists")
        if max_jobs_per_investigation < 1 or max_program_bytes < 1:
            raise ValueError("sandbox policy limits must be positive")
        self.allowed_image_digests = frozenset(allowed_image_digests)
        self.allowed_runtime_digests = frozenset(allowed_runtime_digests or ())
        self.allowed_runner_ids = frozenset(allowed_runner_ids)
        self.max_jobs_per_investigation = max_jobs_per_investigation
        self.max_cpu_seconds = max_cpu_seconds
        self.max_memory_mb = max_memory_mb
        self.max_process_count = max_process_count
        self.max_disk_mb = max_disk_mb
        self.max_wall_seconds = max_wall_seconds
        self.max_output_bytes = max_output_bytes
        self.max_program_bytes = max_program_bytes

    def validate(
        self,
        spec: AnalysisJobSpec,
        repository: Repository,
        artifacts: ArtifactStore,
    ) -> None:
        investigation = repository.get_investigation(spec.investigation_id)
        if (spec.tenant_id, spec.project_id) != (
            investigation.request.tenant_id,
            investigation.request.project_id,
        ):
            raise PermissionError("analysis job scope does not match its investigation")
        if spec.objective_id != investigation.request.objective.objective_id:
            raise ValueError("analysis job objective is outside the investigation")
        if (spec.environment.isolation_kind in {"windows_mxc", "windows_psec"}
                and spec.environment.runtime_digest not in self.allowed_runtime_digests):
            raise PermissionError("sandbox runtime digest is not allowlisted")
        if (spec.environment.isolation_kind not in {"windows_mxc", "windows_psec"}
                and spec.environment.image_digest not in self.allowed_image_digests):
            raise PermissionError("sandbox image digest is not allowlisted")
        if spec.environment.runner_id not in self.allowed_runner_ids:
            raise PermissionError("sandbox runner is not allowlisted")
        if spec.permissions:
            from game_test_agent.analysis.permissions import CodePermissionStore
            CodePermissionStore(repository).authorize_spec(spec)
        limits = spec.limits
        configured_limits = {
            "cpu_seconds": self.max_cpu_seconds,
            "memory_mb": self.max_memory_mb,
            "process_count": self.max_process_count,
            "disk_mb": self.max_disk_mb,
            "wall_seconds": self.max_wall_seconds,
            "output_bytes": self.max_output_bytes,
        }
        for field, maximum in configured_limits.items():
            if getattr(limits, field) > maximum:
                raise ValueError(f"sandbox {field} exceeds policy")
        if len(canonical_json(spec.program.parameters).encode("utf-8")) > 64 * 1024:
            raise ValueError("sandbox program parameters exceed 64 KiB")
        if self._contains_sensitive_key(spec.program.parameters):
            raise ValueError("sandbox program parameters cannot contain credentials")
        if len(canonical_json(spec.expected_output_schema).encode("utf-8")) > 64 * 1024:
            raise ValueError("sandbox output schema exceeds 64 KiB")
        try:
            Draft202012Validator.check_schema(spec.expected_output_schema or {})
        except SchemaError as error:
            raise ValueError("sandbox output schema is invalid") from error

        evidence_by_id = {
            item.evidence_id: item
            for item in repository.list_current_evidence(spec.investigation_id)
        }
        missing = set(spec.input_evidence_ids) - set(evidence_by_id)
        if missing:
            raise ValueError("analysis job references unknown input evidence")
        for evidence_id in spec.input_evidence_ids:
            evidence = evidence_by_id[evidence_id]
            if evidence.retrieval_status != "ready":
                raise ValueError("analysis inputs must be locally readable")
            if not artifacts.validate_scope(evidence.artifact_ref, spec.tenant_id, spec.project_id):
                raise PermissionError("analysis input artifact is outside job scope")
            content = artifacts.read_bytes(evidence.artifact_ref)
            if hashlib.sha256(content).hexdigest() != evidence.sha256.lower():
                raise ValueError("analysis input artifact hash mismatch")

        code_evidence = evidence_by_id.get(spec.program.code_evidence_id)
        if code_evidence is None:
            raise ValueError("analysis program must reference authorized code evidence")
        if (
            code_evidence.artifact_ref != spec.program.code_artifact_ref
            or code_evidence.sha256.lower() != spec.program.code_sha256
        ):
            raise ValueError("analysis program does not match its code evidence")
        if not artifacts.validate_scope(
            spec.program.code_artifact_ref, spec.tenant_id, spec.project_id
        ):
            raise PermissionError("analysis program artifact is outside job scope")
        code = artifacts.read_bytes(spec.program.code_artifact_ref)
        if len(code) > self.max_program_bytes:
            raise ValueError("analysis program exceeds policy size")
        if hashlib.sha256(code).hexdigest() != spec.program.code_sha256:
            raise ValueError("analysis program hash mismatch")

    @classmethod
    def _contains_sensitive_key(cls, value) -> bool:
        sensitive = {
            "authorization",
            "api_key",
            "apikey",
            "credential",
            "credentials",
            "password",
            "secret",
            "token",
        }
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = str(key).casefold().replace("-", "_")
                sensitive_name = normalized in sensitive or any(
                    normalized.endswith("_" + term)
                    for term in ("password", "secret", "token", "credential", "api_key")
                )
                if sensitive_name or cls._contains_sensitive_key(item):
                    return True
            return False
        if isinstance(value, (list, tuple)):
            return any(cls._contains_sensitive_key(item) for item in value)
        return False


class AnalysisSandboxService:
    """Control plane only; untrusted code is never executed by this process."""

    def __init__(
        self,
        repository: Repository,
        artifacts: ArtifactStore,
        policy: SandboxPolicy,
    ) -> None:
        self.repository = repository
        self.artifacts = artifacts
        self.policy = policy
        self.evidence = EvidenceService(repository)

    def prepare(self, spec: AnalysisJobSpec) -> tuple[AnalysisJobRecord, bool]:
        self.policy.validate(spec, self.repository, self.artifacts)
        now = datetime.now(timezone.utc)
        record = AnalysisJobRecord(
            spec=spec,
            spec_hash=payload_hash(spec),
            state=AnalysisJobState.PROPOSED,
            created_at=now,
            updated_at=now,
        )
        return self.repository.reserve_analysis_job(
            record,
            max_jobs_per_investigation=self.policy.max_jobs_per_investigation,
        )

    def bind_operation(self, job_id: str, operation: SandboxOperationRef) -> AnalysisJobRecord:
        job = self.repository.get_analysis_job(job_id)
        if operation.job_id != job_id:
            raise ValueError("sandbox operation job mismatch")
        if operation.runner_id != job.spec.environment.runner_id:
            raise ValueError("sandbox operation runner mismatch")
        if job.operation_ref is not None and _operation_identity(
            job.operation_ref
        ) != _operation_identity(operation):
            raise ConflictError("sandbox operation identity or fencing token changed")
        if operation.execution_status in {"succeeded", "failed"}:
            raise ValueError("terminal sandbox operations require a result snapshot")
        updated = job
        if job.state == AnalysisJobState.PROPOSED:
            updated = self.repository.transition_analysis_job(
                job_id,
                AnalysisJobState.SUBMITTED,
                operation_ref=operation,
            )
        target = {
            "queued": AnalysisJobState.SUBMITTED,
            "running": AnalysisJobState.RUNNING,
            "canceled": AnalysisJobState.CANCELED,
            "unknown": AnalysisJobState.UNKNOWN,
        }[operation.execution_status]
        if target != updated.state:
            updated = self.repository.transition_analysis_job(
                job_id,
                target,
                operation_ref=operation,
            )
        self.repository.record_resource_cleanup(
            job.spec.investigation_id,
            job_id,
            resource_kind="sandbox",
            cleanup_owner=operation.runner_id,
        )
        return updated

    def note_running(self, job_id: str, operation: SandboxOperationRef) -> AnalysisJobRecord:
        if operation.execution_status != "running":
            raise ValueError("running analysis update requires running operation status")
        return self.repository.transition_analysis_job(
            job_id,
            AnalysisJobState.RUNNING,
            operation_ref=operation,
        )

    def record_result(
        self,
        operation: SandboxOperationRef,
        result: AnalysisJobResult,
    ) -> tuple[AnalysisResultSnapshot, list[EvidenceRecord]]:
        job = self.repository.get_analysis_job(operation.job_id)
        if job.operation_ref is None or _operation_identity(
            job.operation_ref
        ) != _operation_identity(operation):
            raise ConflictError("sandbox result operation or fencing token does not match")
        if result.job_id != operation.job_id:
            raise ValueError("analysis result job mismatch")
        if (
            result.isolation_kind != job.spec.environment.isolation_kind
            or result.environment_image_digest != job.spec.environment.image_digest
            or result.environment_runtime_digest != job.spec.environment.runtime_digest
            or result.network_access != job.spec.environment.network_access
            or result.dependency_lock_hash != job.spec.environment.dependency_lock_hash
        ):
            raise ConflictError("sandbox result environment attestation does not match the job")
        output_ids = [item.output_id for item in result.outputs]
        if len(output_ids) != len(set(output_ids)):
            raise ValueError("analysis output IDs must be unique")
        total_size = sum(item.size_bytes for item in result.outputs)
        if total_size > job.spec.limits.output_bytes:
            raise ValueError("analysis outputs exceed the reserved limit")
        wall_seconds = (result.finished_at - result.started_at).total_seconds()
        if wall_seconds > job.spec.limits.wall_seconds and not (
            result.isolation_kind in {"windows_mxc", "windows_psec"} and result.execution_status != "succeeded"
        ):
            raise ValueError("analysis wall time exceeds the reserved limit")
        usage_limits = {
            "cpu_seconds": job.spec.limits.cpu_seconds,
            "peak_memory_mb": job.spec.limits.memory_mb,
            "max_processes": job.spec.limits.process_count,
            "written_bytes": job.spec.limits.disk_mb * 1024 * 1024,
        }
        for field, maximum in usage_limits.items():
            value = result.resource_usage.get(field)
            if value is not None:
                if isinstance(value, bool) or not math.isfinite(float(value)):
                    raise ValueError(f"analysis reported invalid {field}")
                if value < 0 or value > maximum:
                    raise ValueError(f"analysis reported {field} outside the reserved limit")

        snapshot = AnalysisResultSnapshot(
            investigation_id=job.spec.investigation_id,
            result=result,
            result_hash=payload_hash(result),
        )
        persisted, _ = self.repository.save_analysis_result(snapshot)
        ingested: list[EvidenceRecord] = []
        try:
            for output in result.outputs:
                if not self.artifacts.validate_scope(
                    output.artifact_ref, job.spec.tenant_id, job.spec.project_id
                ):
                    raise PermissionError("analysis output artifact is outside job scope")
                content = self.artifacts.read_bytes(output.artifact_ref)
                if len(content) != output.size_bytes:
                    raise ValueError("analysis output size mismatch")
                if hashlib.sha256(content).hexdigest() != output.sha256:
                    raise ValueError("analysis output hash mismatch")
                identity = {
                    "job_id": result.job_id,
                    "result_revision": result.result_revision,
                    "output_id": output.output_id,
                    "sha256": output.sha256,
                }
                evidence = EvidenceRecord(
                    evidence_id="ev-analysis-"
                    + hashlib.sha256(canonical_json(identity).encode()).hexdigest()[:32],
                    tenant_id=job.spec.tenant_id,
                    project_id=job.spec.project_id,
                    build_ref=self.repository.get_investigation(
                        job.spec.investigation_id
                    ).request.build_ref,
                    artifact_ref=output.artifact_ref,
                    kind=output.kind,
                    mime_type=output.mime_type,
                    sha256=output.sha256,
                    producer=f"sandbox:{operation.runner_id}",
                    producer_version=job.spec.environment.runtime_digest or job.spec.environment.image_digest,
                    collected_at=result.finished_at,
                    locator=EvidenceLocator(
                        kind="region", value=f"analysis-output:{output.output_id}"
                    ),
                    quality="untrusted",
                    source_type="analysis",
                    source_evidence_id=output.output_id,
                    source_revision=result.result_revision,
                    analysis_job_id=result.job_id,
                    derived_from=list(
                        dict.fromkeys(
                            [job.spec.program.code_evidence_id, *job.spec.input_evidence_ids]
                        )
                    ),
                    limitations=[
                        "sandbox_generated_unverified",
                        *output.limitations,
                    ],
                )
                outcome = self.evidence.ingest(job.spec.investigation_id, evidence)
                if outcome.status not in {"created", "replayed"}:
                    raise ConflictError(outcome.reason or "analysis evidence ingestion failed")
                ingested.append(evidence)
        except Exception as error:
            self.repository.mark_analysis_result_ingestion(
                result.job_id,
                result.result_revision,
                "failed",
                type(error).__name__,
            )
            raise
        self.repository.mark_analysis_result_ingestion(
            result.job_id,
            result.result_revision,
            "ingested",
        )
        return self.repository.get_latest_analysis_result(result.job_id) or persisted, ingested

    def propose_fact(
        self,
        job_id: str,
        *,
        output_evidence_id: str,
        statement: str,
        data: dict,
    ) -> tuple[DerivedFactCandidate, bool]:
        job = self.repository.get_analysis_job(job_id)
        snapshot = self.repository.get_latest_analysis_result(job_id)
        if snapshot is None or snapshot.ingestion_state != "ingested":
            raise ConflictError("analysis outputs must be ingested before proposing a fact")
        evidence = next(
            (
                item
                for item in self.repository.list_current_evidence(job.spec.investigation_id)
                if item.evidence_id == output_evidence_id
            ),
            None,
        )
        if evidence is None or evidence.analysis_job_id != job_id:
            raise ValueError("derived fact must reference an output from the same analysis job")
        if evidence.mime_type not in {"application/json", "application/ld+json"}:
            raise ValueError("derived fact candidates require a structured JSON output")
        try:
            parsed_output = json.loads(self.artifacts.read_bytes(evidence.artifact_ref))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("analysis output is not valid JSON") from error
        try:
            Draft202012Validator(job.spec.expected_output_schema or {}).validate(parsed_output)
        except JsonSchemaValidationError as error:
            raise ValueError("analysis output does not match the declared schema") from error
        if data != parsed_output:
            raise ValueError("derived fact data must exactly match the validated output")
        identity = {
            "job_id": job_id,
            "output_evidence_id": output_evidence_id,
            "statement": statement,
            "data": data,
        }
        candidate = DerivedFactCandidate(
            candidate_id="dfc-"
            + hashlib.sha256(canonical_json(identity).encode()).hexdigest()[:32],
            revision=1,
            investigation_id=job.spec.investigation_id,
            analysis_job_id=job_id,
            statement=statement,
            evidence_ids=[output_evidence_id],
            data=data,
        )
        return self.repository.save_derived_fact_candidate(
            candidate,
            expected_previous_revision=0,
        )

    def verify_fact(
        self,
        candidate_id: str,
        *,
        accepted: bool,
        verifier: str,
        reason: str,
    ) -> DerivedFactCandidate:
        if not verifier.strip() or not reason.strip() or len(reason) > 4_000:
            raise ValueError("fact verification requires a verifier and a bounded reason")
        current = self.repository.get_derived_fact_candidate(candidate_id)
        target = "verified" if accepted else "rejected"
        if current.status != "candidate":
            if (
                current.status == target
                and current.verifier == verifier
                and current.verification_reason == reason
            ):
                return current
            raise ConflictError("derived fact candidate is already terminal")
        updated = DerivedFactCandidate.model_validate(
            {
                **current.model_dump(mode="python"),
                "revision": current.revision + 1,
                "status": target,
                "verifier": verifier,
                "verification_reason": reason,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        promoted_fact = (
            Fact(
                fact_id=f"fact-{candidate_id}",
                investigation_id=current.investigation_id,
                statement=current.statement,
                evidence_ids=current.evidence_ids,
                extractor="verified-sandbox-analysis",
                extractor_version=verifier,
                data=current.data,
            )
            if accepted
            else None
        )
        persisted, _ = self.repository.save_derived_fact_candidate(
            updated,
            expected_previous_revision=current.revision,
            promoted_fact=promoted_fact,
        )
        return persisted

    def confirm_cleanup(self, job_id: str) -> None:
        job = self.repository.get_analysis_job(job_id)
        if job.state not in {
            AnalysisJobState.SUCCEEDED,
            AnalysisJobState.FAILED,
            AnalysisJobState.CANCELED,
        }:
            raise ConflictError("sandbox cleanup cannot complete while execution may be active")
        self.repository.resolve_cleanup(job_id)
