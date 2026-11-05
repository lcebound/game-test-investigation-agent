from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class InvestigationStatus(StrEnum):
    ACCEPTED = "accepted"
    DISCOVERING = "discovering"
    PLANNING = "planning"
    ACTING = "acting"
    WAITING_EXTERNAL = "waiting_external"
    WAITING_INPUT = "waiting_input"
    ANALYZING = "analyzing"
    EVALUATING = "evaluating"
    REPORTING = "reporting"
    CANCELING = "canceling"
    COMPLETED = "completed"
    CANCELED = "canceled"
    FAILED = "failed"


TERMINAL_STATUSES = {
    InvestigationStatus.COMPLETED,
    InvestigationStatus.CANCELED,
    InvestigationStatus.FAILED,
}


class Resolution(StrEnum):
    DIAGNOSED = "diagnosed"
    INCONCLUSIVE = "inconclusive"
    NOT_REPRODUCED = "not_reproduced"
    GOAL_MET = "goal_met"
    GOAL_FAILED = "goal_failed"
    CANCELED = "canceled"
    SYSTEM_ERROR = "system_error"


class ConclusionLevel(StrEnum):
    UNKNOWN = "unknown"
    OBSERVATION = "observation"
    REPRODUCED = "reproduced"
    BOTTLENECK_SUPPORTED = "bottleneck_supported"
    ROOT_CAUSE_CONFIRMED = "root_cause_confirmed"


class ProjectRef(StrictModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    provider_id: str | None = Field(default=None, max_length=128)


class BuildRef(StrictModel):
    id: str = Field(min_length=1, max_length=256)
    revision: str | None = Field(default=None, max_length=256)
    configuration: dict[str, str] = Field(default_factory=dict)


class TestObjective(StrictModel):
    objective_id: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=4000)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=50)
    priority: Literal["low", "normal", "high", "critical"] = "normal"


class Budget(StrictModel):
    max_rounds: int = Field(default=4, ge=1, le=20)
    max_real_runs: int = Field(default=3, ge=0, le=50)
    max_tool_reads: int = Field(default=40, ge=0, le=1000)
    max_device_minutes: float = Field(default=120, ge=0, le=10000)
    max_wall_seconds: int = Field(default=7200, ge=1, le=604800)


def validate_provider_allowlist(constraints: dict[str, Any]) -> None:
    providers = constraints.get("provider_allowlist")
    if providers is not None and (
        not isinstance(providers, list)
        or not providers
        or not all(isinstance(item, str) and item for item in providers)
        or len(set(providers)) != len(providers)
    ):
        raise ValueError("provider_allowlist must be a nonempty list of unique provider IDs")


class InvestigationRequest(StrictModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    objective: TestObjective
    build_ref: BuildRef | None = None
    constraints: dict[str, Any] = Field(default_factory=dict)
    budget: Budget = Field(default_factory=Budget)
    source: Literal["user", "event"] = "user"
    root_event_id: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def validate_provider_allowlist(self) -> "InvestigationRequest":
        validate_provider_allowlist(self.constraints)
        return self


class InvestigationView(StrictModel):
    investigation_id: str
    workflow_id: str
    status: InvestigationStatus
    resolution: Resolution | None = None
    revision: int
    transition_seq: int
    request: InvestigationRequest
    created_at: datetime
    updated_at: datetime
    stop_reason: str | None = None


class InputCommand(StrictModel):
    command_id: str = Field(min_length=1, max_length=256)
    expected_revision: int = Field(ge=1)
    content: str = Field(min_length=1, max_length=20000)


class ReopenRequest(StrictModel):
    reason: str = Field(min_length=1, max_length=2000)
    budget: Budget
    objective: TestObjective | None = None
    build_ref: BuildRef | None = None
    constraints: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_provider_allowlist(self) -> "ReopenRequest":
        validate_provider_allowlist(self.constraints)
        return self


class CapabilityDescriptor(StrictModel):
    schema_version: Literal["1.0", "2.0"] = "2.0"
    capability_id: str
    capability_version: str
    provider_id: str
    provider_revision: str
    tool_name: str
    project_ids: list[str]
    supported_engines: list[str] = Field(default_factory=list)
    target_kinds: list[str] = Field(default_factory=list)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] = Field(default_factory=dict)
    availability: Literal["ready", "degraded", "unavailable"] = "ready"
    preconditions: list[str] = Field(default_factory=list)
    effects: list[str] = Field(default_factory=list)
    risk_level: Literal["read_only", "bounded_execution", "high_impact"]
    required_scopes: list[str] = Field(default_factory=list)
    execution_owner: str
    resource_domain: str
    idempotency: Literal["required", "supported", "none"] = "required"
    async_execution: bool = Field(default=True, alias="async")
    supports_action_lookup: bool = True
    evidence_types: list[str] = Field(default_factory=list)
    controllable_variables: list[str] = Field(default_factory=list)
    estimated_cost: dict[str, float] = Field(default_factory=dict)
    operation_kind: Literal["query_run", "retrieve_evidence", "experiment", "analyze"] = (
        "experiment"
    )
    creates_run: bool = True
    requires_existing_run: bool = False
    resource_mutation: Literal["none", "bounded", "high_impact"] = "bounded"
    cost_category: Literal["external_read", "execution", "analysis"] = "execution"

    @model_validator(mode="after")
    def operation_semantics_are_consistent(self) -> "CapabilityDescriptor":
        if self.creates_run and self.operation_kind != "experiment":
            raise ValueError("only experiment capabilities may create runs")
        if self.requires_existing_run and self.operation_kind != "query_run":
            raise ValueError("requires_existing_run is only valid for query_run")
        if self.operation_kind == "query_run" and not self.requires_existing_run:
            raise ValueError("query_run capabilities must require an existing run")
        return self


class TargetSelector(StrictModel):
    resource_pool: str | None = None
    environment_fingerprint: str | None = None
    target_kind: str | None = None


class TestRef(StrictModel):
    test_id: str
    version: str | None = None


class ActionSpec(StrictModel):
    schema_version: Literal["1.0", "2.0"] = "2.0"
    tenant_id: str
    project_id: str
    investigation_id: str
    action_id: str
    idempotency_key: str
    capability_id: str
    capability_version: str
    provider_id: str
    provider_revision: str | None = None
    test_ref: TestRef | None = None
    build_ref: BuildRef | None = None
    parameters: dict[str, Any]
    target_selector: TargetSelector = Field(default_factory=TargetSelector)
    constraints: dict[str, Any] = Field(default_factory=dict)
    expected_observation: str
    plan_revision: int = Field(ge=1)
    plan_step_id: str | None = None
    plan_node_revision: int | None = Field(default=None, ge=1)
    repeat_index: int | None = Field(default=None, ge=1)
    operation_kind: Literal["experiment"] = "experiment"
    experiment_id: str | None = None
    run_sequence: int | None = Field(default=None, ge=1)


class ActionPermit(StrictModel):
    permit_id: str
    action_id: str
    principal_id: str
    request_hash: str
    expires_at: datetime
    reserved_device_minutes: float = Field(ge=0)
    admission_ticket_id: str | None = None
    resource_domain: str | None = None
    fencing_token: int | None = Field(default=None, ge=1)
    plan_step_id: str | None = None
    plan_node_revision: int | None = Field(default=None, ge=1)


class RunRef(StrictModel):
    provider_id: str
    project_id: str
    run_id: str


class ActionState(StrEnum):
    PROPOSED = "proposed"
    RESERVED = "reserved"
    DISPATCHING = "dispatching"
    ACCEPTED = "accepted"
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"
    UNKNOWN = "unknown"


class OperationRef(StrictModel):
    accepted: bool
    action_id: str
    run_ref: RunRef | None = None
    operation_id: str | None = None
    execution_status: Literal["queued", "running", "succeeded", "failed", "canceled", "unknown"]
    suggested_poll_interval_seconds: float = Field(default=5, ge=0.1, le=3600)


class EvidenceManifestItem(StrictModel):
    evidence_id: str
    artifact_ref: str
    kind: str
    mime_type: str
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    collected_at: datetime | None = None
    locator: dict[str, str]
    quality: Literal["verified", "partial", "untrusted", "simulated"]
    producer: str
    producer_version: str
    limitations: list[str] = Field(default_factory=list)


class RunResult(StrictModel):
    schema_version: Literal["1.0", "2.0"] = "2.0"
    run_ref: RunRef
    revision: int = Field(ge=1)
    execution_status: Literal["queued", "running", "succeeded", "failed", "canceled", "unknown"]
    test_verdict: Literal["passed", "failed", "not_run", "unknown"]
    execution_source: Literal["real", "simulated"]
    build_ref: BuildRef | None = None
    environment_fingerprint: str | None = None
    provider_status: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    environment: dict[str, Any] = Field(default_factory=dict)
    findings: list[dict[str, Any]] = Field(default_factory=list)
    evidence_manifest: list[EvidenceManifestItem] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)


class RunResultSnapshot(StrictModel):
    schema_version: Literal["2.0"] = "2.0"
    tenant_id: str
    action_id: str
    investigation_id: str
    result: RunResult
    result_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    ingestion_state: Literal["pending", "ingested", "failed"] = "pending"
    ingestion_error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class EvidenceLocator(StrictModel):
    kind: Literal["line_range", "sample_window", "time_range", "video_timecode", "region"]
    value: str


class EvidenceRecord(StrictModel):
    schema_version: Literal["1.0", "2.0"] = "2.0"
    evidence_id: str
    tenant_id: str
    project_id: str
    run_ref: RunRef | None = None
    build_ref: BuildRef | None = None
    environment_fingerprint: str | None = None
    execution_source: Literal["real", "simulated", "unknown"] = "unknown"
    artifact_ref: str
    kind: str
    mime_type: str
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    producer: str
    producer_version: str
    collected_at: datetime
    locator: EvidenceLocator
    quality: Literal["verified", "partial", "untrusted", "simulated"]
    retrieval_status: Literal["ready", "pending", "expired", "not_found"] = "ready"
    parser_version: str | None = None
    derived_from: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    source_type: Literal["run", "analysis", "user_upload", "event", "legacy"] = "legacy"
    source_evidence_id: str | None = None
    source_revision: int = Field(default=1, ge=1)
    action_id: str | None = None
    experiment_id: str | None = None
    run_sequence: int | None = Field(default=None, ge=1)
    analysis_job_id: str | None = None

    @model_validator(mode="after")
    def run_observations_have_source_identity(self) -> "EvidenceRecord":
        if self.source_type == "run":
            if self.run_ref is None or not self.source_evidence_id:
                raise ValueError("run observations require run_ref and source_evidence_id")
        if self.source_type == "analysis":
            if not self.analysis_job_id or not self.source_evidence_id or not self.derived_from:
                raise ValueError(
                    "analysis observations require job, source evidence and input provenance"
                )
            if self.run_ref is not None:
                raise ValueError("analysis observations cannot claim a provider run")
        return self


class SandboxResourceLimits(StrictModel):
    cpu_seconds: int = Field(default=30, ge=1, le=3600)
    memory_mb: int = Field(default=512, ge=64, le=32768)
    process_count: int = Field(default=8, ge=1, le=256)
    disk_mb: int = Field(default=256, ge=1, le=10240)
    wall_seconds: int = Field(default=60, ge=1, le=7200)
    output_bytes: int = Field(default=16 * 1024 * 1024, ge=1, le=1024 * 1024 * 1024)


class SandboxEnvironment(StrictModel):
    runner_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    isolation_kind: Literal["microvm", "gvisor", "kata", "wasm", "container", "windows_mxc", "windows_psec"]
    image_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$", exclude_if=lambda v: v is None)
    runtime_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$", exclude_if=lambda v: v is None)
    dependency_lock_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    network_access: Literal["disabled", "approved"] = "disabled"
    credential_policy: Literal["none"] = "none"
    readonly_inputs: Literal[True] = True

    @model_validator(mode="after")
    def isolation_identity(self) -> "SandboxEnvironment":
        if self.isolation_kind in {"windows_mxc", "windows_psec"}:
            if self.runtime_digest is None or self.image_digest is not None:
                raise ValueError("native sandbox requires a runtime digest and no image digest")
        elif self.image_digest is None or self.runtime_digest is not None:
            raise ValueError("image-based sandbox requires an image digest")
        return self


class AnalysisProgram(StrictModel):
    language: Literal["python", "shell", "powershell", "wasm"]
    code_evidence_id: str = Field(min_length=1, max_length=256)
    code_artifact_ref: str = Field(min_length=1, max_length=2_000)
    code_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    entrypoint: str = Field(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9_.:-]+$",
    )
    parameters: dict[str, Any] = Field(default_factory=dict)


class CodeDirectoryPermission(StrictModel):
    path: str = Field(min_length=1, max_length=1000)
    access: Literal["read", "write"]


class CodeExecutionPermissions(StrictModel):
    """A proposal only. The model cannot approve or activate these permissions."""

    directories: list[CodeDirectoryPermission] = Field(default_factory=list, max_length=8)
    network: bool = False
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def nonempty_request(self) -> "CodeExecutionPermissions":
        if not self.directories and not self.network:
            raise ValueError("a permission request must name directories or network access")
        return self


class AnalysisJobSpec(StrictModel):
    schema_version: Literal["2.0"] = "2.0"
    job_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    tenant_id: str
    project_id: str
    investigation_id: str
    objective_id: str
    purpose: str = Field(min_length=1, max_length=4_000)
    input_evidence_ids: list[str] = Field(min_length=1, max_length=100)
    program: AnalysisProgram
    environment: SandboxEnvironment
    limits: SandboxResourceLimits = Field(default_factory=SandboxResourceLimits)
    expected_output_schema: dict[str, Any] = Field(default_factory=dict)
    created_by: str
    permissions: CodeExecutionPermissions | None = Field(default=None, exclude_if=lambda v: v is None)
    permission_approval_id: str | None = Field(default=None, max_length=128, exclude_if=lambda v: v is None)

    @model_validator(mode="after")
    def approval_matches_permissions(self) -> "AnalysisJobSpec":
        if bool(self.permissions) != bool(self.permission_approval_id):
            raise ValueError("additional permissions require a bound approval")
        network = bool(self.permissions and self.permissions.network)
        if (self.environment.network_access == "approved") != network:
            raise ValueError("network policy must match the requested permissions")
        if self.permissions and self.environment.isolation_kind not in {"windows_mxc", "windows_psec"}:
            raise ValueError("permission grants currently require Windows MXC")
        return self

    @field_validator("input_evidence_ids")
    @classmethod
    def unique_analysis_inputs(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("analysis input evidence IDs must be unique")
        return value


class AnalysisJobState(StrEnum):
    PROPOSED = "proposed"
    SUBMITTED = "submitted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"
    UNKNOWN = "unknown"


class SandboxOperationRef(StrictModel):
    job_id: str
    runner_id: str
    operation_id: str
    fencing_token: str = Field(min_length=16, max_length=512)
    execution_status: Literal["queued", "running", "succeeded", "failed", "canceled", "unknown"]


class AnalysisJobRecord(StrictModel):
    schema_version: Literal["2.0"] = "2.0"
    spec: AnalysisJobSpec
    spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: AnalysisJobState
    operation_ref: SandboxOperationRef | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class SandboxOperationView(StrictModel):
    job_id: str
    runner_id: str
    operation_id: str
    execution_status: Literal["queued", "running", "succeeded", "failed", "canceled", "unknown"]


class AnalysisJobView(StrictModel):
    schema_version: Literal["2.0"] = "2.0"
    spec: AnalysisJobSpec
    spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: AnalysisJobState
    operation_ref: SandboxOperationView | None = None
    created_at: datetime
    updated_at: datetime


class AnalysisOutputItem(StrictModel):
    output_id: str = Field(min_length=1, max_length=256)
    artifact_ref: str = Field(min_length=1, max_length=2_000)
    kind: str = Field(min_length=1, max_length=128)
    mime_type: str = Field(min_length=1, max_length=256)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    limitations: list[str] = Field(default_factory=list, max_length=50)


class AnalysisJobResult(StrictModel):
    schema_version: Literal["2.0"] = "2.0"
    job_id: str
    result_revision: int = Field(ge=1)
    execution_status: Literal["succeeded", "failed", "canceled"]
    exit_code: int | None = None
    outputs: list[AnalysisOutputItem] = Field(default_factory=list, max_length=100)
    isolation_kind: Literal["microvm", "gvisor", "kata", "wasm", "container", "windows_mxc", "windows_psec"]
    environment_image_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$", exclude_if=lambda v: v is None)
    environment_runtime_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$", exclude_if=lambda v: v is None)
    dependency_lock_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    network_access: Literal["disabled", "approved"] = "disabled"
    resource_usage: dict[str, int | float] = Field(default_factory=dict)
    failure_kind: str | None = Field(default=None, max_length=256)
    started_at: datetime
    finished_at: datetime

    @model_validator(mode="after")
    def analysis_result_time_is_monotonic(self) -> "AnalysisJobResult":
        if self.isolation_kind in {"windows_mxc", "windows_psec"}:
            if self.environment_runtime_digest is None or self.environment_image_digest is not None:
                raise ValueError("native result requires a runtime digest and no image digest")
        elif self.environment_image_digest is None or self.environment_runtime_digest is not None:
            raise ValueError("image-based result requires an image digest")
        if self.started_at.tzinfo is None or self.finished_at.tzinfo is None:
            raise ValueError("analysis result times must include a timezone")
        if self.finished_at < self.started_at:
            raise ValueError("analysis result cannot finish before it starts")
        if self.execution_status == "succeeded" and self.exit_code != 0:
            raise ValueError("successful analysis requires exit code zero")
        return self


class AnalysisResultSnapshot(StrictModel):
    schema_version: Literal["2.0"] = "2.0"
    investigation_id: str
    result: AnalysisJobResult
    result_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    ingestion_state: Literal["pending", "ingested", "failed"] = "pending"
    ingestion_error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AnalysisResultSubmission(StrictModel):
    schema_version: Literal["2.0"] = "2.0"
    operation: SandboxOperationRef
    result: AnalysisJobResult

    @model_validator(mode="after")
    def operation_and_result_match(self) -> "AnalysisResultSubmission":
        if self.operation.job_id != self.result.job_id:
            raise ValueError("analysis operation and result job IDs must match")
        return self


class DerivedFactCandidate(StrictModel):
    schema_version: Literal["2.0"] = "2.0"
    candidate_id: str
    revision: int = Field(ge=1)
    investigation_id: str
    analysis_job_id: str
    statement: str = Field(min_length=1, max_length=10_000)
    evidence_ids: list[str] = Field(min_length=1)
    data: dict[str, Any] = Field(default_factory=dict)
    status: Literal["candidate", "verified", "rejected"] = "candidate"
    verifier: str | None = None
    verification_reason: str | None = Field(default=None, max_length=4_000)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def terminal_candidate_has_verification(self) -> "DerivedFactCandidate":
        if self.status != "candidate" and (not self.verifier or not self.verification_reason):
            raise ValueError("verified or rejected facts require verifier and reason")
        return self


class ContextAccessScope(StrictModel):
    schema_version: Literal["2.0"] = "2.0"
    tenant_id: str
    project_id: str


class EvidenceSearchFilters(StrictModel):
    schema_version: Literal["2.0"] = "2.0"
    kind: str | None = None
    mime_type: str | None = None
    quality: Literal["verified", "partial", "untrusted", "simulated"] | None = None
    retrieval_status: Literal["ready", "pending", "expired", "not_found"] | None = None
    source_type: Literal["run", "analysis", "user_upload", "event", "legacy"] | None = None
    provider_id: str | None = None
    run_id: str | None = None
    action_id: str | None = None
    build_id: str | None = None
    environment_fingerprint: str | None = None
    collected_after: datetime | None = None
    collected_before: datetime | None = None

    @model_validator(mode="after")
    def time_range_is_valid(self) -> "EvidenceSearchFilters":
        for value in (self.collected_after, self.collected_before):
            if value is not None and value.tzinfo is None:
                raise ValueError("evidence search times must include a timezone")
        if (
            self.collected_after is not None
            and self.collected_before is not None
            and self.collected_after > self.collected_before
        ):
            raise ValueError("collected_after must not exceed collected_before")
        return self


class EvidenceSearchHit(StrictModel):
    schema_version: Literal["2.0"] = "2.0"
    evidence: EvidenceRecord
    matching_fact_ids: list[str] = Field(default_factory=list)
    score: int = Field(ge=0)


class EvidenceExcerpt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["2.0"] = "2.0"
    evidence_id: str
    artifact_ref: str
    mime_type: str
    locator: EvidenceLocator
    text: str
    byte_offset: int = Field(ge=0)
    returned_bytes: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    next_byte_offset: int | None = Field(default=None, ge=0)
    truncated: bool
    source_revision: int = Field(default=1, ge=1)
    source_sha256: str | None = None
    excerpt_sha256: str | None = None


class ReadRequest(StrictModel):
    evidence_id: str = Field(min_length=1)
    purpose: str = Field(min_length=1, max_length=500)
    byte_offset: int = Field(default=0, ge=0)
    max_bytes: int = Field(default=4096, ge=1, le=4096)


class AnalysisRequest(StrictModel):
    recipe_id: Literal["csv_summary_v1", "log_error_count_v1"]
    recipe_version: Literal["1"] = "1"
    input_evidence_ids: list[str] = Field(min_length=1, max_length=4)
    parameters: dict[str, Any] = Field(default_factory=dict)


class CodeExecutionRequest(StrictModel):
    """The model chooses code and can propose, but never approve, extra access."""

    language: Literal["python", "shell", "powershell"]
    code: str = Field(min_length=1, max_length=32_768)
    purpose: str = Field(min_length=1, max_length=1000)
    input_evidence_ids: list[str] = Field(min_length=1, max_length=20)
    repair_of_job_id: str | None = Field(default=None, max_length=128)
    permissions: CodeExecutionPermissions | None = Field(default=None, exclude_if=lambda v: v is None)

    @field_validator("input_evidence_ids")
    @classmethod
    def unique_inputs(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("code input evidence IDs must be unique")
        return value


class IngestOutcome(StrictModel):
    schema_version: Literal["2.0"] = "2.0"
    status: Literal["created", "replayed", "conflict", "rejected"]
    canonical_evidence_id: str | None = None
    reason: str | None = None


class ClaimVerification(StrictModel):
    citation: Literal["not_checked", "verified", "not_applicable"] = "not_checked"
    provenance: Literal["unknown", "provider_declared", "verified"] = "unknown"
    support: Literal["not_checked", "literal_only", "deterministic"] = "not_checked"
    causality: Literal["not_verified", "verified"] = "not_verified"


class EvidenceAssertion(StrictModel):
    """A checkable statement about an exact JSON field, never a causal verdict."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    evidence_id: str = Field(min_length=1, max_length=256)
    pointer: str = Field(max_length=500)
    operation: Literal["value", "count", "min", "max", "mean", "p50", "p95", "p99"] = "value"
    operator: Literal["eq", "ne", "lt", "le", "gt", "ge"] = "eq"
    expected: StrictBool | StrictInt | StrictFloat | StrictStr | None

    @field_validator("pointer")
    @classmethod
    def valid_pointer(cls, value: str) -> str:
        import re
        if (value and not value.startswith("/")) or re.search(r"~(?![01])", value):
            raise ValueError("pointer must be an RFC 6901 JSON Pointer")
        return value

    @field_validator("expected")
    @classmethod
    def finite_expected(cls, value):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("expected must be finite")
        if isinstance(value, str) and len(value) > 1000:
            raise ValueError("expected string exceeds limit")
        return value


class Claim(StrictModel):
    claim_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1)
    basis: Literal[
        "unspecified",
        "fact",
        "run_verdict",
        "evidence_metadata",
        "evidence_excerpt",
        "analysis_result",
        "hypothesis",
    ] = "unspecified"
    scope: str | None = None
    rule_id: str | None = None
    expected_value: str | None = None
    supporting_excerpt: str | None = None
    verification_status: Literal["unverified", "verified"] = "unverified"
    verification_details: ClaimVerification = Field(default_factory=ClaimVerification)
    evidence_ids: list[str]
    fact_ids: list[str] = Field(default_factory=list)
    assessment_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    assertions: list[EvidenceAssertion] = Field(default_factory=list, max_length=8)

    @field_validator("evidence_ids", "fact_ids", "assessment_ids")
    @classmethod
    def unique_references(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("claim references must be unique")
        return value


class Coverage(StrictModel):
    completed_objectives: list[str] = Field(default_factory=list)
    uncovered_objectives: list[str] = Field(default_factory=list)


class Verification(StrictModel):
    reproduced: bool = False
    fix_applied: Literal[False] = False
    fix_verified: Literal[False] = False


class InvestigationReport(StrictModel):
    schema_version: Literal["1.0", "2.0"] = "2.0"
    investigation_id: str
    revision: int = Field(ge=1)
    status: Literal["completed", "canceled", "failed"]
    resolution: Resolution
    summary: str = Field(min_length=1)
    conclusion_level: ConclusionLevel
    claims: list[Claim] = Field(default_factory=list)
    hypothesis_ids: list[str] = Field(default_factory=list)
    coverage: Coverage
    verification: Verification = Field(default_factory=Verification)
    unresolved_questions: list[str] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    stop_reason: str
    generated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def verified_conclusions_need_claims(self) -> "InvestigationReport":
        verified = {
            ConclusionLevel.REPRODUCED,
            ConclusionLevel.BOTTLENECK_SUPPORTED,
            ConclusionLevel.ROOT_CAUSE_CONFIRMED,
        }
        if self.conclusion_level in verified and not self.claims:
            raise ValueError("verified conclusion levels require evidence-backed claims")
        return self


class NextStep(StrictModel):
    schema_version: Literal["2.0"] = "2.0"
    kind: Literal[
        "query",
        "query_run",
        "retrieve_evidence",
        "search_evidence",
        "experiment",
        "analyze",
        "execute_code",
        "report",
        "request_input",
    ]
    objective_id: str
    hypothesis_ids: list[str] = Field(default_factory=list)
    capability_id: str | None = None
    provider_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    expected_observation: str
    reason_summary: str
    target_action_id: str | None = None
    target_run_ref: RunRef | None = None
    query_purpose: str | None = None
    missing_fields: list[str] = Field(default_factory=list)
    read_request: ReadRequest | None = None
    search_query: str | None = Field(default=None, max_length=1000)
    analysis_request: AnalysisRequest | None = None
    code_request: CodeExecutionRequest | None = Field(default=None, exclude_if=lambda v: v is None)
    plan_delta: PlanDelta | None = None
    hypothesis_delta: HypothesisDelta | None = None


class ActionProposal(StrictModel):
    capability_id: str
    capability_version: str
    provider_id: str
    provider_revision: str | None = None
    parameters: dict[str, Any]
    target_selector: TargetSelector = Field(default_factory=TargetSelector)
    constraints: dict[str, Any] = Field(default_factory=dict)
    expected_observation: str
    repeat_index: int | None = Field(default=None, ge=1)
    experiment_id: str | None = None


class PlannedAction(StrictModel):
    schema_version: Literal["2.0"] = "2.0"
    planning_id: str
    investigation_id: str
    plan_revision: int = Field(ge=1)
    decision_id: str
    proposal: ActionProposal
    proposal_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    action: ActionSpec
    action_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_profile_id: str | None = None
    model_profile_version: str | None = None
    raw_output_ref: str | None = None
    normalization_log: list[str] = Field(default_factory=list)
    attempts: list[dict[str, Any]] = Field(default_factory=list)
    usage: dict[str, int | float | str | bool | None] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class ExperimentSpec(StrictModel):
    schema_version: Literal["2.0"] = "2.0"
    experiment_id: str
    investigation_id: str
    objective_id: str
    capability_id: str
    capability_version: str
    provider_id: str
    purpose: str
    normalized_inputs_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    max_runs: int = Field(default=1, ge=1)
    stop_condition: str


class ObjectiveSpec(StrictModel):
    schema_version: Literal["2.0"] = "2.0"
    criterion_id: str
    objective_id: str
    rule_version: str = "metric-threshold-v1"
    metric: str
    statistic: Literal["minimum", "maximum", "mean", "p50", "p95", "p99"]
    comparison: Literal["lt", "le", "eq", "ge", "gt"]
    threshold: float
    unit: str
    min_independent_runs: int = Field(default=1, ge=1)
    max_spread: float | None = Field(default=None, ge=0)
    allowed_execution_sources: list[Literal["real", "simulated"]] = Field(
        default_factory=lambda: ["real"]
    )
    fact_filters: dict[str, StrictBool | StrictInt | StrictFloat | StrictStr | None] = Field(default_factory=dict)

    @field_validator("threshold", "max_spread")
    @classmethod
    def finite_threshold(cls, value):
        if value is not None and not math.isfinite(value):
            raise ValueError("thresholds and spread must be finite")
        return value

    @field_validator("threshold", "max_spread", mode="before")
    @classmethod
    def numeric_policy_values(cls, value):
        if isinstance(value, bool):
            raise ValueError("boolean values are not numeric thresholds")
        return value

    @field_validator("fact_filters")
    @classmethod
    def bounded_fact_filters(cls, value):
        if len(value) > 10 or any(not key or len(key) > 128 for key in value):
            raise ValueError("fact filters require at most 10 bounded data-field names")
        if any(isinstance(v, float) and not math.isfinite(v) for v in value.values()):
            raise ValueError("fact filters must be finite")
        return value


class FieldOrigin(StrictModel):
    source: Literal["user", "project_pack", "platform", "model", "default"]
    reference: str | None = Field(default=None, max_length=2_000)


class ObjectiveCriterionIR(StrictModel):
    criterion_id: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=4_000)
    evaluator: str = Field(min_length=1, max_length=256)
    evaluator_version: str = Field(min_length=1, max_length=128)
    parameters: dict[str, Any] = Field(default_factory=dict)


class EvidenceRequirement(StrictModel):
    requirement_id: str = Field(min_length=1, max_length=128)
    evidence_types: list[str] = Field(default_factory=list)
    min_independent_runs: int = Field(default=1, ge=0, le=100)
    allowed_sources: list[Literal["real", "simulated", "historical_reference"]] = Field(
        default_factory=lambda: ["real"]
    )
    comparability_keys: list[str] = Field(default_factory=list)


class ObjectiveIR(StrictModel):
    """Versioned, reviewable representation between a request and an executable plan."""

    schema_version: Literal["1.0"] = "1.0"
    objective_id: str
    goal_type: Literal["verification", "comparison", "diagnosis", "coverage"]
    scope: dict[str, Any] = Field(default_factory=dict)
    criteria: list[ObjectiveCriterionIR] = Field(default_factory=list)
    evidence_requirements: list[EvidenceRequirement] = Field(default_factory=list)
    constraints: dict[str, Any] = Field(default_factory=dict)
    field_origins: dict[str, FieldOrigin] = Field(default_factory=dict)
    open_questions: list[str] = Field(default_factory=list)
    compiler_version: str = "objective-compiler-v1"


class PlanScore(StrictModel):
    gap_coverage: float = 0
    hypothesis_discrimination: float = 0
    cost_penalty: float = 0
    risk_penalty: float = 0

    @property
    def total(self) -> float:
        return (
            self.gap_coverage
            + self.hypothesis_discrimination
            - self.cost_penalty
            - self.risk_penalty
        )


class CriterionAssessment(StrictModel):
    schema_version: Literal["2.0"] = "2.0"
    criterion_id: str
    rule_version: str
    investigation_id: str
    evaluated_state_revision: int = Field(ge=1)
    verdict: Literal["passed", "failed", "insufficient", "not_applicable"]
    scope: Literal["real_target", "simulation", "historical_reference"]
    supporting_fact_ids: list[str] = Field(default_factory=list)
    contradicting_fact_ids: list[str] = Field(default_factory=list)
    missing_requirements: list[str] = Field(default_factory=list)
    comparability_checks: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    observed_values: list[float] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=utc_now)

    @property
    def is_complete(self) -> bool:
        return self.verdict in {"passed", "failed"}


class EventPage(StrictModel):
    events: list[dict[str, Any]]
    next_cursor: int


class MetricObservation(StrictModel):
    metric: str
    value: float
    unit: str
    baseline_value: float | None = None
    baseline_run_id: str | None = None
    sample_count: int | None = Field(default=None, ge=0)
    window_seconds: float | None = Field(default=None, ge=0)
    comparison_status: Literal["comparable", "not_comparable", "unknown"] = "unknown"


class AnomalyData(StrictModel):
    finding_id: str
    incident_key: str
    kind: Literal[
        "test_failure", "crash", "performance_regression", "memory_growth", "infrastructure"
    ]
    severity: Literal["low", "medium", "high", "critical"]
    run_ref: RunRef
    result_revision: int = Field(ge=1)
    execution_source: Literal["real", "simulated"]
    build: BuildRef | None = None
    environment_fingerprint: str | None = None
    observation: MetricObservation | dict[str, Any]
    evidence_refs: list[str] = Field(default_factory=list)
    evidence_pending: bool = False
    requested_policy_profile: str = "bounded-diagnosis"
    rule: dict[str, str] | None = None
    test_ref: TestRef | None = None
    target_selector: TargetSelector | None = None

    @field_validator("kind", mode="before")
    @classmethod
    def normalize_infrastructure_kind(cls, value: Any) -> Any:
        return "infrastructure" if value == "infrastructure_failure" else value

    @field_validator("build", mode="before")
    @classmethod
    def normalize_event_build(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        if set(value) - {"id", "revision", "configuration", "sha256"}:
            return value
        if not value.get("id"):
            # Missing source metadata must stay unknown, never an invented build ID.
            return None
        normalized = dict(value)
        digest = normalized.pop("sha256", None)
        if normalized.get("configuration") is None:
            normalized["configuration"] = {}
        if digest and isinstance(normalized["configuration"], dict):
            normalized["configuration"] = {**normalized["configuration"], "artifact_sha256": digest}
        return normalized


class AnomalyDetectedEvent(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    event_id: str
    event_type: Literal["game_test.anomaly.detected"]
    source: str
    tenant_id: str
    project_id: str
    occurred_at: datetime
    published_at: datetime
    correlation_id: str | None = None
    causation_id: str | None = None
    root_event_id: str
    origin: Literal["platform", "agent", "user"]
    investigation_id: str | None = None
    investigation_depth: int = Field(default=0, ge=0, le=10)
    data: AnomalyData


class EvidenceUpdatedData(StrictModel):
    run_ref: RunRef
    result_revision: int = Field(ge=1)
    evidence_refs: list[str] = Field(default_factory=list)
    evidence_pending: bool = False


class EvidenceUpdatedEvent(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    event_id: str
    event_type: Literal["game_test.evidence.updated"]
    source: str
    tenant_id: str
    project_id: str
    occurred_at: datetime
    published_at: datetime
    correlation_id: str | None = None
    causation_id: str | None = None
    root_event_id: str
    origin: Literal["platform", "agent"]
    investigation_id: str
    investigation_depth: int = Field(default=0, ge=0, le=10)
    data: EvidenceUpdatedData


class InvestigationResultData(StrictModel):
    investigation_id: str
    revision: int = Field(ge=1)
    status: Literal["completed", "canceled", "failed"]
    resolution: Resolution
    root_event_id: str
    run_refs: list[RunRef] = Field(default_factory=list)
    summary: str
    report_ref: str
    report_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class InvestigationResultEvent(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    event_id: str
    event_type: Literal["game_test.investigation.updated", "game_test.investigation.completed"]
    source: str
    tenant_id: str
    project_id: str
    occurred_at: datetime
    published_at: datetime
    correlation_id: str
    causation_id: str
    root_event_id: str
    origin: Literal["agent"] = "agent"
    investigation_id: str
    investigation_depth: int = Field(ge=0, le=10)
    data: InvestigationResultData


class HypothesisStatus(StrEnum):
    PROPOSED = "proposed"
    SUPPORTED = "supported"
    REFUTED = "refuted"
    CONFIRMED = "confirmed"
    UNRESOLVED = "unresolved"


class ObservablePrediction(StrictModel):
    """A checkable expected observation, not a self-reported probability."""

    observation_key: str = Field(min_length=1, max_length=128)
    comparator: Literal["eq", "gt", "lt", "contains"]
    value: str | float
    unit: str | None = None
    capability_id: str | None = None
    absolute_tolerance: float = Field(default=0, ge=0)
    minimum_samples: int = Field(default=3, ge=3)

    @field_validator("value")
    @classmethod
    def finite_numeric_prediction(cls, value: str | float) -> str | float:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("numeric prediction must be finite")
        return value

    @model_validator(mode="after")
    def valid_numeric_tolerance(self) -> "ObservablePrediction":
        if not math.isfinite(self.absolute_tolerance):
            raise ValueError("absolute tolerance must be finite")
        if self.absolute_tolerance and (
            self.comparator == "contains" or isinstance(self.value, str)
        ):
            raise ValueError("absolute tolerance requires a numeric prediction")
        return self


class Hypothesis(StrictModel):
    hypothesis_id: str
    investigation_id: str
    revision: int = Field(ge=1)
    statement: str
    scope: str
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    falsifiable_prediction: str
    observable_predictions: list[ObservablePrediction] = Field(default_factory=list)
    next_experiment: str | None = None
    status: HypothesisStatus = HypothesisStatus.PROPOSED


class ExperimentCandidate(StrictModel):
    candidate_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    decision: NextStep
    hypothesis_ids: list[str] = Field(default_factory=list)
    capability_version: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    controlled_variables: list[str] = Field(default_factory=list)
    changed_variables: list[str] = Field(default_factory=list)
    predictions: dict[str, list[ObservablePrediction]] = Field(default_factory=dict)
    evidence_requirements: list[str] = Field(default_factory=list)
    comparability_fields: list[str] = Field(default_factory=list)
    estimated_tool_reads: int = Field(default=0, ge=0)
    estimated_real_runs: int = Field(default=0, ge=0)
    estimated_device_minutes: float = Field(default=0, ge=0)
    stop_rule: str = Field(min_length=1)
    memory_id: str | None = None


class CandidateDraft(StrictModel):
    """Bounded model suggestion; executable fields are checked by the selector."""

    candidate_id: str = Field(min_length=1, max_length=128)
    question: str = Field(min_length=1, max_length=500)
    kind: Literal["experiment", "retrieve_evidence", "analyze", "query_run", "search_evidence"]
    hypothesis_ids: list[str] = Field(default_factory=list, max_length=8)
    capability_id: str | None = None
    provider_id: str | None = None
    evidence_id: str | None = None
    recipe_id: Literal["csv_summary_v1", "log_error_count_v1"] | None = None
    target_action_id: str | None = None
    search_query: str | None = Field(default=None, max_length=1000)
    parameters: dict[str, Any] = Field(default_factory=dict)
    predictions: dict[str, list[ObservablePrediction]] = Field(default_factory=dict)
    expected_observation: str = Field(min_length=1, max_length=500)
    reason_summary: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def required_target_is_present(self) -> "CandidateDraft":
        required = {
            "experiment": (self.capability_id, self.provider_id),
            "retrieve_evidence": (self.evidence_id,),
            "analyze": (self.evidence_id, self.recipe_id),
            "query_run": (self.target_action_id,),
            "search_evidence": (self.search_query,),
        }[self.kind]
        if any(not value for value in required):
            raise ValueError("candidate draft lacks its required target")
        return self


class CandidateDesign(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    candidates: list[CandidateDraft] = Field(default_factory=list, max_length=3)

    @field_validator("candidates")
    @classmethod
    def unique_candidate_ids(cls, value: list[CandidateDraft]) -> list[CandidateDraft]:
        if len({item.candidate_id for item in value}) != len(value):
            raise ValueError("candidate design IDs must be unique")
        return value


class CandidateAssessment(StrictModel):
    candidate_id: str
    executable: bool
    reasons: list[str] = Field(default_factory=list)
    fills_evidence_gap: bool = False
    discriminated_pairs: int = Field(default=0, ge=0)


class SelectionRecord(StrictModel):
    investigation_id: str
    plan_revision: int = Field(ge=1)
    selected_candidate_id: str | None = None
    candidates: list[ExperimentCandidate] = Field(default_factory=list, max_length=3)
    assessments: list[CandidateAssessment]
    selector_version: str


class HypothesisOutcome(StrictModel):
    hypothesis_id: str
    result: Literal["supports", "weakens", "unknown"]
    observation_keys: list[str] = Field(default_factory=list)


class CandidateOutcomeRecord(StrictModel):
    investigation_id: str
    plan_revision: int = Field(ge=1)
    candidate_id: str
    evidence_id: str | None = None
    observation_ref: str | None = None
    action_id: str | None = None
    comparison_key: str | None = None
    observed_values: dict[str, str | None] = Field(default_factory=dict)
    compared_evidence_ids: list[str] = Field(default_factory=list)
    outcomes: list[HypothesisOutcome]
    evaluator_version: str
    limitations: list[str] = Field(default_factory=list)


class EvidenceRelation(StrictModel):
    source_evidence_id: str
    target_id: str
    relation_type: Literal["supports", "refutes", "derived_from", "compares_to", "reproduces"]


class Fact(StrictModel):
    fact_id: str
    investigation_id: str
    statement: str
    evidence_ids: list[str] = Field(min_length=1)
    extractor: str
    extractor_version: str
    data: dict[str, Any] = Field(default_factory=dict)


class ReasoningStep(StrictModel):
    investigation_id: str
    plan_revision: int = Field(ge=1)
    reasoning_step_id: str
    model_name: str
    prompt_version: str
    context_hash: str
    output: NextStep
    usage: dict[str, int | float | str | bool | None] = Field(default_factory=dict)
    model_profile_id: str | None = None
    model_profile_version: str | None = None
    raw_output_ref: str | None = None
    normalization_log: list[str] = Field(default_factory=list)
    attempts: list[dict[str, Any]] = Field(default_factory=list)


class PlaybookExperimentTemplate(StrictModel):
    template_id: str
    question: str
    capability_operation: Literal["query_run", "retrieve_evidence", "experiment", "analyze"]
    controls: list[str] = Field(default_factory=list)
    changed_variables: list[str] = Field(default_factory=list)
    required_evidence_types: list[str] = Field(default_factory=list)
    outcome_interpretation: dict[str, str] = Field(default_factory=dict)
    estimated_cost_class: Literal["free", "low", "medium", "high"] = "low"
    stop_conditions: list[str] = Field(default_factory=list)
    acceptance_eligible: bool = False


class DiagnosticPlaybook(StrictModel):
    schema_version: Literal["2.0"] = "2.0"
    playbook_id: str
    version: str
    anomaly_kind: str
    applicable_when: list[str] = Field(default_factory=list)
    initial_checks: list[str] = Field(default_factory=list)
    required_fields: list[str] = Field(default_factory=list)
    valuable_investigations: list[str] = Field(default_factory=list)
    experiment_templates: list[PlaybookExperimentTemplate] = Field(default_factory=list)
    forbidden_conclusions: list[str] = Field(default_factory=list)

    def as_context(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


class WorkingActionSummary(StrictModel):
    action_id: str
    state: ActionState
    capability_id: str
    provider_id: str
    operation_kind: Literal["experiment"] = "experiment"
    expected_observation: str
    run_ref: RunRef | None = None
    execution_status: (
        Literal["queued", "running", "succeeded", "failed", "canceled", "unknown"] | None
    ) = None
    test_verdict: Literal["passed", "failed", "not_run", "unknown"] | None = None
    execution_source: Literal["real", "simulated"] | None = None
    result_revision: int | None = None
    result_ingestion_state: Literal["pending", "ingested", "failed"] | None = None
    missing_fields: list[str] = Field(default_factory=list)


class WorkingAnalysisSummary(StrictModel):
    job_id: str
    state: AnalysisJobState
    purpose: str
    input_evidence_ids: list[str]
    runner_id: str
    operation_id: str | None = None
    result_ingestion_state: Literal["pending", "ingested", "failed"] | None = None
    exit_code: int | None = Field(default=None, exclude_if=lambda v: v is None)
    failure_kind: str | None = Field(default=None, exclude_if=lambda v: v is None)
    output_evidence_ids: list[str] = Field(default_factory=list, exclude_if=lambda v: not v)


class InvestigationWorkingState(StrictModel):
    """Authoritative compact state rebuilt from ledgers, never from model prose."""

    schema_version: Literal["2.0"] = "2.0"
    investigation_id: str
    state_revision: int = Field(ge=1)
    objective: TestObjective
    assessment: CriterionAssessment | None = None
    active_hypotheses: list[Hypothesis] = Field(default_factory=list)
    refuted_hypotheses: list[Hypothesis] = Field(default_factory=list)
    completed_actions: list[WorkingActionSummary] = Field(default_factory=list)
    failed_actions: list[WorkingActionSummary] = Field(default_factory=list)
    in_flight_actions: list[WorkingActionSummary] = Field(default_factory=list)
    analysis_jobs: list[WorkingAnalysisSummary] = Field(default_factory=list)
    recent_candidate_outcomes: list[CandidateOutcomeRecord] = Field(
        default_factory=list, max_length=3
    )
    pending_evidence_action_ids: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    budget_remaining: dict[str, float | int] = Field(default_factory=dict)
    next_candidates: list[
        Literal[
            "query_run",
            "retrieve_evidence",
            "experiment",
            "analyze",
            "execute_code",
            "request_input",
            "report",
        ]
    ] = Field(default_factory=list)


class ContextWindowUsage(StrictModel):
    estimator: Literal["utf8-bytes-conservative-v1"] = "utf8-bytes-conservative-v1"
    window_limit_tokens: int = Field(ge=1)
    reserved_output_tokens: int = Field(ge=0)
    mandatory_tokens: int = Field(ge=0)
    optional_tokens: int = Field(ge=0)
    estimated_input_tokens: int = Field(ge=0)
    over_budget: bool = False
    omitted_records: dict[str, int] = Field(default_factory=dict)


class ContextSummary(StrictModel):
    schema_version: Literal["2.0"] = "2.0"
    summary_id: str
    investigation_id: str
    source_state_revision: int = Field(ge=1)
    source_event_cursor_start: int = Field(ge=0)
    source_event_cursor_end: int = Field(ge=0)
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_evidence_ids: list[str] = Field(default_factory=list)
    source_fact_ids: list[str] = Field(default_factory=list)
    source_hypothesis_revisions: dict[str, int] = Field(default_factory=dict)
    source_action_ids: list[str] = Field(default_factory=list)
    source_analysis_job_ids: list[str] = Field(default_factory=list)
    source_derived_fact_candidate_ids: list[str] = Field(default_factory=list)
    generator_kind: Literal["deterministic", "model"] = "deterministic"
    generator_version: str
    summary_text: str = Field(min_length=1, max_length=100_000)
    summary_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime = Field(default_factory=utc_now)


class CaseMemoryStatus(StrEnum):
    CANDIDATE = "candidate"
    REVIEWED = "reviewed"
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    RETRACTED = "retracted"


class VersionInterval(StrictModel):
    """Opaque, provider-neutral applicability interval.

    Matching uses a deterministic natural-token ordering. Providers should supply
    normalized engine/build revisions; exact allow-lists remain available when an
    ordering is not meaningful (for example source-control hashes).
    """

    min_inclusive: str | None = Field(default=None, max_length=256)
    max_exclusive: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def contains_a_boundary(self) -> "VersionInterval":
        if self.min_inclusive is None and self.max_exclusive is None:
            raise ValueError("version interval requires at least one boundary")
        return self


class CaseApplicability(StrictModel):
    engine_family: str | None = Field(default=None, max_length=128)
    engine_version: VersionInterval | None = None
    build_ids: list[str] = Field(default_factory=list, max_length=100)
    build_revision: VersionInterval | None = None
    environment_fingerprints: list[str] = Field(default_factory=list, max_length=100)


class CaseMemoryContent(StrictModel):
    title: str = Field(min_length=1, max_length=500)
    symptoms: list[str] = Field(min_length=1, max_length=50)
    investigation_path: list[str] = Field(default_factory=list, max_length=100)
    effective_experiments: list[str] = Field(default_factory=list, max_length=50)
    ineffective_experiments: list[str] = Field(default_factory=list, max_length=50)
    confirmed_outcome: str = Field(min_length=1, max_length=10_000)
    limitations: list[str] = Field(default_factory=list, max_length=50)
    tags: list[str] = Field(default_factory=list, max_length=100)


class CaseMemoryDraft(StrictModel):
    schema_version: Literal["2.0"] = "2.0"
    content: CaseMemoryContent
    applicability: CaseApplicability = Field(default_factory=CaseApplicability)
    confidence: Literal["low", "medium", "high"] = "low"


class CaseMemoryRecord(StrictModel):
    schema_version: Literal["2.0"] = "2.0"
    case_id: str
    revision: int = Field(ge=1)
    tenant_id: str
    project_id: str
    status: CaseMemoryStatus
    confidence: Literal["low", "medium", "high"]
    content: CaseMemoryContent
    applicability: CaseApplicability
    source_investigation_id: str
    source_report_revision: int = Field(ge=1)
    source_report_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_evidence_ids: list[str] = Field(default_factory=list)
    created_by: str
    reviewed_by: str | None = None
    review_notes: str | None = Field(default=None, max_length=10_000)
    state_reason: str = Field(min_length=1, max_length=2_000)
    state_changed_by: str
    valid_until: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def reviewed_states_have_a_reviewer(self) -> "CaseMemoryRecord":
        if (
            self.status
            in {
                CaseMemoryStatus.REVIEWED,
                CaseMemoryStatus.ACTIVE,
                CaseMemoryStatus.DEPRECATED,
            }
            and not self.reviewed_by
        ):
            raise ValueError("reviewed case-memory states require reviewed_by")
        if self.valid_until is not None and self.valid_until.tzinfo is None:
            raise ValueError("case memory validity must include a timezone")
        return self


class CaseMemoryTransition(StrictModel):
    expected_revision: int = Field(ge=1)
    target_status: CaseMemoryStatus
    reason: str = Field(min_length=1, max_length=2_000)
    review_notes: str | None = Field(default=None, max_length=10_000)
    confidence: Literal["low", "medium", "high"] | None = None


class HistoricalCaseReference(StrictModel):
    """Read-only hint for planning; never a current-run fact or evidence record."""

    schema_version: Literal["2.0"] = "2.0"
    scope: Literal["historical_reference"] = "historical_reference"
    case_id: str
    revision: int = Field(ge=1)
    title: str
    symptoms: list[str]
    confirmed_outcome: str
    effective_experiments: list[str] = Field(default_factory=list)
    ineffective_experiments: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    applicability: CaseApplicability
    confidence: Literal["low", "medium", "high"]
    source_investigation_id: str
    score: int = Field(ge=0)
    warning: Literal["historical_case_is_not_current_evidence"] = (
        "historical_case_is_not_current_evidence"
    )


class ProjectPack(StrictModel):
    """Reviewed declarative project knowledge; it cannot grant permissions or execute code."""

    schema_version: Literal["1.0"] = "1.0"
    pack_id: str
    version: str
    tenant_id: str
    project_id: str
    terminology: dict[str, str] = Field(default_factory=dict)
    build_source_rules: dict[str, Any] = Field(default_factory=dict)
    metric_definitions: dict[str, dict[str, Any]] = Field(default_factory=dict)
    test_catalog_semantics: dict[str, str] = Field(default_factory=dict)
    diagnostic_playbooks: list[DiagnosticPlaybook] = Field(default_factory=list)
    reviewed_by: str
    valid_until: datetime | None = None

    @model_validator(mode="after")
    def validity_has_timezone(self) -> "ProjectPack":
        if self.valid_until is not None and self.valid_until.tzinfo is None:
            raise ValueError("project pack validity must include a timezone")
        return self


class CapabilitySummary(StrictModel):
    capability_id: str
    capability_version: str
    provider_id: str
    operation_kind: Literal["query_run", "retrieve_evidence", "experiment", "analyze"]
    risk_level: Literal["read_only", "bounded_execution", "high_impact"]
    availability: Literal["ready", "degraded", "unavailable"]
    evidence_types: list[str] = Field(default_factory=list)
    required_scopes: list[str] = Field(default_factory=list)
    estimated_cost: dict[str, float] = Field(default_factory=dict)


class ContextManifestItem(StrictModel):
    object_type: str = Field(min_length=1, max_length=128)
    object_id: str = Field(min_length=1, max_length=512)
    revision: str = Field(min_length=1, max_length=256)
    reason: str = Field(min_length=1, max_length=2_000)
    freshness: Literal["authoritative", "current", "stale", "historical"]
    estimated_tokens: int = Field(default=0, ge=0)


class ContextManifest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    investigation_id: str
    goal_id: str
    step_id: str | None = None
    state_revision: int = Field(ge=1)
    source_cursor: int = Field(default=0, ge=0)
    permission_scope: list[str] = Field(default_factory=list)
    included: list[ContextManifestItem] = Field(default_factory=list)
    omitted: dict[str, int] = Field(default_factory=dict)
    capability_summaries: list[CapabilitySummary] = Field(default_factory=list)
    loaded_capability_ids: list[str] = Field(default_factory=list)
    estimated_tokens: int = Field(default=0, ge=0)
    generated_at: datetime = Field(default_factory=utc_now)


class ContextBundle(StrictModel):
    investigation: InvestigationView
    working_state: InvestigationWorkingState
    plan: TestPlan | None = None
    capabilities: list[CapabilityDescriptor]
    evidence: list[EvidenceRecord]
    excerpts: list[EvidenceExcerpt] = Field(default_factory=list)
    facts: list[Fact]
    hypotheses: list[Hypothesis]
    actions: list[dict[str, Any]]
    user_inputs: list[dict[str, Any]] = Field(default_factory=list)
    budget: dict[str, float | int]
    diagnostic_guidance: dict[str, Any] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)
    context_window: ContextWindowUsage
    summary: ContextSummary | None = None
    historical_cases: list[HistoricalCaseReference] = Field(default_factory=list)
    project_pack: ProjectPack | None = None
    context_manifest: ContextManifest | None = None


class ContextSnapshot(StrictModel):
    schema_version: Literal["2.0"] = "2.0"
    snapshot_id: str
    investigation_id: str
    state_revision: int = Field(ge=1)
    plan_revision: int = Field(ge=1)
    context_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    authoritative_state_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    authorization_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_name: str
    prompt_version: str
    tool_schema_version: str
    context: ContextBundle
    context_manifest: ContextManifest | None = None
    release_unit: ReleaseUnit | None = None
    created_at: datetime = Field(default_factory=utc_now)


class ModelProfile(StrictModel):
    profile_id: str
    profile_version: str
    provider: str
    model_name: str
    api_mode: Literal["responses", "chat_completions", "callable"] = "callable"
    output_mode: Literal["json_schema", "json_object", "typed"] = "typed"
    context_limit_tokens: int = Field(default=32_000, ge=1)
    max_output_tokens: int = Field(default=4_096, ge=1)
    timeout_seconds: float = Field(default=60, gt=0)
    max_attempts: int = Field(default=2, ge=1, le=5)
    max_total_tokens: int | None = Field(default=None, ge=1)
    max_estimated_cost: float | None = Field(default=None, ge=0)
    cost_per_input_token: float = Field(default=0, ge=0)
    cost_per_output_token: float = Field(default=0, ge=0)
    provider_parameters: dict[str, Any] = Field(default_factory=dict)


class ModelUsage(StrictModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    latency_ms: float = Field(default=0, ge=0)
    estimated_cost: float = Field(default=0, ge=0)
    provider_request_id: str | None = None
    provider_model_id: str | None = None
    provider_model_version: str | None = None
    cache_hit: bool = False


class ModelAttempt(StrictModel):
    attempt: int = Field(ge=1)
    status: Literal["valid", "transport_error", "format_error", "schema_error", "budget_error"]
    raw_output_ref: str | None = None
    normalization_log: list[str] = Field(default_factory=list)
    error: str | None = None
    usage: ModelUsage = Field(default_factory=ModelUsage)


class ReleaseUnit(StrictModel):
    """Immutable identity of code/config versions used for one decision."""

    release_id: str = Field(pattern=r"^release-[0-9a-f]{32}$")
    components: dict[str, str] = Field(min_length=1)


class DecisionEnvelope(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    decision_id: str
    investigation_id: str
    state_revision: int = Field(ge=1)
    context_manifest: ContextManifest | None = None
    source_cursor: int = Field(default=0, ge=0)
    release_unit: ReleaseUnit | None = None
    model_profile: str
    model_profile_version: str
    prompt_version: str
    schema_version_used: str
    project_pack_version: str | None = None
    playbook_version: str | None = None
    raw_output_ref: str | None = None
    normalization_log: list[str] = Field(default_factory=list)
    validated_decision: NextStep
    validator_result: Literal["accepted"] = "accepted"
    budget_snapshot: dict[str, float | int] = Field(default_factory=dict)
    chosen_action_id: str | None = None
    observed_outcome_refs: list[str] = Field(default_factory=list)
    termination_basis: str | None = None


class DecisionRecord(StrictModel):
    schema_version: Literal["2.0"] = "2.0"
    decision_id: str
    investigation_id: str
    plan_revision: int = Field(ge=1)
    context_snapshot_id: str
    expected_state_revision: int = Field(ge=1)
    decision: NextStep
    decision_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["validated", "superseded", "executed"] = "validated"
    envelope: DecisionEnvelope | None = None
    created_at: datetime = Field(default_factory=utc_now)


class DecisionOutcomeRecord(StrictModel):
    """Append-only observation of what followed an immutable decision."""

    schema_version: Literal["1.0"] = "1.0"
    decision_id: str
    investigation_id: str
    revision: int = Field(ge=1)
    chosen_action_id: str | None = None
    observed_outcome_refs: list[str] = Field(default_factory=list)
    termination_basis: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class EvaluationResult(StrictModel):
    accepted: bool
    blocking_issues: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    recommended_action: str | None = None


class PlanInputBinding(StrictModel):
    input_name: str = Field(min_length=1)
    source_step_id: str = Field(min_length=1)
    source_output: str = Field(min_length=1)
    expected_type: str = Field(min_length=1)


class PlanOutputContract(StrictModel):
    output_name: str = Field(min_length=1)
    output_type: str = Field(min_length=1)
    required: bool = True


class PlanResourceRequirement(StrictModel):
    resource_domain: str = Field(min_length=1)
    amount: float = Field(default=1, gt=0)
    unit: Literal["slots", "device_minutes", "bytes"] = "slots"
    exclusive: bool = True


class PlanFailureHandling(StrictModel):
    strategy: Literal["stop", "skip", "retry", "replan", "request_input"] = "replan"
    max_attempts: int = Field(default=1, ge=1, le=20)
    fallback_step_id: str | None = None


class PlanStep(StrictModel):
    step_id: str
    objective_id: str
    dependencies: list[str] = Field(default_factory=list)
    action_type: Literal["query", "experiment", "analyze", "evaluate", "report", "request_input"]
    capability_id: str | None = None
    capability_version: str | None = None
    provider_id: str | None = None
    required_inputs: list[str] = Field(default_factory=list)
    input_bindings: list[PlanInputBinding] = Field(default_factory=list)
    outputs: list[PlanOutputContract] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    completion_conditions: list[str] = Field(default_factory=list)
    resource_requirements: list[PlanResourceRequirement] = Field(default_factory=list)
    expected_evidence: list[str] = Field(default_factory=list)
    estimated_device_minutes: float = Field(default=0, ge=0)
    cost_limit_device_minutes: float | None = Field(default=None, ge=0)
    failure_handling: PlanFailureHandling = Field(default_factory=PlanFailureHandling)
    status: Literal["pending", "ready", "running", "completed", "failed", "skipped"] = "pending"


class TestPlan(StrictModel):
    investigation_id: str
    revision: int = Field(ge=1)
    steps: list[PlanStep]


class PlanCandidate(StrictModel):
    candidate_id: str
    plan: TestPlan
    score: PlanScore = Field(default_factory=PlanScore)
    validation_errors: list[str] = Field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return not self.validation_errors


class PlanStepStatusUpdate(StrictModel):
    step_id: str
    expected_status: Literal["pending", "ready", "running"]
    target_status: Literal["ready", "running", "completed", "failed", "skipped"]


class PlanStepReplacement(StrictModel):
    step_id: str
    replacement: PlanStep

    @model_validator(mode="after")
    def identity_is_stable(self) -> "PlanStepReplacement":
        if self.replacement.step_id != self.step_id:
            raise ValueError("replacement must preserve the existing step ID")
        if self.replacement.status not in {"pending", "ready"}:
            raise ValueError("replacement steps must remain unexecuted")
        return self


class PlanDelta(StrictModel):
    schema_version: Literal["2.0"] = "2.0"
    base_revision: int = Field(ge=0)
    added_steps: list[PlanStep] = Field(default_factory=list)
    replacements: list[PlanStepReplacement] = Field(default_factory=list)
    status_updates: list[PlanStepStatusUpdate] = Field(default_factory=list)
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def contains_a_change(self) -> "PlanDelta":
        if not (self.added_steps or self.replacements or self.status_updates):
            raise ValueError("plan delta must contain at least one change")
        return self


class HypothesisDelta(StrictModel):
    schema_version: Literal["2.0"] = "2.0"
    hypotheses: list[Hypothesis] = Field(min_length=1)
    reason: str = Field(min_length=1)

    @field_validator("hypotheses")
    @classmethod
    def hypothesis_ids_are_unique(cls, value: list[Hypothesis]) -> list[Hypothesis]:
        identities = [item.hypothesis_id for item in value]
        if len(identities) != len(set(identities)):
            raise ValueError("hypothesis delta IDs must be unique")
        return value


class PlanStatusSummary(StrictModel):
    revision: int = Field(ge=1)
    total_steps: int = Field(ge=0)
    pending_steps: int = Field(ge=0)
    running_steps: int = Field(ge=0)
    completed_steps: int = Field(ge=0)
    failed_steps: int = Field(ge=0)


class ProvenanceNode(StrictModel):
    node_id: str
    node_type: Literal[
        "run",
        "observation",
        "artifact",
        "fact",
        "hypothesis",
        "assessment",
        "claim",
        "summary",
        "case",
    ]
    revision: str = "1"
    scope: dict[str, str] = Field(default_factory=dict)
    stale: bool = False


class ProjectionCheckpoint(StrictModel):
    investigation_id: str
    projection_name: Literal["objective", "provenance", "summary"]
    processed_cursor: int = Field(ge=0)
    projection_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    updated_at: datetime = Field(default_factory=utc_now)


class DependencyEdge(StrictModel):
    source_id: str
    target_id: str
    edge_kind: Literal["data", "argument"]
    relation: str
    algorithm_version: str | None = None
    parameters_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class InvalidationPlan(StrictModel):
    changed_source_id: str
    affected_node_ids: list[str] = Field(default_factory=list)
    recompute_order: list[str] = Field(default_factory=list)
    cycles: list[list[str]] = Field(default_factory=list)


class ResourceRequest(StrictModel):
    project_id: str
    task_id: str
    node_id: str
    plan_step_id: str | None = None
    plan_node_revision: int | None = Field(default=None, ge=1)
    resource_domain: str
    device_minutes: float = Field(default=0, ge=0)
    priority: int = Field(default=0, ge=-100, le=100)
    exclusive: bool = True
    ttl_seconds: int = Field(default=300, ge=1, le=86_400)


class AdmissionTicket(StrictModel):
    ticket_id: str
    project_id: str
    task_id: str
    node_id: str
    plan_step_id: str | None = None
    plan_node_revision: int | None = Field(default=None, ge=1)
    resource_domain: str
    cost_limit: float = Field(ge=0)
    fencing_token: int = Field(ge=1)
    expires_at: datetime
    canceled: bool = False


class AnalysisRecipeStep(StrictModel):
    step_id: str
    operation: Literal["parse", "filter", "aggregate", "compare", "verify"]
    dependencies: list[str] = Field(default_factory=list)
    component: str
    component_version: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    output_type: str


class AnalysisRecipe(StrictModel):
    recipe_id: str
    version: str
    input_types: list[str] = Field(min_length=1)
    steps: list[AnalysisRecipeStep] = Field(min_length=1)
    image_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    output_schema: dict[str, Any] = Field(default_factory=dict)
    verifier: str


class ExternalRunSummary(StrictModel):
    action_id: str
    action_state: ActionState
    operation_id: str | None = None
    run_ref: RunRef | None = None


class CleanupResourceSummary(StrictModel):
    resource_id: str
    resource_kind: Literal["action", "sandbox", "device_lease", "temporary_account", "directory"]
    cleanup_owner: str
    attempts: int = Field(ge=0)
    last_error: str | None = None
    updated_at: datetime


class InvestigationDetail(InvestigationView):
    plan_summary: PlanStatusSummary | None = None
    budget_usage: dict[str, float | int] = Field(default_factory=dict)
    model_usage: dict[str, float | int] = Field(default_factory=dict)
    evidence_gaps: list[str] = Field(default_factory=list)
    external_runs: list[ExternalRunSummary] = Field(default_factory=list)
    pending_cleanup_action_ids: list[str] = Field(default_factory=list)
    pending_cleanup_resources: list[CleanupResourceSummary] = Field(default_factory=list)
