from __future__ import annotations

from game_test_agent.contracts.models import (
    ContextBundle,
    Hypothesis,
    HypothesisDelta,
    NextStep,
    PlanDelta,
    TestPlan,
)
from game_test_agent.storage import ConflictError, Repository, payload_hash

from .validator import PlanValidator


class InvestigationDeltaService:
    """Validates and applies model-proposed ledger deltas with optimistic revisions."""

    _STATUS_TRANSITIONS = {
        "pending": {"ready", "skipped"},
        "ready": {"skipped"},
        "running": set(),
    }

    def __init__(self, repository: Repository) -> None:
        from game_test_agent.evidence.ledger import HypothesisLedger

        self.repository = repository
        self.plan_validator = PlanValidator()
        self.hypothesis_ledger = HypothesisLedger(repository)

    def validate(self, context: ContextBundle, decision: NextStep) -> None:
        if decision.plan_delta is not None:
            self._prepare_plan(context, decision.plan_delta)
        if decision.hypothesis_delta is not None:
            self._prepare_hypotheses(context, decision.hypothesis_delta)

    def apply(self, context: ContextBundle, decision: NextStep) -> None:
        if decision.plan_delta is not None:
            plan = self._prepare_plan(context, decision.plan_delta)
            capability_hash = payload_hash(
                sorted(
                    (item.model_dump(mode="json", by_alias=True) for item in context.capabilities),
                    key=lambda item: (
                        item["provider_id"],
                        item["capability_id"],
                        item["capability_version"],
                    ),
                )
            )
            self.repository.save_plan(plan, capability_hash)
        if decision.hypothesis_delta is not None:
            for hypothesis in self._prepare_hypotheses(context, decision.hypothesis_delta):
                self.hypothesis_ledger.apply(hypothesis)

    def _prepare_plan(self, context: ContextBundle, delta: PlanDelta) -> TestPlan:
        latest = self.repository.get_latest_plan(context.investigation.investigation_id)
        latest_revision = latest.revision if latest else 0
        if latest_revision == delta.base_revision:
            base = latest
        elif latest_revision == delta.base_revision + 1:
            base = (
                self.repository.get_plan(
                    context.investigation.investigation_id, delta.base_revision
                )
                if delta.base_revision
                else None
            )
        else:
            raise ConflictError("plan delta is based on a stale revision")

        candidate = self._merge_plan(
            context.investigation.investigation_id,
            base,
            delta,
        )
        objective_id = context.investigation.request.objective.objective_id
        if any(item.objective_id != objective_id for item in candidate.steps):
            raise ValueError("plan delta contains a step outside the investigation objective")
        if latest_revision == delta.base_revision + 1:
            if latest != candidate:
                raise ConflictError("plan delta conflicts with the persisted next revision")
            return latest
        validation = self.plan_validator.validate(
            candidate,
            context.capabilities,
            max_device_minutes=float(context.budget["max_device_minutes"]),
        )
        if not validation.accepted:
            raise ValueError("invalid plan delta: " + "; ".join(validation.errors))
        return candidate

    def _merge_plan(
        self,
        investigation_id: str,
        base: TestPlan | None,
        delta: PlanDelta,
    ) -> TestPlan:
        steps = list(base.steps if base else [])
        by_id = {item.step_id: item for item in steps}
        executed_ids = {
            item["action"].get("plan_step_id")
            for item in self.repository.list_actions(investigation_id)
        }
        added_ids = [item.step_id for item in delta.added_steps]
        replacement_ids = [item.step_id for item in delta.replacements]
        update_ids = [item.step_id for item in delta.status_updates]
        for name, identities in {
            "added": added_ids,
            "replacement": replacement_ids,
            "status update": update_ids,
        }.items():
            if len(identities) != len(set(identities)):
                raise ValueError(f"plan delta has duplicate {name} step IDs")
        if set(replacement_ids) & set(update_ids):
            raise ValueError("a plan step cannot be replaced and status-updated together")
        if set(added_ids) & by_id.keys():
            raise ValueError("plan delta cannot add an existing step ID")
        for step in delta.added_steps:
            if step.status not in {"pending", "ready"}:
                raise ValueError("new plan steps must be unexecuted")
            by_id[step.step_id] = step
            steps.append(step)

        for replacement in delta.replacements:
            if replacement.step_id in executed_ids:
                raise ValueError("executed plan steps cannot be replaced")
            current = by_id.get(replacement.step_id)
            if current is None:
                raise ValueError(f"replacement step {replacement.step_id!r} does not exist")
            if current.status not in {"pending", "ready"}:
                raise ValueError("executed plan steps cannot be replaced")
            index = next(
                index for index, item in enumerate(steps) if item.step_id == replacement.step_id
            )
            steps[index] = replacement.replacement
            by_id[replacement.step_id] = replacement.replacement

        for update in delta.status_updates:
            if update.step_id in executed_ids:
                raise ValueError("executed plan step status is owned by the runtime")
            current = by_id.get(update.step_id)
            if current is None:
                raise ValueError(f"status update step {update.step_id!r} does not exist")
            if current.status != update.expected_status:
                raise ConflictError("plan step status changed before delta application")
            allowed = self._STATUS_TRANSITIONS.get(current.status, set())
            if update.target_status not in allowed:
                raise ValueError(
                    f"invalid plan step transition: {current.status} -> {update.target_status}"
                )
            changed = current.model_copy(update={"status": update.target_status})
            index = next(
                index for index, item in enumerate(steps) if item.step_id == update.step_id
            )
            steps[index] = changed
            by_id[update.step_id] = changed

        return TestPlan(
            investigation_id=investigation_id,
            revision=delta.base_revision + 1,
            steps=steps,
        )

    def _prepare_hypotheses(
        self,
        context: ContextBundle,
        delta: HypothesisDelta,
    ) -> list[Hypothesis]:
        investigation_id = context.investigation.investigation_id
        current = {
            item.hypothesis_id: item for item in self.repository.list_hypotheses(investigation_id)
        }
        pending: list[Hypothesis] = []
        for hypothesis in delta.hypotheses:
            if hypothesis.investigation_id != investigation_id:
                raise ValueError("hypothesis delta is outside the investigation")
            existing = current.get(hypothesis.hypothesis_id)
            if existing is None:
                if hypothesis.revision != 1:
                    raise ConflictError("new hypotheses must start at revision 1")
            elif hypothesis.revision == existing.revision:
                if hypothesis != existing:
                    raise ConflictError("hypothesis revision was reused with different content")
                continue
            elif hypothesis.revision != existing.revision + 1:
                raise ConflictError("hypothesis delta is based on a stale revision")
            self.hypothesis_ledger.validate(hypothesis)
            pending.append(hypothesis)
        return pending
