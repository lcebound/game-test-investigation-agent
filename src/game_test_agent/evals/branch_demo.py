"""Scripted integration demo for the branch driver and production selector.

This is deliberately a training-fixture policy, not a model benchmark policy.
"""

from game_test_agent.contracts.models import NextStep
from game_test_agent.evals.branching import BranchDecision, BranchRunConfig, BranchView
from game_test_agent.planning import CandidateSelectionService
from game_test_agent.storage import Repository


class ScriptedSelectorDemo:
    def decide(self, view: BranchView) -> BranchDecision:
        objective_id = view.context.investigation.request.objective.objective_id
        findings = view.observations[-1].findings
        conclusion = None
        if findings.get("error_code") == "ASSET_MISSING":
            conclusion = "asset"
        elif isinstance(findings.get("frame_ms"), (int, float)):
            conclusion = "gpu" if findings["frame_ms"] < 20 else "cpu"
        if conclusion:
            return BranchDecision(decision=NextStep(
                kind="report", objective_id=objective_id,
                evidence_ids=view.observations[-1].evidence_ids,
                expected_observation="Stop after the scripted fixture observation",
                reason_summary="Report the fixture-specific metric or error classification",
            ), conclusion=conclusion)
        if view.history and view.history[-1].response.status in {
            "unsupported", "unavailable", "budget_exhausted", "invalid",
        }:
            return BranchDecision(decision=NextStep(
                kind="report", objective_id=objective_id,
                expected_observation="No supported observation was obtained",
                reason_summary="Stop with unresolved diagnosis",
            ), conclusion="unresolved")
        proposed = next((item.operation for item in view.operations
                         if item.operation.decision.kind == "experiment"), None)
        if proposed is None:
            raise ValueError("demo requires a modeled experiment proposal")
        # choose() only needs a repository interface for this bounded selection;
        # no provider, action dispatch, artifact ingestion or model calls occur.
        repository = Repository(":memory:")
        try:
            selected, record = CandidateSelectionService(repository).choose(
                view.context, proposed.decision, plan_revision=len(view.history) + 1,
            )
        finally:
            repository.close()
        return BranchDecision(
            decision=selected,
            parameters=proposed.parameters if selected.kind == "experiment" else {},
            conclusion="unresolved" if selected.kind == "report" else None,
            selection=record,
        )


def create_demo_policy(config: BranchRunConfig) -> ScriptedSelectorDemo:
    if config != BranchRunConfig(
        policy_id="scripted-selector-demo-v1", model_name="scripted-fixture",
    ):
        raise ValueError("the demo adapter only supports its frozen scripted configuration")
    return ScriptedSelectorDemo()
