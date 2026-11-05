from __future__ import annotations

import pytest

from game_test_agent.capabilities import CapabilityRegistry
from game_test_agent.contracts.models import (
    Hypothesis,
    HypothesisDelta,
    InvestigationRequest,
    NextStep,
    PlanDelta,
    PlanStep,
    PlanStepReplacement,
    PlanStepStatusUpdate,
    TestObjective as Objective,
)
from game_test_agent.evidence import ContextBuilder
from game_test_agent.runtime import InvestigationKernel
from game_test_agent.storage import ConflictError


def _prepare(repository, registry, investigation_id: str, round_number: int):
    context = ContextBuilder(repository, registry).build(investigation_id, scopes=set())
    prepared = InvestigationKernel(repository).prepare_round(
        context,
        plan_revision=round_number,
        model_name="test-model",
        prompt_version="investigator-v1",
    )
    assert prepared.snapshot is not None
    return prepared.snapshot


def _report(objective_id: str, *, plan_delta=None, hypothesis_delta=None) -> NextStep:
    return NextStep(
        kind="report",
        objective_id=objective_id,
        expected_observation="保存调查账本变更",
        reason_summary="更新计划或假设后形成有边界的报告",
        plan_delta=plan_delta,
        hypothesis_delta=hypothesis_delta,
    )


def test_plan_deltas_are_versioned_idempotent_and_preserve_executed_steps(repository):
    investigation, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective=Objective(objective_id="delta-plan", description="验证计划增量"),
        ),
        "delta-plan",
    )
    registry = CapabilityRegistry(repository)
    kernel = InvestigationKernel(repository)
    first_snapshot = _prepare(repository, registry, investigation.investigation_id, 1)
    first_decision = _report(
        "delta-plan",
        plan_delta=PlanDelta(
            base_revision=0,
            added_steps=[
                PlanStep(
                    step_id="analyze-existing",
                    objective_id="delta-plan",
                    action_type="analyze",
                )
            ],
            reason="先分析已有材料",
        ),
    )
    first_record = kernel.accept_decision(first_snapshot, first_decision)
    assert repository.get_latest_plan(investigation.investigation_id).revision == 1
    assert kernel.accept_decision(first_snapshot, first_decision) == first_record

    ready_snapshot = _prepare(repository, registry, investigation.investigation_id, 2)
    kernel.accept_decision(
        ready_snapshot,
        _report(
            "delta-plan",
            plan_delta=PlanDelta(
                base_revision=1,
                status_updates=[
                    PlanStepStatusUpdate(
                        step_id="analyze-existing",
                        expected_status="pending",
                        target_status="ready",
                    )
                ],
                reason="前置条件已经满足",
            ),
        ),
    )
    ready_plan = repository.get_latest_plan(investigation.investigation_id)
    running_plan = ready_plan.model_copy(
        update={
            "revision": 3,
            "steps": [ready_plan.steps[0].model_copy(update={"status": "running"})],
        }
    )
    repository.save_plan(running_plan, "system-execution-projection")
    assert repository.get_latest_plan(investigation.investigation_id).steps[0].status == "running"

    replacement_snapshot = _prepare(repository, registry, investigation.investigation_id, 3)
    with pytest.raises(ValueError, match="executed plan steps cannot be replaced"):
        kernel.accept_decision(
            replacement_snapshot,
            _report(
                "delta-plan",
                plan_delta=PlanDelta(
                    base_revision=3,
                    replacements=[
                        PlanStepReplacement(
                            step_id="analyze-existing",
                            replacement=PlanStep(
                                step_id="analyze-existing",
                                objective_id="delta-plan",
                                action_type="report",
                            ),
                        )
                    ],
                    reason="尝试改写已经执行的步骤",
                ),
            ),
        )
    assert repository.get_decision_record(investigation.investigation_id, 3) is None


def test_hypothesis_deltas_require_monotonic_revisions(repository):
    investigation, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective=Objective(objective_id="delta-hyp", description="验证假设增量"),
        ),
        "delta-hypothesis",
    )
    registry = CapabilityRegistry(repository)
    kernel = InvestigationKernel(repository)
    hypothesis = Hypothesis(
        hypothesis_id="hyp-thread-contention",
        investigation_id=investigation.investigation_id,
        revision=1,
        statement="主线程竞争导致尖峰",
        scope="current-build",
        missing_evidence=["thread trace"],
        falsifiable_prediction="消除竞争后尖峰下降",
    )
    snapshot = _prepare(repository, registry, investigation.investigation_id, 1)
    decision = _report(
        "delta-hyp",
        hypothesis_delta=HypothesisDelta(
            hypotheses=[hypothesis],
            reason="登记可证伪假设",
        ),
    )
    record = kernel.accept_decision(snapshot, decision)
    assert repository.list_hypotheses(investigation.investigation_id) == [hypothesis]
    assert kernel.accept_decision(snapshot, decision) == record

    stale_snapshot = _prepare(repository, registry, investigation.investigation_id, 2)
    with pytest.raises(ConflictError, match="reused with different content"):
        kernel.accept_decision(
            stale_snapshot,
            _report(
                "delta-hyp",
                hypothesis_delta=HypothesisDelta(
                    hypotheses=[
                        hypothesis.model_copy(update={"statement": "同 revision 的不同内容"})
                    ],
                    reason="错误复用 revision",
                ),
            ),
        )
    assert repository.get_decision_record(investigation.investigation_id, 2) is None
