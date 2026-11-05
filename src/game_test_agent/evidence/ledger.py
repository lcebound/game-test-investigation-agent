from __future__ import annotations

from game_test_agent.contracts.models import (
    EvidenceRecord,
    EvidenceRelation,
    Fact,
    Hypothesis,
    HypothesisStatus,
)
from game_test_agent.storage import Repository


class HypothesisLedger:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def add_fact(self, fact: Fact) -> bool:
        known = {
            item.evidence_id
            for item in self.repository.list_current_evidence(fact.investigation_id)
        }
        missing = set(fact.evidence_ids) - known
        if missing:
            raise ValueError(f"fact references missing evidence: {sorted(missing)}")
        return self.repository.add_fact(fact)

    def apply(self, hypothesis: Hypothesis) -> None:
        self.validate(hypothesis)
        self.repository.save_hypothesis(hypothesis)
        for evidence_id in hypothesis.supporting_evidence_ids:
            self.repository.add_evidence_relation(
                hypothesis.investigation_id,
                EvidenceRelation(
                    source_evidence_id=evidence_id,
                    target_id=hypothesis.hypothesis_id,
                    relation_type="supports",
                ),
            )
        for evidence_id in hypothesis.contradicting_evidence_ids:
            self.repository.add_evidence_relation(
                hypothesis.investigation_id,
                EvidenceRelation(
                    source_evidence_id=evidence_id,
                    target_id=hypothesis.hypothesis_id,
                    relation_type="refutes",
                ),
            )

    def validate(self, hypothesis: Hypothesis) -> dict[str, EvidenceRecord]:
        self.repository.get_investigation(hypothesis.investigation_id)
        evidence = {
            item.evidence_id: item
            for item in self.repository.list_current_evidence(hypothesis.investigation_id)
        }
        referenced = set(hypothesis.supporting_evidence_ids) | set(
            hypothesis.contradicting_evidence_ids
        )
        missing = referenced - evidence.keys()
        if missing:
            raise ValueError(f"hypothesis references missing evidence: {sorted(missing)}")
        overlap = set(hypothesis.supporting_evidence_ids) & set(
            hypothesis.contradicting_evidence_ids
        )
        if overlap:
            raise ValueError(
                f"evidence cannot both support and refute one revision: {sorted(overlap)}"
            )
        if hypothesis.status == HypothesisStatus.CONFIRMED:
            supporting = [evidence[item] for item in hypothesis.supporting_evidence_ids]
            if not supporting or any(
                item.quality in {"simulated", "untrusted"} or item.execution_source != "real"
                for item in supporting
            ):
                raise ValueError(
                    "a confirmed hypothesis requires real, non-simulated supporting evidence"
                )
        return evidence
