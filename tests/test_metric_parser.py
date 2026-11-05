from __future__ import annotations

from datetime import datetime, timezone

from game_test_agent.contracts.models import (
    EvidenceLocator,
    EvidenceRecord,
    InvestigationRequest,
    TestObjective as Objective,
)
from game_test_agent.evidence import EvidenceService, MetricCsvAnalyzer
from game_test_agent.storage import LocalArtifactStore


def test_metric_csv_becomes_traceable_fact(repository, settings):
    investigation, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective=Objective(objective_id="perf", description="验证帧耗时"),
        ),
        "req-metric",
    )
    artifacts = LocalArtifactStore(settings.artifact_root)
    stored = artifacts.put_bytes("studio-a", "game-a", b"frame_ms\n10\n20\n30\n40\n50\n")
    evidence = EvidenceRecord(
        evidence_id="ev-frame-csv",
        tenant_id="studio-a",
        project_id="game-a",
        artifact_ref=stored.ref,
        kind="frame_metrics",
        mime_type="text/csv",
        sha256=stored.sha256,
        producer="platform",
        producer_version="1",
        collected_at=datetime.now(timezone.utc),
        locator=EvidenceLocator(kind="sample_window", value="rows 2-6"),
        quality="verified",
    )
    EvidenceService(repository).ingest(investigation.investigation_id, evidence)
    fact = MetricCsvAnalyzer(repository, artifacts).analyze(
        investigation.investigation_id, evidence.evidence_id, column="frame_ms", unit="ms"
    )
    assert fact.data["count"] == 5
    assert fact.data["p50"] == 30
    assert fact.data["p95"] == 48
    assert repository.list_facts(investigation.investigation_id)[0].fact_id == fact.fact_id
