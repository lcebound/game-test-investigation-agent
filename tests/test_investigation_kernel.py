from __future__ import annotations

import asyncio

import pytest

from game_test_agent.capabilities import CapabilityRegistry
from game_test_agent.contracts.models import (
    ActionProposal,
    ActionSpec,
    ActionState,
    CapabilityDescriptor,
    ContextSnapshot,
    Hypothesis,
    InvestigationRequest,
    InvestigationStatus,
    NextStep,
    TestObjective as Objective,
)
from game_test_agent.evidence import ContextBuilder
from game_test_agent.planning import DurableActionPlanner
from game_test_agent.policies import PolicyEngine
from game_test_agent.runtime import InvestigationKernel
from game_test_agent.storage import ConflictError, payload_hash


class CountingActionPlanner:
    def __init__(self) -> None:
        self.calls = 0

    async def propose(self, _context, _decision):
        self.calls += 1
        return ActionProposal(
            capability_id="test.run",
            capability_version="1",
            provider_id="portable-provider",
            provider_revision="catalog-1",
            parameters={},
            expected_observation="冒烟测试结果",
        )


def test_kernel_freezes_context_and_persists_validated_decision(repository):
    investigation, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective=Objective(objective_id="smoke", description="执行冒烟测试"),
        ),
        "kernel-freeze",
    )
    registry = CapabilityRegistry(repository)
    registry.register(
        CapabilityDescriptor(
            capability_id="test.run",
            capability_version="1",
            provider_id="portable-provider",
            provider_revision="catalog-1",
            tool_name="submit",
            project_ids=["game-a"],
            input_schema={"type": "object"},
            risk_level="bounded_execution",
            execution_owner="portable-provider",
            resource_domain="pool",
        )
    )
    repository.transition(investigation.investigation_id, InvestigationStatus.DISCOVERING)
    repository.transition(investigation.investigation_id, InvestigationStatus.PLANNING)
    context = ContextBuilder(repository, registry).build(
        investigation.investigation_id, scopes=set()
    )
    kernel = InvestigationKernel(repository)

    prepared = kernel.prepare_round(
        context,
        plan_revision=1,
        model_name="test-model",
        prompt_version="investigator-v1",
    )
    assert not prepared.should_stop
    assert prepared.snapshot is not None
    assert repository.get_context_snapshot(prepared.snapshot.snapshot_id) == prepared.snapshot

    proposed = NextStep(
        kind="experiment",
        objective_id="smoke",
        capability_id="test.run",
        provider_id="portable-provider",
        expected_observation="冒烟测试结果",
        reason_summary="执行受控冒烟实验",
    )
    record = kernel.accept_decision(prepared.snapshot, proposed)
    assert record.decision == proposed
    assert repository.get_decision_record(investigation.investigation_id, 1) == record
    assert kernel.accept_decision(prepared.snapshot, proposed) == record


def test_kernel_rejects_decision_when_investigation_changed_after_snapshot(repository):
    investigation, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective=Objective(objective_id="stale", description="验证旧决策拒绝"),
        ),
        "kernel-stale",
    )
    registry = CapabilityRegistry(repository)
    repository.transition(investigation.investigation_id, InvestigationStatus.DISCOVERING)
    repository.transition(investigation.investigation_id, InvestigationStatus.PLANNING)
    context = ContextBuilder(repository, registry).build(
        investigation.investigation_id, scopes=set()
    )
    kernel = InvestigationKernel(repository)
    prepared = kernel.prepare_round(
        context,
        plan_revision=1,
        model_name="test-model",
        prompt_version="investigator-v1",
    )
    assert prepared.snapshot is not None
    repository.transition(investigation.investigation_id, InvestigationStatus.ANALYZING)

    with pytest.raises(ConflictError, match="changed after"):
        kernel.accept_decision(
            prepared.snapshot,
            NextStep(
                kind="report",
                objective_id="stale",
                expected_observation="生成报告",
                reason_summary="状态已改变",
            ),
        )


def test_kernel_rejects_decision_when_authoritative_ledger_changed(repository):
    investigation, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective=Objective(objective_id="ledger-stale", description="验证账本并发"),
        ),
        "kernel-ledger-stale",
    )
    registry = CapabilityRegistry(repository)
    context = ContextBuilder(repository, registry).build(
        investigation.investigation_id, scopes=set()
    )
    kernel = InvestigationKernel(repository)
    prepared = kernel.prepare_round(
        context,
        plan_revision=1,
        model_name="test-model",
        prompt_version="investigator-v1",
    )
    assert prepared.snapshot is not None
    repository.save_hypothesis(
        Hypothesis(
            hypothesis_id="hyp-concurrent",
            investigation_id=investigation.investigation_id,
            revision=1,
            statement="并发写入改变了调查账本",
            scope="current-build",
            falsifiable_prediction="新证据能够区分该假设",
        )
    )

    with pytest.raises(ConflictError, match="authoritative investigation state changed"):
        kernel.accept_decision(
            prepared.snapshot,
            NextStep(
                kind="report",
                objective_id="ledger-stale",
                expected_observation="生成报告",
                reason_summary="该决策基于旧账本",
            ),
        )


def test_kernel_reuses_legacy_snapshot_during_mixed_version_retry(repository):
    investigation, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective=Objective(objective_id="legacy-snapshot", description="恢复旧快照"),
        ),
        "kernel-legacy-snapshot",
    )
    registry = CapabilityRegistry(repository)
    context = ContextBuilder(repository, registry).build(
        investigation.investigation_id, scopes=set()
    )
    authorization = repository.get_authorization(investigation.investigation_id)
    legacy = ContextSnapshot(
        snapshot_id="ctx-legacy-mixed-version",
        investigation_id=investigation.investigation_id,
        state_revision=investigation.revision,
        plan_revision=1,
        context_hash=payload_hash(context),
        authorization_hash=payload_hash(
            {
                "principal_id": authorization["principal_id"],
                "scopes": sorted(authorization["scopes"]),
                "policy_profile": authorization["policy_profile"],
            }
        ),
        model_name="test-model",
        prompt_version="investigator-v1",
        tool_schema_version="capability-descriptor-v2",
        context=context,
    )
    repository.save_context_snapshot(legacy)

    prepared = InvestigationKernel(repository).prepare_round(
        context,
        plan_revision=1,
        model_name="test-model",
        prompt_version="investigator-v1",
    )
    assert prepared.snapshot == legacy


def test_durable_action_planner_reuses_frozen_action_on_retry(repository):
    investigation, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective=Objective(objective_id="durable", description="验证动作规划恢复"),
        ),
        "durable-action-planning",
    )
    registry = CapabilityRegistry(repository)
    registry.register(
        CapabilityDescriptor(
            capability_id="test.run",
            capability_version="1",
            provider_id="portable-provider",
            provider_revision="catalog-1",
            tool_name="submit",
            project_ids=["game-a"],
            input_schema={"type": "object"},
            risk_level="bounded_execution",
            execution_owner="portable-provider",
            resource_domain="pool",
        )
    )
    repository.transition(investigation.investigation_id, InvestigationStatus.DISCOVERING)
    repository.transition(investigation.investigation_id, InvestigationStatus.PLANNING)
    context = ContextBuilder(repository, registry).build(
        investigation.investigation_id, scopes=set()
    )
    kernel = InvestigationKernel(repository)
    prepared = kernel.prepare_round(
        context,
        plan_revision=1,
        model_name="test-model",
        prompt_version="investigator-v1",
    )
    assert prepared.snapshot is not None
    decision = kernel.accept_decision(
        prepared.snapshot,
        NextStep(
            kind="experiment",
            objective_id="durable",
            capability_id="test.run",
            provider_id="portable-provider",
            expected_observation="冒烟测试结果",
            reason_summary="执行受控实验",
        ),
    )
    delegate = CountingActionPlanner()
    planner = DurableActionPlanner(repository, delegate)

    first = asyncio.run(planner.plan(prepared.snapshot.context, decision))
    second = asyncio.run(planner.plan(prepared.snapshot.context, decision))
    assert first == second
    assert delegate.calls == 1
    assert repository.get_planned_action(investigation.investigation_id, 1) == first


def test_progress_fingerprint_tracks_authoritative_action_state(repository):
    investigation, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective=Objective(objective_id="progress", description="验证动作进展"),
        ),
        "kernel-progress",
    )
    registry = CapabilityRegistry(repository)
    registry.register(
        CapabilityDescriptor(
            capability_id="test.run",
            capability_version="1",
            provider_id="portable-provider",
            provider_revision="catalog-1",
            tool_name="submit",
            project_ids=["game-a"],
            input_schema={"type": "object"},
            risk_level="bounded_execution",
            execution_owner="portable-provider",
            resource_domain="pool",
        )
    )
    kernel = InvestigationKernel(repository)
    before_reservation = kernel.progress_fingerprint(investigation.investigation_id)
    reservation = PolicyEngine(repository, registry).reserve(
        ActionSpec(
            tenant_id="studio-a",
            project_id="game-a",
            investigation_id=investigation.investigation_id,
            action_id="act-progress",
            idempotency_key="kernel-progress/act-progress",
            capability_id="test.run",
            capability_version="1",
            provider_id="portable-provider",
            parameters={},
            expected_observation="运行状态推进",
            plan_revision=1,
        ),
        principal_id="system",
        scopes=set(),
    )
    after_reservation = kernel.progress_fingerprint(investigation.investigation_id)
    repository.transition_action(
        reservation.action.action_id,
        {ActionState.RESERVED},
        ActionState.DISPATCHING,
    )
    after_dispatch = kernel.progress_fingerprint(investigation.investigation_id)
    context = ContextBuilder(repository, registry).build(
        investigation.investigation_id, scopes=set()
    )

    assert before_reservation != after_reservation
    assert after_reservation != after_dispatch
    assert [item.action_id for item in context.working_state.in_flight_actions] == ["act-progress"]
    assert context.working_state.budget_remaining["real_runs"] == 2
    assert "query_run" in context.working_state.next_candidates

    tiny_context = ContextBuilder(repository, registry).build(
        investigation.investigation_id,
        scopes=set(),
        window_limit_tokens=64,
        reserved_output_tokens=1,
    )
    assert tiny_context.context_window.over_budget is True
    assert tiny_context.actions == []
    assert tiny_context.context_window.omitted_records["actions"] == 1
    assert [item.action_id for item in tiny_context.working_state.in_flight_actions] == [
        "act-progress"
    ]
    assert "mandatory_context_exceeds_token_budget" in tiny_context.limitations
