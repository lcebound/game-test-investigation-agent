from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from game_test_agent.contracts.models import (
    ConclusionLevel,
    ContextSnapshot,
    DecisionOutcomeRecord,
    DecisionRecord,
    NextStep,
)
from game_test_agent.provenance import ProvenanceIndex
from game_test_agent.storage import NotFoundError, Repository, payload_hash


@dataclass(frozen=True, slots=True)
class ReplayBundle:
    investigation_id: str
    snapshots: tuple[ContextSnapshot, ...]
    decisions: tuple[DecisionRecord, ...]
    outcomes: tuple[DecisionOutcomeRecord, ...]
    events: tuple[dict, ...]


@dataclass(frozen=True, slots=True)
class IntegrityReport:
    investigation_id: str
    complete: bool
    issues: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BranchSimulationResult:
    snapshot_id: str
    candidate: NextStep
    simulated_outcome: dict
    warning: str = "simulation_does_not_establish_a_real_world_outcome"


class ReadOnlyReplayService:
    """Reconstruct and compare decisions without any platform execution dependency."""

    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def state_replay(self, investigation_id: str) -> ReplayBundle:
        snapshots = tuple(self.repository.list_context_snapshots(investigation_id))
        decisions = tuple(self.repository.list_decision_records(investigation_id))
        outcomes = tuple(self.repository.list_decision_outcomes(investigation_id))
        events = tuple(self.repository.list_events(investigation_id, limit=500))
        return ReplayBundle(investigation_id, snapshots, decisions, outcomes, events)

    async def decision_replay(
        self,
        snapshot_id: str,
        reasoner: Callable[[dict], NextStep | Awaitable[NextStep]],
    ) -> NextStep:
        snapshot = self.repository.get_context_snapshot(snapshot_id)
        candidate = reasoner(snapshot.context.model_dump(mode="json"))
        if hasattr(candidate, "__await__"):
            candidate = await candidate  # type: ignore[assignment,misc]
        if not isinstance(candidate, NextStep):
            candidate = NextStep.model_validate(candidate)
        # Returning a candidate is deliberate: this service cannot accept or execute it.
        return candidate

    async def branch_simulation(
        self,
        snapshot_id: str,
        reasoner: Callable[[dict], NextStep | Awaitable[NextStep]],
        simulator: Callable[[dict, NextStep], dict | Awaitable[dict]],
    ) -> BranchSimulationResult:
        snapshot = self.repository.get_context_snapshot(snapshot_id)
        candidate = await self.decision_replay(snapshot_id, reasoner)
        outcome = simulator(snapshot.context.model_dump(mode="json"), candidate)
        if hasattr(outcome, "__await__"):
            outcome = await outcome  # type: ignore[assignment,misc]
        if not isinstance(outcome, dict):
            raise TypeError("branch simulator must return an object")
        return BranchSimulationResult(snapshot_id, candidate, outcome)

    def integrity(self, investigation_id: str) -> IntegrityReport:
        bundle = self.state_replay(investigation_id)
        snapshots = {item.snapshot_id: item for item in bundle.snapshots}
        decisions_by_revision = {item.plan_revision: item for item in bundle.decisions}
        decisions_by_id = {item.decision_id: item for item in bundle.decisions}
        issues: list[str] = []
        for decision in bundle.decisions:
            snapshot = snapshots.get(decision.context_snapshot_id)
            if snapshot is None:
                issues.append(f"decision {decision.decision_id} has no context snapshot")
                continue
            if payload_hash(snapshot.context) != snapshot.context_hash:
                issues.append(f"snapshot {snapshot.snapshot_id} failed its context hash")
            if payload_hash(decision.decision) != decision.decision_hash:
                issues.append(f"decision {decision.decision_id} failed its decision hash")
            if decision.envelope is None:
                issues.append(f"decision {decision.decision_id} has no decision envelope")
        for outcome in bundle.outcomes:
            decision = decisions_by_id.get(outcome.decision_id)
            if decision is None:
                issues.append(f"outcome {outcome.decision_id}/{outcome.revision} has no decision")
            elif decision.investigation_id != outcome.investigation_id:
                issues.append(
                    f"outcome {outcome.decision_id}/{outcome.revision} has mismatched scope"
                )
        issues.extend(self.repository.decision_outcome_integrity_issues(investigation_id))
        for action in self.repository.list_actions(investigation_id):
            action_data = action["action"]
            revision = int(action_data["plan_revision"])
            if revision not in decisions_by_revision:
                issues.append(
                    f"action {action_data['action_id']} has no accepted decision for revision {revision}"
                )
            plan_step_id = action_data.get("plan_step_id")
            plan_node_revision = action_data.get("plan_node_revision")
            if (plan_step_id is None) != (plan_node_revision is None):
                issues.append(f"action {action_data['action_id']} has an incomplete plan binding")
            elif plan_step_id is not None:
                try:
                    plan = self.repository.get_plan(investigation_id, int(plan_node_revision))
                except NotFoundError:
                    issues.append(
                        f"action {action_data['action_id']} references missing plan revision "
                        f"{plan_node_revision}"
                    )
                else:
                    step = next((item for item in plan.steps if item.step_id == plan_step_id), None)
                    if step is None:
                        issues.append(
                            f"action {action_data['action_id']} references missing plan step "
                            f"{plan_step_id}"
                        )
                    elif (
                        step.action_type != "experiment"
                        or step.capability_id != action_data["capability_id"]
                        or step.capability_version != action_data["capability_version"]
                        or step.provider_id != action_data["provider_id"]
                    ):
                        issues.append(
                            f"action {action_data['action_id']} does not match its plan step"
                        )
            permit = self.repository.get_action_permit(action_data["action_id"])
            if permit.admission_ticket_id is not None and (
                permit.plan_step_id != plan_step_id
                or permit.plan_node_revision != plan_node_revision
            ):
                issues.append(
                    f"action {action_data['action_id']} admission does not match its plan step"
                )
        evidence_ids = {
            item.evidence_id for item in self.repository.list_current_evidence(investigation_id)
        }
        try:
            report, _markdown, _digest = self.repository.get_latest_report(investigation_id)
        except NotFoundError:
            report = None
        if report is not None:
            provenance = ProvenanceIndex(self.repository)
            graph = provenance.build(investigation_id)
            verified_levels = {
                ConclusionLevel.REPRODUCED,
                ConclusionLevel.BOTTLENECK_SUPPORTED,
                ConclusionLevel.ROOT_CAUSE_CONFIRMED,
            }
            for claim in report.claims:
                if not claim.evidence_ids:
                    issues.append(f"report claim {claim.claim_id} has no evidence")
                for evidence_id in claim.evidence_ids:
                    if evidence_id not in evidence_ids:
                        issues.append(
                            f"report claim {claim.claim_id} references missing evidence {evidence_id}"
                        )
                claim_node_id = f"claim:{investigation_id}:{report.revision}:{claim.claim_id}"
                sources = set(provenance.sources(investigation_id, claim_node_id))
                expected_sources = set(claim.evidence_ids + claim.fact_ids + claim.assessment_ids)
                missing_sources = expected_sources - sources
                if missing_sources:
                    issues.append(
                        f"report claim {claim.claim_id} has incomplete provenance: "
                        f"{sorted(missing_sources)}"
                    )
                node = graph.nodes.get(claim_node_id)
                if node is None:
                    issues.append(f"report claim {claim.claim_id} has no provenance node")
                elif node.stale:
                    issues.append(f"report claim {claim.claim_id} depends on stale input")
                if report.conclusion_level in verified_levels:
                    if not claim.fact_ids:
                        issues.append(f"verified report claim {claim.claim_id} has no fact lineage")
                    if not claim.assessment_ids:
                        issues.append(
                            f"verified report claim {claim.claim_id} has no assessment lineage"
                        )
        return IntegrityReport(investigation_id, not issues, tuple(issues))
