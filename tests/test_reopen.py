from __future__ import annotations

from game_test_agent.contracts.models import (
    Budget,
    InvestigationRequest,
    InvestigationStatus,
    ReopenRequest,
    Resolution,
    TestObjective as Objective,
)


def test_reopen_creates_related_investigation_with_new_budget(repository):
    parent, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective=Objective(objective_id="perf", description="调查性能"),
        ),
        "req-parent",
    )
    for state in (
        InvestigationStatus.DISCOVERING,
        InvestigationStatus.PLANNING,
        InvestigationStatus.REPORTING,
    ):
        repository.transition(parent.investigation_id, state)
    repository.transition(
        parent.investigation_id, InvestigationStatus.COMPLETED, resolution=Resolution.INCONCLUSIVE
    )
    reopened, created = repository.reopen_investigation(
        parent.investigation_id,
        ReopenRequest(reason="新增 trace 能力", budget=Budget(max_device_minutes=5)),
        "reopen-1",
    )
    replay, repeated = repository.reopen_investigation(
        parent.investigation_id,
        ReopenRequest(reason="新增 trace 能力", budget=Budget(max_device_minutes=5)),
        "reopen-1",
    )
    assert created is True
    assert repeated is False
    assert replay.investigation_id == reopened.investigation_id
    assert reopened.request.budget.max_device_minutes == 5


def test_reopen_build_can_change_inherit_or_be_cleared(repository):
    from game_test_agent.contracts.models import BuildRef

    parent, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective=Objective(objective_id="build", description="验证构建"),
            build_ref=BuildRef(id="build-old"),
        ),
        "build-parent",
    )
    inherited, _ = repository.reopen_investigation(
        parent.investigation_id, ReopenRequest(reason="继承构建", budget=Budget()), "inherit"
    )
    changed, _ = repository.reopen_investigation(
        parent.investigation_id,
        ReopenRequest(reason="新构建", budget=Budget(), build_ref=BuildRef(id="build-new")),
        "change",
    )
    cleared, _ = repository.reopen_investigation(
        parent.investigation_id,
        ReopenRequest(reason="不限定构建", budget=Budget(), build_ref=None),
        "clear",
    )
    assert inherited.request.build_ref.id == "build-old"
    assert changed.request.build_ref.id == "build-new"
    assert cleared.request.build_ref is None
    assert repository.get_investigation(parent.investigation_id).request.build_ref.id == "build-old"
