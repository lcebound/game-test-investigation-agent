from __future__ import annotations

from dataclasses import dataclass, field

from game_test_agent.contracts.models import CapabilityDescriptor, TestPlan


@dataclass(slots=True)
class PlanValidation:
    accepted: bool
    errors: list[str] = field(default_factory=list)
    required_device_minutes: float = 0.0
    device_minute_limit: float = 0.0

    @property
    def budget_exceeded(self) -> bool:
        return self.required_device_minutes > self.device_minute_limit


class PlanValidator:
    def validate(
        self,
        plan: TestPlan,
        capabilities: list[CapabilityDescriptor],
        *,
        max_device_minutes: float,
    ) -> PlanValidation:
        errors: list[str] = []
        step_ids = [step.step_id for step in plan.steps]
        if len(step_ids) != len(set(step_ids)):
            errors.append("step IDs must be unique")
        known = set(step_ids)
        by_id = {step.step_id: step for step in plan.steps}
        for step in plan.steps:
            missing = set(step.dependencies) - known
            if missing:
                errors.append(f"step {step.step_id} has missing dependencies: {sorted(missing)}")
            if step.step_id in step.dependencies:
                errors.append(f"step {step.step_id} depends on itself")
            if len(step.dependencies) != len(set(step.dependencies)):
                errors.append(f"step {step.step_id} has duplicate dependencies")
            output_names = [item.output_name for item in step.outputs]
            if len(output_names) != len(set(output_names)):
                errors.append(f"step {step.step_id} has duplicate output names")
            input_names = [item.input_name for item in step.input_bindings]
            if len(input_names) != len(set(input_names)):
                errors.append(f"step {step.step_id} has duplicate input bindings")
            ancestors = self._ancestors(step.step_id, by_id)
            for binding in step.input_bindings:
                source = by_id.get(binding.source_step_id)
                if source is None:
                    errors.append(
                        f"step {step.step_id} input {binding.input_name} has missing source "
                        f"{binding.source_step_id}"
                    )
                    continue
                if binding.source_step_id not in ancestors:
                    errors.append(
                        f"step {step.step_id} input {binding.input_name} is not dependency-ordered"
                    )
                outputs = {item.output_name: item.output_type for item in source.outputs}
                actual_type = outputs.get(binding.source_output)
                if actual_type is None:
                    errors.append(
                        f"step {step.step_id} input {binding.input_name} references missing output "
                        f"{binding.source_output}"
                    )
                elif actual_type != binding.expected_type:
                    errors.append(
                        f"step {step.step_id} input {binding.input_name} expects "
                        f"{binding.expected_type}, got {actual_type}"
                    )
            fallback = step.failure_handling.fallback_step_id
            if fallback is not None and fallback not in known:
                errors.append(f"step {step.step_id} has missing failure fallback {fallback}")
            if fallback == step.step_id:
                errors.append(f"step {step.step_id} cannot fall back to itself")
            if (
                step.cost_limit_device_minutes is not None
                and step.estimated_device_minutes > step.cost_limit_device_minutes
            ):
                errors.append(f"step {step.step_id} exceeds its device-minute cost limit")
            if step.status in {"ready", "running"} and any(
                by_id[parent].status not in {"completed", "skipped"}
                for parent in step.dependencies
                if parent in by_id
            ):
                errors.append(f"step {step.step_id} is runnable before its dependencies complete")
        if self._has_cycle(plan):
            errors.append("plan dependency graph contains a cycle")
        available = {
            (item.provider_id, item.capability_id, item.capability_version): item
            for item in capabilities
        }
        for step in plan.steps:
            if step.action_type in {"query", "experiment"}:
                binding = (step.provider_id, step.capability_id, step.capability_version)
                descriptor = available.get(binding)
                if descriptor is None:
                    errors.append(f"step {step.step_id} binds an unavailable capability")
                elif descriptor.availability != "ready":
                    errors.append(f"step {step.step_id} binds a capability that is not ready")
                elif step.action_type == "experiment" and (
                    descriptor.operation_kind != "experiment" or not descriptor.creates_run
                ):
                    errors.append(f"step {step.step_id} experiment binding cannot create a run")
                elif step.action_type == "query" and descriptor.operation_kind != "query_run":
                    errors.append(f"step {step.step_id} query binding is not a run query")
                if not step.expected_evidence:
                    errors.append(f"step {step.step_id} has no observable result")
                if descriptor is not None:
                    for requirement in step.resource_requirements:
                        if requirement.resource_domain != descriptor.resource_domain:
                            errors.append(
                                f"step {step.step_id} resource domain does not match its capability"
                            )
                        if (
                            requirement.unit == "device_minutes"
                            and requirement.amount < step.estimated_device_minutes
                        ):
                            errors.append(
                                f"step {step.step_id} under-declares required device minutes"
                            )
        total = sum(
            step.estimated_device_minutes for step in plan.steps if step.status != "skipped"
        )
        if total > max_device_minutes:
            errors.append("plan exceeds the device-minute budget")
        return PlanValidation(
            accepted=not errors,
            errors=errors,
            required_device_minutes=total,
            device_minute_limit=max_device_minutes,
        )

    @staticmethod
    def _ancestors(step_id: str, by_id: dict) -> set[str]:
        found: set[str] = set()
        pending = list(by_id.get(step_id).dependencies if step_id in by_id else [])
        while pending:
            parent = pending.pop()
            if parent in found:
                continue
            found.add(parent)
            if parent in by_id:
                pending.extend(by_id[parent].dependencies)
        return found

    @staticmethod
    def _has_cycle(plan: TestPlan) -> bool:
        dependencies = {step.step_id: set(step.dependencies) for step in plan.steps}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> bool:
            if node in visiting:
                return True
            if node in visited:
                return False
            visiting.add(node)
            if any(visit(parent) for parent in dependencies.get(node, set())):
                return True
            visiting.remove(node)
            visited.add(node)
            return False

        return any(visit(node) for node in dependencies)
