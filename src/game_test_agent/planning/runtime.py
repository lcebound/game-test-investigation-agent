from __future__ import annotations

from game_test_agent.contracts.models import CriterionAssessment, TestPlan


class PlanRuntimeProjector:
    """Projects immutable plan definitions over authoritative runtime ledgers."""

    _ACTIVE_ACTION_STATES = {"reserved", "dispatching", "accepted", "waiting", "unknown"}
    _FAILED_ACTION_STATES = {"failed", "canceled"}

    def project(
        self,
        plan: TestPlan | None,
        *,
        actions: list[dict],
        assessment: CriterionAssessment | None,
        has_user_input: bool,
        report_published: bool,
    ) -> TestPlan | None:
        if plan is None:
            return None
        statuses = {item.step_id: item.status for item in plan.steps}
        actions_by_step: dict[str, list[str]] = {}
        for entry in actions:
            action = entry.get("action", {})
            revision = action.get("plan_node_revision")
            if revision is None or revision > plan.revision:
                continue
            step_id = action.get("plan_step_id")
            if step_id:
                actions_by_step.setdefault(str(step_id), []).append(str(entry.get("state")))

        for step in plan.steps:
            states = actions_by_step.get(step.step_id, [])
            if "succeeded" in states:
                statuses[step.step_id] = "completed"
            elif any(state in self._ACTIVE_ACTION_STATES for state in states):
                statuses[step.step_id] = "running"
            elif states and all(state in self._FAILED_ACTION_STATES for state in states):
                retryable = (
                    step.failure_handling.strategy == "retry"
                    and len(states) < step.failure_handling.max_attempts
                )
                statuses[step.step_id] = "ready" if retryable else "failed"
            elif step.action_type == "request_input" and has_user_input:
                statuses[step.step_id] = "completed"

        changed = True
        while changed:
            changed = False
            for step in plan.steps:
                dependencies_complete = all(
                    statuses.get(parent) in {"completed", "skipped"} for parent in step.dependencies
                )
                target = statuses[step.step_id]
                if (
                    step.action_type == "evaluate"
                    and assessment is not None
                    and dependencies_complete
                ):
                    target = "completed"
                elif step.action_type == "report" and report_published and dependencies_complete:
                    target = "completed"
                elif target == "pending" and dependencies_complete:
                    target = "ready"
                if target != statuses[step.step_id]:
                    statuses[step.step_id] = target
                    changed = True

        return plan.model_copy(
            update={
                "steps": [
                    step.model_copy(update={"status": statuses[step.step_id]})
                    for step in plan.steps
                ]
            }
        )
