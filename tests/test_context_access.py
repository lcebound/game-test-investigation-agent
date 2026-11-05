from __future__ import annotations

from datetime import datetime, timezone

import pytest

from game_test_agent.context import ContextAccess
from game_test_agent.contracts.models import (
    ContextAccessScope,
    EvidenceLocator,
    EvidenceRecord,
    EvidenceSearchFilters,
    Fact,
    InvestigationRequest,
    RunRef,
    TestObjective as Objective,
)
from game_test_agent.evidence import EvidenceService
from game_test_agent.storage import LocalArtifactStore


def test_context_access_searches_facts_and_reads_bounded_excerpt(repository, settings):
    investigation, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective=Objective(objective_id="context", description="检索已有证据"),
        ),
        "context-access",
    )
    artifacts = LocalArtifactStore(settings.artifact_root)
    content = b"frame spike in battle\nsecond line\n"
    stored = artifacts.put_bytes("studio-a", "game-a", content)
    evidence = EvidenceRecord(
        evidence_id="ev-context-log",
        tenant_id="studio-a",
        project_id="game-a",
        artifact_ref=stored.ref,
        kind="runtime_log",
        mime_type="text/plain",
        sha256=stored.sha256,
        producer="portable-provider",
        producer_version="1",
        collected_at=datetime.now(timezone.utc),
        locator=EvidenceLocator(kind="line_range", value="1-2"),
        quality="verified",
        source_type="user_upload",
        source_evidence_id="upload-1",
    )
    outcome = EvidenceService(repository).ingest(investigation.investigation_id, evidence)
    assert outcome.status == "created"
    repository.add_fact(
        Fact(
            fact_id="fact-gpu-spike",
            investigation_id=investigation.investigation_id,
            statement="GPU spike observed during battle",
            evidence_ids=[evidence.evidence_id],
            extractor="test",
            extractor_version="1",
        )
    )
    access = ContextAccess(repository, artifacts)
    scope = ContextAccessScope(tenant_id="studio-a", project_id="game-a")

    hits = access.search_evidence(
        investigation.investigation_id,
        scope,
        filters=EvidenceSearchFilters(kind="runtime_log", quality="verified"),
        query="gpu battle",
    )
    assert [item.evidence.evidence_id for item in hits] == ["ev-context-log"]
    assert hits[0].matching_fact_ids == ["fact-gpu-spike"]

    excerpt = access.read_evidence(
        investigation.investigation_id,
        evidence.evidence_id,
        scope,
        max_bytes=5,
    )
    assert excerpt.text == "frame"
    assert excerpt.truncated is True
    assert excerpt.next_byte_offset == 5
    remainder = access.read_evidence(
        investigation.investigation_id,
        evidence.evidence_id,
        scope,
        byte_offset=excerpt.next_byte_offset,
    )
    assert remainder.text.startswith(" spike")
    assert remainder.truncated is False

    history = access.get_history(investigation.investigation_id, scope)
    assert history.events
    assert history.next_cursor == history.events[-1]["cursor"]


def test_context_access_rejects_cross_project_reads(repository, settings):
    investigation, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective=Objective(objective_id="scope", description="验证读取隔离"),
        ),
        "context-scope",
    )
    access = ContextAccess(repository, LocalArtifactStore(settings.artifact_root))
    wrong_scope = ContextAccessScope(tenant_id="studio-a", project_id="game-b")

    with pytest.raises(PermissionError, match="cross-project"):
        access.search_evidence(investigation.investigation_id, wrong_scope)
    with pytest.raises(PermissionError, match="cross-project"):
        access.get_run(
            ContextAccessScope(tenant_id="studio-a", project_id="game-a"),
            RunRef(provider_id="portable-provider", project_id="game-b", run_id="run-1"),
        )
