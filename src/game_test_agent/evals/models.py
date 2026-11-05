from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from game_test_agent.contracts.models import (
    ConclusionLevel,
    EvidenceRecord,
    InvestigationReport,
    InvestigationRequest,
    StrictModel,
)


class EvaluationExpectation(StrictModel):
    should_accept: bool
    allowed_conclusion_levels: list[ConclusionLevel] = Field(min_length=1)
    required_evidence_ids: list[str] = Field(default_factory=list)
    required_limitations: list[str] = Field(default_factory=list)
    stop_reason_contains: list[str] = Field(default_factory=list)
    expected_blocking_issue_contains: list[str] = Field(default_factory=list)


class EvaluationCase(StrictModel):
    case_id: str = Field(min_length=1, max_length=128)
    category: Literal[
        "known_failure",
        "performance_regression",
        "infrastructure_anomaly",
        "simulated_result",
        "missing_evidence",
        "conflicting_evidence",
        "stale_evidence",
        "provenance_mismatch",
        "tool_description_injection",
        "log_prompt_injection",
    ]
    split: Literal["train", "validation", "test"] = "test"
    split_group: str = Field(
        min_length=1, description="Project/build grouping used to avoid leakage"
    )
    request: InvestigationRequest
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    candidate_report: InvestigationReport
    expectation: EvaluationExpectation

    @model_validator(mode="after")
    def validate_fixture_scope(self) -> "EvaluationCase":
        for item in self.evidence:
            if (item.tenant_id, item.project_id) != (
                self.request.tenant_id,
                self.request.project_id,
            ):
                raise ValueError("evaluation evidence must remain in the request tenant/project")
        return self


class EvaluationCaseResult(StrictModel):
    case_id: str
    category: str
    passed: bool
    evaluator_accepted: bool
    expected_accepted: bool
    citation_validity_rate: float = Field(ge=0, le=1)
    blocking_issues: list[str] = Field(default_factory=list)
    label_violations: list[str] = Field(default_factory=list)


class EvaluationMetrics(StrictModel):
    total_cases: int = Field(ge=0)
    passed_cases: int = Field(ge=0)
    case_pass_rate: float = Field(ge=0, le=1)
    acceptance_accuracy: float = Field(ge=0, le=1)
    citation_validity_rate: float = Field(ge=0, le=1)
    unsafe_case_rejection_rate: float = Field(ge=0, le=1)


class EvaluationSuiteResult(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    passed: bool
    metrics: EvaluationMetrics
    cases: list[EvaluationCaseResult]
