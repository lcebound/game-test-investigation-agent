from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from game_test_agent.contracts.models import CapabilityDescriptor


def test_exported_contract_schemas_are_valid_and_versioned():
    root = Path(__file__).parents[1] / "src" / "game_test_agent" / "contracts" / "v1"
    expected = {
        "InvestigationRequest",
        "AnomalyDetectedEvent",
        "CapabilityDescriptor",
        "ActionSpec",
        "RunResult",
        "EvidenceRecord",
        "EvidenceUpdatedEvent",
        "InvestigationReport",
        "InvestigationResultEvent",
    }
    assert {path.stem for path in root.glob("*.json")} == expected
    for path in root.glob("*.json"):
        schema = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        assert schema["$id"] == f"contract://v1/{path.stem}"


def test_v2_contract_schemas_are_valid_and_versioned():
    root = Path(__file__).parents[1] / "src" / "game_test_agent" / "contracts" / "v2"
    expected = {
        "EvidenceAssertion",
        "CapabilityDescriptor",
        "ActionSpec",
        "RunResult",
        "RunResultSnapshot",
        "EvidenceRecord",
        "IngestOutcome",
        "NextStep",
        "ReadRequest",
        "AnalysisRequest",
        "CodeExecutionRequest",
        "CodeDirectoryPermission",
        "CodeExecutionPermissions",
        "ObservablePrediction",
        "ExperimentCandidate",
        "CandidateAssessment",
        "CandidateDraft",
        "CandidateDesign",
        "SelectionRecord",
        "HypothesisOutcome",
        "CandidateOutcomeRecord",
        "ExperimentSpec",
        "ObjectiveSpec",
        "CriterionAssessment",
        "InvestigationWorkingState",
        "ContextSnapshot",
        "ContextSummary",
        "ContextAccessScope",
        "ContextWindowUsage",
        "EvidenceSearchFilters",
        "EvidenceSearchHit",
        "EvidenceExcerpt",
        "DecisionRecord",
        "DecisionOutcomeRecord",
        "DiagnosticPlaybook",
        "PlaybookExperimentTemplate",
        "PlanDelta",
        "HypothesisDelta",
        "PlannedAction",
        "InvestigationReport",
        "CaseMemoryDraft",
        "CaseMemoryRecord",
        "CaseMemoryTransition",
        "HistoricalCaseReference",
        "SandboxResourceLimits",
        "SandboxEnvironment",
        "AnalysisProgram",
        "AnalysisJobSpec",
        "AnalysisJobView",
        "SandboxOperationRef",
        "SandboxOperationView",
        "AnalysisJobRecord",
        "AnalysisOutputItem",
        "AnalysisJobResult",
        "AnalysisResultSnapshot",
        "AnalysisResultSubmission",
        "DerivedFactCandidate",
    }
    assert {path.stem for path in root.glob("*.json")} == expected
    for path in root.glob("*.json"):
        schema = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        assert schema["$id"] == f"contract://v2/{path.stem}"


def test_runtime_accepts_explicit_v1_capability_during_mixed_version_window():
    descriptor = CapabilityDescriptor.model_validate(
        {
            "schema_version": "1.0",
            "capability_id": "legacy.run",
            "capability_version": "1",
            "provider_id": "legacy-platform-adapter",
            "provider_revision": "1",
            "tool_name": "run",
            "project_ids": ["game-a"],
            "input_schema": {"type": "object"},
            "risk_level": "bounded_execution",
            "execution_owner": "legacy-platform-adapter",
            "resource_domain": "legacy-pool",
        }
    )
    assert descriptor.schema_version == "1.0"
    assert (
        CapabilityDescriptor.model_validate(
            {
                key: value
                for key, value in descriptor.model_dump(mode="json", by_alias=True).items()
                if key != "schema_version"
            }
        ).schema_version
        == "2.0"
    )
