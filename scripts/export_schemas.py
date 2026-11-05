from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from game_test_agent.contracts.models import (  # noqa: E402
    ActionSpec,
    AnalysisJobRecord,
    AnalysisJobResult,
    AnalysisJobSpec,
    AnalysisJobView,
    AnalysisOutputItem,
    AnalysisProgram,
    AnalysisRequest,
    CodeExecutionRequest,
    CodeDirectoryPermission,
    CodeExecutionPermissions,
    AnalysisResultSnapshot,
    AnalysisResultSubmission,
    AnomalyDetectedEvent,
    CapabilityDescriptor,
    CandidateAssessment,
    CandidateDesign,
    CandidateDraft,
    CandidateOutcomeRecord,
    CaseMemoryDraft,
    CaseMemoryRecord,
    CaseMemoryTransition,
    ContextSnapshot,
    ContextSummary,
    ContextAccessScope,
    ContextWindowUsage,
    CriterionAssessment,
    DecisionRecord,
    DecisionOutcomeRecord,
    DerivedFactCandidate,
    DiagnosticPlaybook,
    EvidenceRecord,
    EvidenceAssertion,
    EvidenceExcerpt,
    EvidenceSearchFilters,
    EvidenceSearchHit,
    EvidenceUpdatedEvent,
    ExperimentCandidate,
    HistoricalCaseReference,
    InvestigationReport,
    InvestigationRequest,
    InvestigationResultEvent,
    IngestOutcome,
    InvestigationWorkingState,
    HypothesisDelta,
    HypothesisOutcome,
    ObservablePrediction,
    NextStep,
    ObjectiveSpec,
    PlannedAction,
    PlanDelta,
    PlaybookExperimentTemplate,
    RunResult,
    RunResultSnapshot,
    ReadRequest,
    SelectionRecord,
    SandboxEnvironment,
    SandboxOperationRef,
    SandboxOperationView,
    SandboxResourceLimits,
    ExperimentSpec,
)


V1_MODELS = {
    "InvestigationRequest": InvestigationRequest,
    "AnomalyDetectedEvent": AnomalyDetectedEvent,
    "CapabilityDescriptor": CapabilityDescriptor,
    "ActionSpec": ActionSpec,
    "RunResult": RunResult,
    "EvidenceRecord": EvidenceRecord,
    "EvidenceUpdatedEvent": EvidenceUpdatedEvent,
    "InvestigationReport": InvestigationReport,
    "InvestigationResultEvent": InvestigationResultEvent,
}

V2_MODELS = {
    "EvidenceAssertion": EvidenceAssertion,
    "CapabilityDescriptor": CapabilityDescriptor,
    "ActionSpec": ActionSpec,
    "RunResult": RunResult,
    "RunResultSnapshot": RunResultSnapshot,
    "EvidenceRecord": EvidenceRecord,
    "IngestOutcome": IngestOutcome,
    "NextStep": NextStep,
    "ReadRequest": ReadRequest,
    "AnalysisRequest": AnalysisRequest,
    "CodeExecutionRequest": CodeExecutionRequest,
    "CodeDirectoryPermission": CodeDirectoryPermission,
    "CodeExecutionPermissions": CodeExecutionPermissions,
    "ObservablePrediction": ObservablePrediction,
    "ExperimentCandidate": ExperimentCandidate,
    "CandidateAssessment": CandidateAssessment,
    "CandidateDraft": CandidateDraft,
    "CandidateDesign": CandidateDesign,
    "SelectionRecord": SelectionRecord,
    "HypothesisOutcome": HypothesisOutcome,
    "CandidateOutcomeRecord": CandidateOutcomeRecord,
    "ExperimentSpec": ExperimentSpec,
    "ObjectiveSpec": ObjectiveSpec,
    "CriterionAssessment": CriterionAssessment,
    "InvestigationWorkingState": InvestigationWorkingState,
    "ContextSnapshot": ContextSnapshot,
    "ContextSummary": ContextSummary,
    "ContextAccessScope": ContextAccessScope,
    "ContextWindowUsage": ContextWindowUsage,
    "EvidenceSearchFilters": EvidenceSearchFilters,
    "EvidenceSearchHit": EvidenceSearchHit,
    "EvidenceExcerpt": EvidenceExcerpt,
    "DecisionRecord": DecisionRecord,
    "DecisionOutcomeRecord": DecisionOutcomeRecord,
    "DiagnosticPlaybook": DiagnosticPlaybook,
    "PlaybookExperimentTemplate": PlaybookExperimentTemplate,
    "PlanDelta": PlanDelta,
    "HypothesisDelta": HypothesisDelta,
    "PlannedAction": PlannedAction,
    "InvestigationReport": InvestigationReport,
    "CaseMemoryDraft": CaseMemoryDraft,
    "CaseMemoryRecord": CaseMemoryRecord,
    "CaseMemoryTransition": CaseMemoryTransition,
    "HistoricalCaseReference": HistoricalCaseReference,
    "SandboxResourceLimits": SandboxResourceLimits,
    "SandboxEnvironment": SandboxEnvironment,
    "AnalysisProgram": AnalysisProgram,
    "AnalysisJobSpec": AnalysisJobSpec,
    "AnalysisJobView": AnalysisJobView,
    "SandboxOperationRef": SandboxOperationRef,
    "SandboxOperationView": SandboxOperationView,
    "AnalysisJobRecord": AnalysisJobRecord,
    "AnalysisOutputItem": AnalysisOutputItem,
    "AnalysisJobResult": AnalysisJobResult,
    "AnalysisResultSnapshot": AnalysisResultSnapshot,
    "AnalysisResultSubmission": AnalysisResultSubmission,
    "DerivedFactCandidate": DerivedFactCandidate,
}


def main() -> None:
    # v1 is a published snapshot and must not be regenerated from evolving models.
    target = Path(__file__).resolve().parents[1] / "src" / "game_test_agent" / "contracts" / "v2"
    target.mkdir(parents=True, exist_ok=True)
    for name, model in V2_MODELS.items():
        schema = model.model_json_schema(by_alias=True)
        schema["$id"] = f"contract://v2/{name}"
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        (target / f"{name}.json").write_text(
            json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
