from __future__ import annotations

from game_test_agent.contracts.models import EvidenceRecord, HypothesisStatus, IngestOutcome
from game_test_agent.memory import CaseMemoryService
from game_test_agent.objectives import ObjectiveEvaluator
from game_test_agent.provenance import ProvenanceIndex
from game_test_agent.storage import Repository
from game_test_agent.observability.tracing import trace_operation


class EvidenceService:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    @trace_operation("evidence.ingest")
    def ingest(self, investigation_id: str, evidence: EvidenceRecord) -> IngestOutcome:
        investigation = self.repository.get_investigation(investigation_id)
        if (evidence.tenant_id, evidence.project_id) != (
            investigation.request.tenant_id,
            investigation.request.project_id,
        ):
            raise PermissionError("cross-project evidence is forbidden")
        if evidence.retrieval_status == "ready" and not evidence.artifact_ref:
            raise ValueError("ready evidence must have an artifact reference")
        if evidence.execution_source == "simulated" and evidence.quality != "simulated":
            raise ValueError("simulated execution evidence must be marked simulated")
        expected_build = investigation.request.build_ref
        if expected_build and evidence.build_ref and evidence.build_ref.id != expected_build.id:
            raise ValueError("evidence build does not match the investigation build")
        expected_environment = investigation.request.constraints.get("environment_fingerprint")
        if (
            expected_environment
            and evidence.environment_fingerprint
            and evidence.environment_fingerprint != expected_environment
        ):
            raise ValueError("evidence environment does not match the investigation scope")
        previous = [
            item
            for item in self.repository.list_current_evidence(investigation_id)
            if self._same_source(item, evidence) and item.source_revision < evidence.source_revision
        ]
        outcome = self.repository.add_evidence(investigation_id, evidence)
        if outcome.status != "created":
            return outcome

        changed_sources = [evidence.evidence_id]
        if previous:
            previous_ids = sorted(item.evidence_id for item in previous)
            provenance = ProvenanceIndex(self.repository)
            invalidations = [
                provenance.invalidate(investigation_id, evidence_id).model_dump(mode="json")
                for evidence_id in previous_ids
            ]
            self.repository.record_investigation_event(
                investigation_id,
                "provenance.source_superseded",
                {
                    "superseded_evidence_ids": previous_ids,
                    "replacement_evidence_id": evidence.evidence_id,
                    "source_revision": evidence.source_revision,
                    "invalidations": invalidations,
                },
            )
            superseded = set(previous_ids)
            for hypothesis in self.repository.list_hypotheses(investigation_id):
                supporting = [
                    item for item in hypothesis.supporting_evidence_ids if item not in superseded
                ]
                contradicting = [
                    item for item in hypothesis.contradicting_evidence_ids if item not in superseded
                ]
                if (
                    supporting == hypothesis.supporting_evidence_ids
                    and contradicting == hypothesis.contradicting_evidence_ids
                ):
                    continue
                self.repository.save_hypothesis(
                    hypothesis.model_copy(
                        update={
                            "revision": hypothesis.revision + 1,
                            "supporting_evidence_ids": supporting,
                            "contradicting_evidence_ids": contradicting,
                            "missing_evidence": list(
                                dict.fromkeys(
                                    [
                                        *hypothesis.missing_evidence,
                                        "previous hypothesis evidence was superseded",
                                    ]
                                )
                            ),
                            "status": (
                                HypothesisStatus.UNRESOLVED
                                if hypothesis.status
                                in {HypothesisStatus.SUPPORTED, HypothesisStatus.CONFIRMED}
                                else hypothesis.status
                            ),
                        }
                    )
                )
            CaseMemoryService(self.repository).retract_for_source(
                investigation_id,
                actor="system:provenance",
                reason=(
                    f"source evidence superseded by {evidence.evidence_id} "
                    f"revision {evidence.source_revision}"
                ),
            )
            changed_sources.extend(previous_ids)
        ObjectiveEvaluator(self.repository).evaluate_incremental(
            investigation_id,
            changed_sources,
        )
        return outcome

    @staticmethod
    def _same_source(left: EvidenceRecord, right: EvidenceRecord) -> bool:
        left_run = left.run_ref
        right_run = right.run_ref
        return (
            left.source_type == right.source_type
            and left.source_evidence_id == right.source_evidence_id
            and left.analysis_job_id == right.analysis_job_id
            and (
                None
                if left_run is None
                else (left_run.provider_id, left_run.project_id, left_run.run_id)
            )
            == (
                None
                if right_run is None
                else (right_run.provider_id, right_run.project_id, right_run.run_id)
            )
        )
