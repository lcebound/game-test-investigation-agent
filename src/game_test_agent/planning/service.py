from __future__ import annotations

import hashlib

from game_test_agent.contracts.models import (
    ActionProposal,
    ContextBundle,
    DecisionRecord,
    PlannedAction,
)
from game_test_agent.planning.action_planner import ActionFactory, ActionPlanner
from game_test_agent.planning.runtime import PlanRuntimeProjector
from game_test_agent.planning.validator import PlanValidator
from game_test_agent.storage import Repository, canonical_json, payload_hash


class PlanBudgetExceeded(ValueError):
    """A deterministic resource rejection, safe to expose without model/provider text."""

    def __init__(self, required: float, limit: float) -> None:
        self.required = required
        self.limit = limit
        self.stop_reason = (
            f"plan_budget_rejected:device_minutes:required={required:g}:limit={limit:g}"
        )
        super().__init__(self.stop_reason)


class PlanCorrectionRequired(ValueError):
    code = "FAILED_EXPERIMENT_REQUIRES_PLAN_DELTA"
    instruction = (
        "The previous experiment failed. Read its logs before retrying. "
        "To run a new experiment, supply a plan_delta based on the current plan revision "
        "that adds a new pending experiment step without replacing executed steps. "
        "Alternatively retrieve_evidence, request_input, or report the remaining gap."
    )


class DurableActionPlanner:
    """Freezes one proposal and normalized action before any external submission."""

    def __init__(self, repository: Repository, planner: ActionPlanner) -> None:
        self.repository = repository
        self.planner = planner

    async def plan(
        self, context: ContextBundle, decision: DecisionRecord, *, ordinal: int = 1
    ) -> PlannedAction:
        existing = self.repository.get_planned_action(
            decision.investigation_id, decision.plan_revision
        )
        if existing:
            if existing.decision_id != decision.decision_id:
                raise ValueError("stored planned action belongs to another decision")
            return existing
        audited = getattr(self.planner, "propose_audited", None)
        if audited is None:
            proposal = await self.planner.propose(context, decision.decision)
            model_audit = None
        else:
            proposal, model_audit = await audited(context, decision.decision)
        execution_context = self._execution_context(context, decision, proposal)
        action = ActionFactory.create(
            execution_context,
            proposal,
            plan_revision=decision.plan_revision,
            ordinal=ordinal,
        )
        request = context.investigation.request
        # A user-created investigation is also an authoritative lineage root.
        # Platform adapters must not invent different roots or classify it as a platform run.
        action = action.model_copy(
            update={
                "constraints": {
                    **action.constraints,
                    "root_event_id": request.root_event_id or context.investigation.workflow_id,
                    "causation_id": decision.decision_id,
                    "investigation_depth": 1,
                }
            }
        )
        proposal_hash = payload_hash(proposal)
        action_hash = payload_hash(action)
        identity = {
            "investigation_id": decision.investigation_id,
            "plan_revision": decision.plan_revision,
            "decision_id": decision.decision_id,
            "proposal_hash": proposal_hash,
            "action_hash": action_hash,
        }
        planned = PlannedAction(
            planning_id="planning-"
            + hashlib.sha256(canonical_json(identity).encode()).hexdigest()[:32],
            investigation_id=decision.investigation_id,
            plan_revision=decision.plan_revision,
            decision_id=decision.decision_id,
            proposal=proposal,
            proposal_hash=proposal_hash,
            action=action,
            action_hash=action_hash,
            model_profile_id=(model_audit.model_profile_id if model_audit is not None else None),
            model_profile_version=(
                model_audit.model_profile_version if model_audit is not None else None
            ),
            raw_output_ref=(model_audit.raw_output_ref if model_audit is not None else None),
            normalization_log=(model_audit.normalization_log if model_audit is not None else []),
            attempts=(model_audit.attempts if model_audit is not None else []),
            usage=(model_audit.usage if model_audit is not None else {}),
        )
        persisted, _ = self.repository.save_planned_action(planned)
        return persisted

    def _execution_context(
        self, context: ContextBundle, decision: DecisionRecord, proposal: ActionProposal
    ) -> ContextBundle:
        """Bind against the accepted plan delta without mutating the reasoning snapshot.

        Each additional experiment gets a new node. A deterministic node ID makes
        recovery after saving the plan but before saving the action idempotent.
        """
        investigation_id = decision.investigation_id
        latest = self.repository.get_latest_plan(investigation_id)
        if latest is None:
            return context
        base_revision = context.plan.revision if context.plan else 0
        if decision.decision.plan_delta is not None:
            base_revision = decision.decision.plan_delta.base_revision + 1
        node_id = f"experiment-{decision.decision_id}"
        recovering = any(step.step_id == node_id for step in latest.steps)
        if latest.revision != base_revision and not (
            recovering and latest.revision == base_revision + 1
        ):
            raise ValueError("execution plan changed after the accepted decision")
        plan = PlanRuntimeProjector().project(
            latest,
            actions=self.repository.list_actions(investigation_id),
            assessment=self.repository.latest_assessment(investigation_id),
            has_user_input=bool(self.repository.list_user_inputs(investigation_id)),
            report_published=False,
        )
        matching = [
            step
            for step in plan.steps
            if step.action_type == "experiment"
            and step.capability_id == proposal.capability_id
            and step.capability_version == proposal.capability_version
            and step.provider_id == proposal.provider_id
        ]
        available = [step for step in matching if step.status in {"pending", "ready"}]
        if not available and matching and not recovering:
            if any(step.status == "running" for step in matching):
                raise ValueError("existing experiment is still in flight; query it first")
            if not decision.decision.evidence_ids:
                raise ValueError("an additional experiment requires evidence-backed justification")
            completed = [step for step in matching if step.status == "completed"]
            if not completed:
                raise PlanCorrectionRequired("failed experiment requires an explicit plan delta")
            previous = completed[-1]
            new_step = previous.model_copy(
                update={
                    "step_id": node_id,
                    "dependencies": [previous.step_id],
                    "input_bindings": [],
                    "status": "pending",
                }
            )
            # Preserve executed definitions; future evaluation must wait for the
            # additional observation rather than the already completed run.
            steps = []
            for step in latest.steps:
                if step.action_type == "evaluate" and previous.step_id in step.dependencies:
                    step = step.model_copy(
                        update={
                            "dependencies": [
                                node_id if parent == previous.step_id else parent
                                for parent in step.dependencies
                            ],
                            "input_bindings": [
                                binding.model_copy(update={"source_step_id": node_id})
                                if binding.source_step_id == previous.step_id
                                else binding
                                for binding in step.input_bindings
                            ],
                            "status": "pending",
                        }
                    )
                steps.append(step)
            extended = latest.model_copy(
                update={
                    "revision": latest.revision + 1,
                    "steps": [*steps, new_step],
                }
            )
            validation = PlanValidator().validate(
                extended,
                context.capabilities,
                max_device_minutes=context.investigation.request.budget.max_device_minutes,
            )
            if validation.budget_exceeded:
                raise PlanBudgetExceeded(
                    validation.required_device_minutes, validation.device_minute_limit
                )
            if not validation.accepted:
                raise ValueError(
                    "additional experiment plan rejected: " + "; ".join(validation.errors)
                )
            self.repository.save_plan(extended, payload_hash(context.capabilities))
            plan = PlanRuntimeProjector().project(
                extended,
                actions=self.repository.list_actions(investigation_id),
                assessment=self.repository.latest_assessment(investigation_id),
                has_user_input=bool(self.repository.list_user_inputs(investigation_id)),
                report_published=False,
            )
        return context.model_copy(update={"plan": plan})
