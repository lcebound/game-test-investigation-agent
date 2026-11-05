from __future__ import annotations

import json
import asyncio
from datetime import datetime, timezone

import pytest

from game_test_agent.capabilities import CapabilityRegistry
from game_test_agent.activities import ActionService, ReasoningService
from game_test_agent.context.tool_loop import InvestigationToolLoop
from game_test_agent.contracts.models import (
    AnalysisRequest,
    Budget,
    EvidenceLocator,
    EvidenceRecord,
    InvestigationRequest,
    ReadRequest,
    NextStep,
    Claim,
    ConclusionLevel,
    Coverage,
    InvestigationReport,
    Resolution,
    TestObjective as Objective,
)
from game_test_agent.evidence import ContextBuilder, EvidenceService
from game_test_agent.evidence import EvidenceManifestIngestor
from game_test_agent.mcp_gateway import ActionDispatcher
from game_test_agent.planning import ActionPlanner
from game_test_agent.policies import PolicyEngine
from game_test_agent.reports import ReportService
from game_test_agent.reports.evaluator import ConclusionEvaluator
from game_test_agent.storage import ConflictError, LocalArtifactStore
from game_test_agent.workflows import InvestigationController


def _investigation(repository, *, max_tool_reads=3):
    view, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective=Objective(objective_id="diagnose", description="find the hidden error"),
            budget=Budget(max_tool_reads=max_tool_reads),
        ),
        f"read-loop-{max_tool_reads}",
    )
    return view.investigation_id


def _evidence(repository, artifacts, investigation_id, evidence_id, mime, content):
    stored = artifacts.put_bytes("studio-a", "game-a", content)
    record = EvidenceRecord(
        evidence_id=evidence_id,
        tenant_id="studio-a",
        project_id="game-a",
        artifact_ref=stored.ref,
        kind="runtime_log" if mime == "text/plain" else "metrics",
        mime_type=mime,
        sha256=stored.sha256,
        producer="test",
        producer_version="1",
        collected_at=datetime.now(timezone.utc),
        locator=EvidenceLocator(kind="sample_window", value="all"),
        quality="verified",
        source_type="user_upload",
        source_evidence_id=evidence_id,
    )
    EvidenceService(repository).ingest(investigation_id, record)
    return record


def test_read_result_reenters_context_and_consumes_logical_budget(repository, settings):
    investigation_id = _investigation(repository, max_tool_reads=1)
    artifacts = LocalArtifactStore(settings.artifact_root)
    _evidence(
        repository, artifacts, investigation_id, "log-1", "text/plain",
        b"ordinary line\nERROR: hidden crash marker\nordinary line\n",
    )
    loop = InvestigationToolLoop(repository, artifacts)
    excerpt = loop.read(
        investigation_id, "decision-1",
        ReadRequest(evidence_id="log-1", purpose="inspect the middle of the log"),
    )
    assert "hidden crash marker" in excerpt.text
    assert excerpt.excerpt_sha256 and excerpt.source_sha256
    context = ContextBuilder(repository, CapabilityRegistry(repository)).build(
        investigation_id, scopes=set()
    )
    assert [item.text for item in context.excerpts] == [excerpt.text]
    assert context.budget["tool_reads"] == 1
    with pytest.raises(ConflictError, match="tool_reads"):
        loop.read(
            investigation_id, "decision-2",
            ReadRequest(evidence_id="log-1", purpose="redundant read"),
        )


def test_registered_csv_analysis_creates_derived_evidence(repository, settings):
    investigation_id = _investigation(repository)
    artifacts = LocalArtifactStore(settings.artifact_root)
    _evidence(
        repository, artifacts, investigation_id, "metric-1", "text/csv",
        b"frame_ms,gpu_ms\n10,4\n20,\n30,8\n",
    )
    loop = InvestigationToolLoop(repository, artifacts)
    evidence_id = loop.analyze(
        investigation_id, "decision-analysis",
        AnalysisRequest(recipe_id="csv_summary_v1", input_evidence_ids=["metric-1"]),
    )
    derived = repository.get_evidence(evidence_id)
    assert derived.derived_from == ["metric-1"]
    result = json.loads(artifacts.read_bytes(derived.artifact_ref))
    assert result["columns"]["frame_ms"]["median"] == 20
    assert result["columns"]["gpu_ms"]["missing"] == 1
    assert any(item.evidence_id == evidence_id for item in
               ContextBuilder(repository, CapabilityRegistry(repository)).build(
                   investigation_id, scopes=set()
               ).evidence)


def test_repeated_range_uses_cache_but_counts_each_decision(repository, settings):
    investigation_id = _investigation(repository, max_tool_reads=2)
    artifacts = LocalArtifactStore(settings.artifact_root)
    _evidence(repository, artifacts, investigation_id, "log-1", "text/plain", b"ERROR one\n")
    loop = InvestigationToolLoop(repository, artifacts)
    first = loop.read(
        investigation_id, "decision-1", ReadRequest(evidence_id="log-1", purpose="first")
    )
    second = loop.read(
        investigation_id, "decision-2", ReadRequest(evidence_id="log-1", purpose="second")
    )
    assert first == second
    events = repository.list_events(investigation_id)
    read_events = [event for event in events if event["event_type"] == "evidence.excerpt_read"]
    assert [event["data"]["cache_hit"] for event in read_events] == [False, True]
    assert repository.budget_usage(investigation_id)["tool_reads"] == 2


def test_log_error_count_keeps_original_line_locations(repository, settings):
    investigation_id = _investigation(repository)
    artifacts = LocalArtifactStore(settings.artifact_root)
    _evidence(
        repository, artifacts, investigation_id, "log-1", "text/plain",
        b"ordinary\nERROR loading asset\nordinary\nfatal crash\n",
    )
    evidence_id = InvestigationToolLoop(repository, artifacts).analyze(
        investigation_id, "decision-errors",
        AnalysisRequest(recipe_id="log_error_count_v1", input_evidence_ids=["log-1"]),
    )
    result = json.loads(artifacts.read_bytes(repository.get_evidence(evidence_id).artifact_ref))
    assert result["count"] == 2
    assert [item["line"] for item in result["locations"]] == [2, 4]


def test_controller_reads_before_reporting_without_creating_run(repository, settings):
    investigation_id = _investigation(repository)
    artifacts = LocalArtifactStore(settings.artifact_root)
    _evidence(
        repository, artifacts, investigation_id, "log-1", "text/plain",
        b"normal\nERROR hidden between ordinary lines\nnormal\n",
    )

    class ReadThenReport:
        model_name = "scripted-reader"
        calls = 0

        async def reason_next_step(self, context):
            self.calls += 1
            if self.calls == 1:
                assert not context["excerpts"]
                return NextStep(
                    kind="retrieve_evidence",
                    objective_id="diagnose",
                    read_request=ReadRequest(
                        evidence_id="log-1", purpose="inspect body for an error"
                    ),
                    expected_observation="find the log error",
                    reason_summary="read the content",
                )
            assert "ERROR hidden" in context["excerpts"][0]["text"]
            return NextStep(
                kind="report",
                objective_id="diagnose",
                expected_observation="report the observed error",
                reason_summary="content_inspected",
            )

    registry = CapabilityRegistry(repository)
    controller = InvestigationController(
        repository,
        registry,
        ReasoningService(repository, ReadThenReport()),
        ActionPlanner("disabled", enabled=False),
        ActionService(repository, PolicyEngine(repository, registry), ActionDispatcher()),
        EvidenceManifestIngestor(repository, artifacts),
        ReportService(repository),
        provider_lookup=lambda _provider_id: None,
    )
    report = asyncio.run(
        controller.run(investigation_id, principal_id="reader", scopes=set())
    )
    assert report is not None
    assert report.stop_reason == "content_inspected"
    assert repository.list_actions(investigation_id) == []


def test_excerpt_claim_requires_actual_current_read(repository, settings):
    investigation_id = _investigation(repository)
    artifacts = LocalArtifactStore(settings.artifact_root)
    _evidence(
        repository, artifacts, investigation_id, "log-1", "text/plain",
        b"ordinary\nERROR shader compilation failed\n",
    )
    claim = Claim(
        claim_id="log-claim",
        text="The log contains a shader compilation error.",
        basis="evidence_excerpt",
        evidence_ids=["log-1"],
        supporting_excerpt="ERROR shader compilation failed",
    )
    report = InvestigationReport(
        investigation_id=investigation_id,
        revision=1,
        status="completed",
        resolution=Resolution.INCONCLUSIVE,
        summary="A log error was observed.",
        conclusion_level=ConclusionLevel.OBSERVATION,
        claims=[claim],
        coverage=Coverage(uncovered_objectives=["diagnose"]),
        stop_reason="manual_review",
    )
    evaluator = ConclusionEvaluator(repository)
    assert not evaluator.evaluate(report).accepted
    InvestigationToolLoop(repository, artifacts).read(
        investigation_id, "decision-read",
        ReadRequest(evidence_id="log-1", purpose="verify quoted error"),
    )
    assert evaluator.evaluate(report).accepted
    claim.supporting_excerpt = "ERROR different failure"
    assert not evaluator.evaluate(report).accepted


def test_recent_events_are_scoped_before_limiting(repository):
    first = _investigation(repository)
    second, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective=Objective(objective_id="other", description="other investigation"),
        ),
        "interleaved-events",
    )
    repository.record_investigation_event(first, "marker", {"value": "first"})
    for index in range(10):
        repository.record_investigation_event(
            second.investigation_id, "noise", {"index": index}
        )
    assert repository.list_recent_events(first, limit=1)[0]["data"]["value"] == "first"
