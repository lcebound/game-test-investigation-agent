from __future__ import annotations

from datetime import datetime, timezone

from game_test_agent.capabilities import CapabilityRegistry
from game_test_agent.contracts.models import (
    ActionSpec,
    CapabilityDescriptor,
    EvidenceLocator,
    EvidenceRecord,
    InvestigationRequest,
    RunRef,
    RunResult,
    TestObjective as Objective,
)
from game_test_agent.policies import PolicyEngine
from game_test_agent.reports import ReportBuilder


def test_report_states_real_run_verdict_without_claiming_build_acceptance(repository):
    investigation, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective=Objective(objective_id="single", description="本次运行是否通过"),
        ),
        "report-real-run-verdict",
    )
    registry = CapabilityRegistry(repository)
    registry.register(
        CapabilityDescriptor.model_validate(
            {
                "capability_id": "test.run",
                "capability_version": "1",
                "provider_id": "portable-lab",
                "provider_revision": "1",
                "tool_name": "test.run",
                "project_ids": ["game-a"],
                "input_schema": {"type": "object"},
                "risk_level": "bounded_execution",
                "execution_owner": "portable-lab",
                "resource_domain": "lab",
                "estimated_cost": {"device_minutes": 1},
            }
        )
    )
    action = ActionSpec(
        tenant_id="studio-a",
        project_id="game-a",
        investigation_id=investigation.investigation_id,
        action_id="act-real-verdict",
        idempotency_key="act-real-verdict",
        capability_id="test.run",
        capability_version="1",
        provider_id="portable-lab",
        parameters={},
        expected_observation="平台测试判定",
        plan_revision=1,
    )
    PolicyEngine(repository, registry).reserve(action, principal_id="test", scopes=set())
    run_ref = RunRef(provider_id="portable-lab", project_id="game-a", run_id="run-one")
    repository.record_run_result(
        action.action_id,
        RunResult(
            run_ref=run_ref,
            revision=1,
            execution_status="succeeded",
            test_verdict="passed",
            execution_source="real",
            provider_status="PASSED",
            missing_fields=["build.hash", "environment.environment_fingerprint"],
        ),
    )
    repository.add_evidence(
        investigation.investigation_id,
        EvidenceRecord(
            evidence_id="obs-run-one-report",
            tenant_id="studio-a",
            project_id="game-a",
            run_ref=run_ref,
            execution_source="real",
            artifact_ref="artifact://studio-a/game-a/report",
            kind="test_report",
            mime_type="text/html",
            sha256="a" * 64,
            producer="portable-lab",
            producer_version="1",
            collected_at=datetime.now(timezone.utc),
            locator=EvidenceLocator(kind="region", value="whole"),
            quality="partial",
            limitations=["build identity missing"],
            source_type="run",
            source_evidence_id="artifact-report",
            action_id=action.action_id,
        ),
    )
    report = ReportBuilder(repository).build(investigation.investigation_id, "model_report")
    assert report.summary == "本次 Run 平台判定通过；指定构建验收证据不足。"
    interrupted = ReportBuilder(repository).build(
        investigation.investigation_id, "action_planning_failed:ValueError"
    )
    assert interrupted.summary.startswith("调查因 action_planning_failed:ValueError 中断")
    assert "目标尚未完成" in interrupted.summary
    assert "本次 Run 平台判定通过" in interrupted.summary
    assert interrupted.coverage.uncovered_objectives == ["single"]
    assert report.resolution == "inconclusive"
    assert any(
        claim.text == "平台判定本次 Run run-one 的测试结果为通过。"
        and claim.evidence_ids == ["obs-run-one-report"]
        and "build.hash" in claim.limitations
        for claim in report.claims
    )
