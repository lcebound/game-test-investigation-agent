from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from game_test_agent.api import create_app
from game_test_agent.capabilities import CapabilityRegistry
from game_test_agent.contracts.models import (
    BuildRef,
    CaseApplicability,
    CaseMemoryContent,
    CaseMemoryDraft,
    CaseMemoryStatus,
    CaseMemoryTransition,
    ConclusionLevel,
    Coverage,
    InvestigationReport,
    InvestigationRequest,
    InvestigationStatus,
    Resolution,
    TestObjective as Objective,
    VersionInterval,
)
from game_test_agent.evidence.context import ContextBuilder
from game_test_agent.memory import CaseMemoryService
from game_test_agent.storage import ConflictError


def _completed_source(repository, *, tenant: str = "studio-a", project: str = "game-a"):
    view, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id=tenant,
            project_id=project,
            objective=Objective(objective_id="diagnose", description="诊断卡顿"),
            build_ref=BuildRef(id="build-42", revision="2026.9.12"),
        ),
        f"source-{tenant}-{project}",
    )
    repository.transition(view.investigation_id, InvestigationStatus.DISCOVERING)
    repository.transition(view.investigation_id, InvestigationStatus.PLANNING)
    repository.transition(view.investigation_id, InvestigationStatus.REPORTING)
    report = InvestigationReport(
        investigation_id=view.investigation_id,
        revision=1,
        status="completed",
        resolution=Resolution.DIAGNOSED,
        summary="历史调查完成",
        conclusion_level=ConclusionLevel.OBSERVATION,
        coverage=Coverage(completed_objectives=["diagnose"]),
        stop_reason="sufficient_evidence",
    )
    repository.save_report(report, "# 历史调查")
    repository.transition(
        view.investigation_id,
        InvestigationStatus.COMPLETED,
        resolution=Resolution.DIAGNOSED,
        stop_reason="sufficient_evidence",
    )
    return repository.get_investigation(view.investigation_id)


def _draft() -> CaseMemoryDraft:
    return CaseMemoryDraft(
        content=CaseMemoryContent(
            title="流式加载时帧时间尖峰",
            symptoms=["进入新区域时 p95 帧时间升高"],
            investigation_path=["先核对已有运行", "比较关闭流式加载的对照运行"],
            effective_experiments=["固定场景后切换流式加载并比较 p95"],
            ineffective_experiments=["仅重复同一运行而不改变变量"],
            confirmed_outcome="历史构建中，流式加载与帧时间尖峰相关",
            limitations=["必须在当前构建重新验证"],
            tags=["performance_regression", "streaming"],
        ),
        applicability=CaseApplicability(
            engine_family="generic-engine",
            engine_version=VersionInterval(
                min_inclusive="5.2",
                max_exclusive="6.0",
            ),
            build_revision=VersionInterval(
                min_inclusive="2026.1",
                max_exclusive="2027.1",
            ),
        ),
        confidence="medium",
    )


def _activate(service: CaseMemoryService, case_id: str):
    reviewed = service.transition(
        case_id,
        CaseMemoryTransition(
            expected_revision=1,
            target_status=CaseMemoryStatus.REVIEWED,
            reason="人工核对报告和调查轨迹",
            confidence="high",
        ),
        actor="reviewer-a",
    )
    return service.transition(
        case_id,
        CaseMemoryTransition(
            expected_revision=reviewed.revision,
            target_status=CaseMemoryStatus.ACTIVE,
            reason="批准用于规划提示",
        ),
        actor="reviewer-a",
    )


def test_case_memory_requires_review_and_never_returns_current_evidence(repository):
    source = _completed_source(repository)
    service = CaseMemoryService(repository)

    candidate, created = service.create_candidate(
        source.investigation_id,
        _draft(),
        created_by="author-a",
    )
    replayed, replay_created = service.create_candidate(
        source.investigation_id,
        _draft(),
        created_by="another-author",
    )
    assert created and not replay_created
    assert replayed == candidate
    assert service.search("studio-a", "game-a") == []

    active = _activate(service, candidate.case_id)
    hits = service.search(
        "studio-a",
        "game-a",
        query="streaming",
        engine_family="GENERIC-ENGINE",
        engine_version="5.4",
        build_ref=BuildRef(id="other-build", revision="2026.9.20"),
    )

    assert active.status == CaseMemoryStatus.ACTIVE
    assert len(hits) == 1
    assert hits[0].scope == "historical_reference"
    assert hits[0].warning == "historical_case_is_not_current_evidence"
    assert "evidence_ids" not in hits[0].model_dump()
    assert service.search("other-tenant", "game-a") == []
    assert service.search("studio-a", "other-project") == []
    assert (
        service.search(
            "studio-a",
            "game-a",
            engine_family="generic-engine",
            engine_version="6.0",
            build_ref=BuildRef(id="other-build", revision="2026.9.20"),
        )
        == []
    )


def test_case_memory_lifecycle_is_revisioned_and_retraction_invalidates_search(repository):
    source = _completed_source(repository)
    service = CaseMemoryService(repository)
    candidate, _ = service.create_candidate(
        source.investigation_id,
        _draft(),
        created_by="author-a",
    )
    active = _activate(service, candidate.case_id)

    with pytest.raises(ConflictError, match="invalid case memory status transition"):
        repository.save_case_memory(
            active.model_copy(
                update={
                    "revision": 4,
                    "status": CaseMemoryStatus.REVIEWED,
                    "state_reason": "bypass service",
                    "state_changed_by": "bypass",
                }
            ),
            expected_previous_revision=3,
        )

    with pytest.raises(ConflictError, match="expected revision 3"):
        service.transition(
            candidate.case_id,
            CaseMemoryTransition(
                expected_revision=1,
                target_status=CaseMemoryStatus.RETRACTED,
                reason="stale command",
            ),
            actor="reviewer-a",
        )

    retracted = service.retract_for_source(
        source.investigation_id,
        actor="privacy-service",
        reason="source investigation withdrawn",
    )
    assert [item.status for item in retracted] == [CaseMemoryStatus.RETRACTED]
    assert [item.status for item in repository.list_case_memory_revisions(candidate.case_id)] == [
        CaseMemoryStatus.CANDIDATE,
        CaseMemoryStatus.REVIEWED,
        CaseMemoryStatus.ACTIVE,
        CaseMemoryStatus.RETRACTED,
    ]
    assert service.search("studio-a", "game-a") == []


def test_expired_memory_is_not_retrieved(repository):
    source = _completed_source(repository)
    service = CaseMemoryService(repository)
    candidate, _ = service.create_candidate(
        source.investigation_id,
        _draft(),
        created_by="author-a",
        valid_until=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    _activate(service, candidate.case_id)
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    assert service.search("studio-a", "game-a", now=future) == []


def test_context_includes_only_active_applicable_cases_as_bounded_hints(repository):
    source = _completed_source(repository)
    service = CaseMemoryService(repository)
    candidate, _ = service.create_candidate(
        source.investigation_id,
        _draft(),
        created_by="author-a",
    )
    _activate(service, candidate.case_id)
    target, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective=Objective(objective_id="new", description="调查新的帧时间尖峰"),
            build_ref=BuildRef(id="build-99", revision="2026.10"),
            constraints={
                "anomaly_kind": "performance_regression",
                "engine_family": "generic-engine",
                "engine_version": "5.4",
            },
        ),
        "target-context",
    )

    context = ContextBuilder(repository, CapabilityRegistry(repository)).build(
        target.investigation_id,
        scopes=set(),
    )

    assert [item.case_id for item in context.historical_cases] == [candidate.case_id]
    assert "historical_cases_are_planning_hints_not_current_evidence" in context.limitations
    assert context.evidence == []
    assert context.facts == []
    ablated = ContextBuilder(repository, CapabilityRegistry(repository)).build(
        target.investigation_id,
        scopes=set(),
        include_historical_cases=False,
    )
    assert ablated.historical_cases == []
    assert "historical_cases_are_planning_hints_not_current_evidence" not in ablated.limitations
    assert ablated.evidence == context.evidence
    assert ablated.facts == context.facts
    assert ablated.working_state == context.working_state


def test_case_memory_api_enforces_review_scopes_and_exposes_only_active_hints(settings):
    async def scenario() -> None:
        app = create_app(settings)
        source = _completed_source(app.state.container.repository)
        transport = httpx.ASGITransport(app=app)
        base_auth = {
            "X-Principal-ID": "memory-user",
            "X-Tenant-ID": "studio-a",
            "X-Project-ID": "game-a",
        }
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                forbidden = await client.post(
                    f"/v1/investigations/{source.investigation_id}/memory-candidates",
                    headers=base_auth,
                    json=_draft().model_dump(mode="json"),
                )
                assert forbidden.status_code == 403
                candidate_response = await client.post(
                    f"/v1/investigations/{source.investigation_id}/memory-candidates",
                    headers={**base_auth, "X-Scopes": "agent:memory:write"},
                    json=_draft().model_dump(mode="json"),
                )
                assert candidate_response.status_code == 201
                candidate = candidate_response.json()
                hidden = await client.get("/v1/memory/cases", headers=base_auth)
                assert hidden.json() == []
                for expected_revision, target in ((1, "reviewed"), (2, "active")):
                    transitioned = await client.post(
                        f"/v1/memory/cases/{candidate['case_id']}/transitions",
                        headers={**base_auth, "X-Scopes": "agent:memory:review"},
                        json={
                            "expected_revision": expected_revision,
                            "target_status": target,
                            "reason": f"approve {target}",
                        },
                    )
                    assert transitioned.status_code == 200
                visible = await client.get(
                    "/v1/memory/cases",
                    headers=base_auth,
                    params={
                        "query": "streaming",
                        "engine_family": "generic-engine",
                        "engine_version": "5.4",
                        "build_id": "build-99",
                        "build_revision": "2026.10",
                    },
                )
                assert visible.status_code == 200
                assert visible.json()[0]["scope"] == "historical_reference"
                assert "source_evidence_ids" not in visible.json()[0]

    asyncio.run(scenario())
