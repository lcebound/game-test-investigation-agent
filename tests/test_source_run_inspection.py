from __future__ import annotations

import asyncio
import hashlib
from unittest.mock import AsyncMock, Mock
from types import SimpleNamespace

import pytest

from game_test_agent.activities import ReasoningService
from game_test_agent.agents import Investigator
from game_test_agent.capabilities import CapabilityRegistry
from game_test_agent.contracts.models import InvestigationRequest, RunResult
from game_test_agent.evidence import EvidenceManifestIngestor
from game_test_agent.planning import ActionPlanner
from game_test_agent.reports import ReportBuilder, ReportService
from game_test_agent.storage import LocalArtifactStore
from game_test_agent.workflows.controller import InvestigationController


def test_optional_mcp_source_reader_is_skipped_without_spending_budget(repository):
    view, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a", project_id="game-a", source="event",
            objective={"objective_id": "diagnose", "description": "Inspect failure"},
            constraints={"source_run_ref": {
                "provider_id": "lab-a", "project_id": "game-a", "run_id": "run-original",
            }},
        ),
        "optional-mcp-source",
    )
    provider = SimpleNamespace(source_run_supported=False, query_source_run=AsyncMock())
    controller = object.__new__(InvestigationController)
    controller.repository = repository
    controller.provider_lookup = lambda _: provider
    asyncio.run(controller._inspect_source_run(view.investigation_id, {"runs:read", "evidence:read"}))
    provider.query_source_run.assert_not_awaited()
    assert repository.budget_usage(view.investigation_id).get("tool_reads", 0) == 0


def test_source_inspection_collects_original_evidence_without_creating_a_run(repository, settings):
    ref = {"provider_id": "lab-a", "project_id": "game-a", "run_id": "run-original"}
    view, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            source="event",
            objective={"objective_id": "diagnose", "description": "Diagnose original failure"},
            constraints={"source_run_ref": ref, "provider_allowlist": ["lab-a"]},
        ),
        "source-inspection",
    )
    content = b"Original Unreal test failed: assertion did not match"
    result = RunResult(
        run_ref=ref,
        revision=1,
        execution_status="failed",
        test_verdict="failed",
        execution_source="real",
        provider_status="failed",
        evidence_manifest=[
            {
                "evidence_id": "log-original",
                "artifact_ref": "artifact-original",
                "kind": "log",
                "mime_type": "text/plain",
                "sha256": hashlib.sha256(content).hexdigest(),
                "locator": {"kind": "region", "value": "whole"},
                "quality": "partial",
                "producer": "platform",
                "producer_version": "1",
            }
        ],
    )
    provider = Mock(
        provider_id="lab-a",
        query_source_run=AsyncMock(return_value=result),
        fetch_artifact=AsyncMock(return_value=(content, "text/plain")),
    )
    controller = InvestigationController(
        repository,
        CapabilityRegistry(repository),
        ReasoningService(repository, Investigator("disabled", False)),
        ActionPlanner("disabled", False),
        Mock(),
        EvidenceManifestIngestor(repository, LocalArtifactStore(settings.artifact_root)),
        ReportService(repository),
        provider_lookup=lambda _: provider,
    )
    with pytest.raises(PermissionError):
        asyncio.run(controller._inspect_source_run(view.investigation_id, set()))
    provider.query_source_run.assert_not_called()
    asyncio.run(
        controller._inspect_source_run(view.investigation_id, {"runs:read", "evidence:read"})
    )
    asyncio.run(
        controller._inspect_source_run(view.investigation_id, {"runs:read", "evidence:read"})
    )
    provider.query_source_run.assert_awaited_once()
    assert not repository.list_actions(view.investigation_id)
    records = repository.list_current_evidence(view.investigation_id)
    assert len(records) == 1 and records[0].run_ref == result.run_ref
    assert records[0].source_type == "run"
    assert records[0].build_ref is None
    assert repository.budget_usage(view.investigation_id).get("real_runs", 0) == 0
    assert repository.budget_usage(view.investigation_id)["tool_reads"] == 2
    context = controller.context_builder.build(view.investigation_id, scopes={"runs:read", "evidence:read"})
    assert context.excerpts[0].text == content.decode()
    report = ReportBuilder(repository).build(view.investigation_id, "source_inspected")
    ReportService(repository).publish(report)
    assert repository.pending_events()[0]["payload"]["data"]["run_refs"][0] == ref
