from __future__ import annotations

from dataclasses import dataclass, field

from game_test_agent.contracts.models import EvidenceRecord, Fact
from game_test_agent.evidence.parsers import (
    CrashJsonAnalyzer,
    FunctionalFailureJsonAnalyzer,
    InfrastructureJsonAnalyzer,
    MemoryTrendJsonAnalyzer,
    MetricCsvAnalyzer,
)
from game_test_agent.storage import ArtifactStore, Repository


@dataclass(slots=True)
class EvidenceAnalysisOutcome:
    facts: list[Fact] = field(default_factory=list)
    failed_evidence_ids: list[str] = field(default_factory=list)
    unsupported_evidence_ids: list[str] = field(default_factory=list)


class EvidenceAnalyzerRouter:
    """Routes allowlisted evidence kinds to deterministic parsers.

    Parser failures remain visible as investigation events but do not discard the
    immutable source evidence or fail the external run itself.
    """

    def __init__(self, repository: Repository, artifacts: ArtifactStore) -> None:
        self.repository = repository
        self.metric = MetricCsvAnalyzer(repository, artifacts)
        self.failure = FunctionalFailureJsonAnalyzer(repository, artifacts)
        self.crash = CrashJsonAnalyzer(repository, artifacts)
        self.memory = MemoryTrendJsonAnalyzer(repository, artifacts)
        self.infrastructure = InfrastructureJsonAnalyzer(repository, artifacts)

    def analyze_records(
        self, investigation_id: str, records: list[EvidenceRecord]
    ) -> EvidenceAnalysisOutcome:
        outcome = EvidenceAnalysisOutcome()
        for record in records:
            try:
                fact = self._analyze_one(investigation_id, record)
            except (OSError, ValueError) as error:
                outcome.failed_evidence_ids.append(record.evidence_id)
                self.repository.record_investigation_event(
                    investigation_id,
                    "evidence.analysis_failed",
                    {
                        "evidence_id": record.evidence_id,
                        "kind": record.kind,
                        "error_type": type(error).__name__,
                    },
                )
                continue
            if fact is None:
                outcome.unsupported_evidence_ids.append(record.evidence_id)
            else:
                outcome.facts.append(fact)
        return outcome

    def _analyze_one(self, investigation_id: str, record: EvidenceRecord) -> Fact | None:
        if record.kind in {"frame_metrics", "frame_metrics_csv"}:
            return self.metric.analyze(
                investigation_id, record.evidence_id, column="frame_ms", unit="ms"
            )
        if record.kind in self.failure.ALLOWED_KINDS:
            return self.failure.analyze(investigation_id, record.evidence_id)
        if record.kind in self.crash.ALLOWED_KINDS:
            return self.crash.analyze(investigation_id, record.evidence_id)
        if record.kind in self.memory.ALLOWED_KINDS:
            return self.memory.analyze(investigation_id, record.evidence_id)
        if record.kind in self.infrastructure.ALLOWED_KINDS:
            return self.infrastructure.analyze(investigation_id, record.evidence_id)
        return None
