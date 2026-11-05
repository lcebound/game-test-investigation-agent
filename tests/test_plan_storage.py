from __future__ import annotations

import pytest

from game_test_agent.contracts.models import (
    InvestigationRequest,
    PlanStep,
    TestObjective as Objective,
    TestPlan as Plan,
)
from game_test_agent.storage import ConflictError


def test_plan_revisions_are_immutable(repository):
    investigation, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective=Objective(objective_id="o", description="测试计划"),
        ),
        "plan-storage",
    )
    plan = Plan(
        investigation_id=investigation.investigation_id,
        revision=1,
        steps=[
            PlanStep(
                step_id="report",
                objective_id="o",
                action_type="report",
            )
        ],
    )
    digest = repository.save_plan(plan, "snapshot-1")
    assert repository.save_plan(plan, "snapshot-1") == digest
    changed = plan.model_copy(
        update={"steps": [PlanStep(step_id="analyze", objective_id="o", action_type="analyze")]}
    )
    with pytest.raises(ConflictError, match="immutable"):
        repository.save_plan(changed, "snapshot-1")
