from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest

from game_test_agent.contracts.models import (
    EvidenceLocator,
    EvidenceRecord,
    Hypothesis,
    HypothesisStatus,
    InvestigationRequest,
    TestObjective as Objective,
)
from game_test_agent.evidence import EvidenceService, HypothesisLedger


def test_simulated_evidence_cannot_confirm_hypothesis(repository):
    investigation, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective=Objective(objective_id="perf", description="调查性能"),
        ),
        "req-ledger",
    )
    content = b"simulated"
    evidence = EvidenceRecord(
        evidence_id="ev-sim",
        tenant_id="studio-a",
        project_id="game-a",
        artifact_ref="artifact://studio-a/game-a/simulated",
        kind="metrics",
        mime_type="text/csv",
        sha256=hashlib.sha256(content).hexdigest(),
        producer="reference",
        producer_version="1",
        collected_at=datetime.now(timezone.utc),
        locator=EvidenceLocator(kind="sample_window", value="0-10s"),
        quality="simulated",
    )
    EvidenceService(repository).ingest(investigation.investigation_id, evidence)
    hypothesis = Hypothesis(
        hypothesis_id="hyp-gpu",
        investigation_id=investigation.investigation_id,
        revision=1,
        statement="GPU 是瓶颈",
        scope="build-42/env-1",
        supporting_evidence_ids=["ev-sim"],
        falsifiable_prediction="降低 GPU 负载后帧耗时下降",
        status=HypothesisStatus.CONFIRMED,
    )
    with pytest.raises(ValueError, match="non-simulated"):
        HypothesisLedger(repository).apply(hypothesis)
