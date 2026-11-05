from __future__ import annotations

import pytest

from game_test_agent.contracts.models import (
    Claim,
    ConclusionLevel,
    Coverage,
    InvestigationReport,
    InvestigationRequest,
    InvestigationResultEvent,
    Resolution,
    TestObjective as Objective,
)
from game_test_agent.reports import ReportService


def test_report_rejects_missing_evidence(repository):
    investigation, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective=Objective(objective_id="perf", description="验证帧耗时"),
        ),
        "req-1",
    )
    report = InvestigationReport(
        investigation_id=investigation.investigation_id,
        revision=1,
        status="completed",
        resolution=Resolution.DIAGNOSED,
        summary="已复现",
        conclusion_level=ConclusionLevel.REPRODUCED,
        claims=[Claim(claim_id="c1", text="问题已复现", evidence_ids=["missing"])],
        coverage=Coverage(completed_objectives=["perf"]),
        stop_reason="goal_met",
    )
    with pytest.raises(ValueError, match="missing evidence"):
        ReportService(repository).publish(report)


def test_report_creates_versioned_result_event_envelope(repository):
    investigation, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective=Objective(objective_id="perf", description="验证帧耗时"),
        ),
        "req-event-report",
    )
    report = InvestigationReport(
        investigation_id=investigation.investigation_id,
        revision=1,
        status="completed",
        resolution=Resolution.INCONCLUSIVE,
        summary="尚无足够证据",
        conclusion_level=ConclusionLevel.UNKNOWN,
        coverage=Coverage(uncovered_objectives=["perf"]),
        stop_reason="required_capability_unavailable",
    )
    digest = ReportService(repository).publish(report)
    queued = repository.pending_events()[0]
    envelope = InvestigationResultEvent.model_validate(queued["payload"])
    assert envelope.data.report_hash == digest
    assert envelope.tenant_id == "studio-a"
    assert envelope.event_type == "game_test.investigation.completed"
