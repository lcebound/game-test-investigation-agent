from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from typing import Any

from game_test_agent.contracts.models import (
    ActionProposal,
    ActionSpec,
    ContextBundle,
    ModelProfile,
    NextStep,
    TargetSelector,
)
from game_test_agent.models import (
    ModelGateway,
    ModelProviderResponse,
    usage_from_agents_result,
)


@dataclass(frozen=True, slots=True)
class ActionPlanningAudit:
    model_profile_id: str
    model_profile_version: str
    raw_output_ref: str
    normalization_log: list[str]
    attempts: list[dict[str, Any]]
    usage: dict[str, int | float | str | bool | None]


class ActionPlanner:
    def __init__(
        self,
        model_name: str,
        enabled: bool = False,
        *,
        model_api: str = "responses",
        output_mode: str = "json_schema",
        timeout_seconds: float = 60.0,
        max_output_tokens: int = 4096,
        reasoning_effort: str | None = None,
        extra_body: dict[str, Any] | None = None,
        execution_limits=None,
        tracer=None,
    ) -> None:
        self.model_name = model_name
        self.enabled = enabled
        self.model_api = model_api
        if output_mode not in {"json_schema", "json_prompt", "json_object"}:
            raise ValueError("unsupported action planner output mode")
        if output_mode == "json_object" and model_api != "chat_completions":
            raise ValueError("json_object output requires chat_completions")
        self.output_mode = output_mode
        self.extra_body = dict(extra_body or {})
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens = max_output_tokens
        self.reasoning_effort = reasoning_effort
        if enabled:
            from agents import set_default_openai_api

            set_default_openai_api(model_api)
        self.model_profile = ModelProfile(
            profile_id="action-planner",
            profile_version="1",
            provider="configured",
            model_name=model_name,
            api_mode=(model_api if model_api in {"responses", "chat_completions"} else "callable"),
            output_mode="json_schema" if output_mode == "json_schema" else "json_object",
            timeout_seconds=timeout_seconds,
            max_output_tokens=max_output_tokens,
            max_attempts=2,
        )
        self.gateway = ModelGateway([self.model_profile], self._provider_call,
                                    execution_limits=execution_limits, tracer=tracer)

    async def propose(self, context: ContextBundle, next_step: NextStep) -> ActionProposal:
        proposal, _audit = await self.propose_audited(context, next_step)
        return proposal

    async def propose_audited(
        self, context: ContextBundle, next_step: NextStep
    ) -> tuple[ActionProposal, ActionPlanningAudit | None]:
        candidates = [
            item
            for item in context.capabilities
            if item.capability_id == next_step.capability_id
            and item.provider_id == next_step.provider_id
        ]
        if not candidates:
            raise ValueError("next step does not bind an available capability")
        capability = candidates[0]
        parameters = context.investigation.request.constraints.get("action_parameters")
        if isinstance(parameters, dict):
            request_constraints = context.investigation.request.constraints
            target = TargetSelector.model_validate(
                request_constraints.get("action_target_selector", {})
            )
            execution_constraints = request_constraints.get("action_constraints", {})
            if not isinstance(execution_constraints, dict):
                raise ValueError("action_constraints must be an object")
            return ActionProposal(
                capability_id=capability.capability_id,
                capability_version=capability.capability_version,
                provider_id=capability.provider_id,
                provider_revision=capability.provider_revision,
                parameters=parameters,
                target_selector=target,
                expected_observation=next_step.expected_observation,
                constraints={
                    **execution_constraints,
                    "max_device_minutes": min(
                        float(capability.estimated_cost.get("device_minutes", 0)),
                        context.investigation.request.budget.max_device_minutes,
                    ),
                },
            ), None
        if self.enabled:
            return await self._model_proposal(context, next_step, candidates)
        raise ValueError("action parameters require model planning or explicit constraints")

    async def _model_proposal(
        self,
        context: ContextBundle,
        next_step: NextStep,
        candidates: list,
    ) -> tuple[ActionProposal, ActionPlanningAudit]:
        allowed = [item.model_dump(mode="json", by_alias=True) for item in candidates]
        payload: dict[str, Any] = {
            "next_step": next_step.model_dump(mode="json"),
            "objective": context.investigation.request.objective.model_dump(mode="json"),
            "build_ref": (
                context.investigation.request.build_ref.model_dump(mode="json")
                if context.investigation.request.build_ref
                else None
            ),
            "request_constraints": context.investigation.request.constraints,
            "allowed_capabilities": allowed,
            "remaining_budget": context.budget,
        }
        result = await self.gateway.invoke(
            self.model_profile.profile_id,
            payload,
            ActionProposal,
            investigation_id=context.investigation.investigation_id,
            project_scope=(context.investigation.request.tenant_id, context.investigation.request.project_id),
        )
        proposal = result.output
        if not any(
            proposal.capability_id == item.capability_id
            and proposal.capability_version == item.capability_version
            and proposal.provider_id == item.provider_id
            for item in candidates
        ):
            raise ValueError("model proposed a capability outside the allowed candidates")
        selected = next(
            item
            for item in candidates
            if proposal.capability_id == item.capability_id
            and proposal.capability_version == item.capability_version
            and proposal.provider_id == item.provider_id
        )
        if (
            proposal.provider_revision is not None
            and proposal.provider_revision != selected.provider_revision
        ):
            raise ValueError("model proposed a stale provider revision")
        proposal = proposal.model_copy(update={"provider_revision": selected.provider_revision})
        audit = ActionPlanningAudit(
            model_profile_id=self.model_profile.profile_id,
            model_profile_version=self.model_profile.profile_version,
            raw_output_ref=result.raw_output_ref,
            normalization_log=result.normalization_log,
            attempts=[item.model_dump(mode="json") for item in result.attempts],
            usage={
                **result.usage.model_dump(mode="json"),
                "attempt_count": len(result.attempts),
            },
        )
        return proposal, audit

    async def _provider_call(
        self,
        _profile: ModelProfile,
        payload: dict[str, Any],
    ) -> ModelProviderResponse:
        from agents import Agent, AgentOutputSchema, ModelSettings, RunConfig, Runner
        from openai.types.shared import Reasoning

        agent = Agent(
            name="GameTestActionPlanner",
            model=self.model_name,
            instructions=(
                "只从 allowed_capabilities 选择一个精确版本，并按它的 input_schema 生成参数。"
                "不得生成命令、URL、凭据或未声明字段。保持项目、构建和目标范围。"
                "若需要重复实验，必须填写 repeat_index。"
                + (
                    "只输出一个 JSON 对象，不要 Markdown 或解释文字。必须包含 capability_id、"
                    "capability_version、provider_id、parameters、expected_observation。"
                    if self.output_mode in {"json_prompt", "json_object"}
                    else ""
                )
            ),
            # Parameters and constraints intentionally follow the selected capability's
            # runtime JSON Schema, so they cannot be represented by OpenAI's strict
            # structured-output subset. Pydantic still validates the decoded proposal,
            # and the capability registry validates parameters before dispatch.
            output_type=(
                AgentOutputSchema(ActionProposal, strict_json_schema=False)
                if self.output_mode == "json_schema"
                else None
            ),
            model_settings=ModelSettings(
                timeout=self.timeout_seconds,
                max_tokens=self.max_output_tokens,
                extra_body={
                    **self.extra_body,
                    **(
                        {"response_format": {"type": "json_object"}}
                        if self.output_mode == "json_object"
                        else {}
                    ),
                },
                reasoning=(
                    Reasoning(effort=self.reasoning_effort)
                    if self.reasoning_effort is not None
                    else None
                ),
            ),
        )
        result = await Runner.run(
            agent,
            input=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            max_turns=3,
            run_config=RunConfig(tracing_disabled=True, trace_include_sensitive_data=False),
        )
        if self.output_mode in {"json_prompt", "json_object"} and not isinstance(
            result.final_output, str
        ):
            raise ValueError("model did not return a JSON string")
        return ModelProviderResponse(
            content=result.final_output,
            usage=usage_from_agents_result(result),
        )


class ActionFactory:
    @staticmethod
    def create(
        context: ContextBundle,
        proposal: ActionProposal,
        *,
        plan_revision: int,
        ordinal: int,
    ) -> ActionSpec:
        investigation = context.investigation
        plan_step_id: str | None = None
        plan_node_revision: int | None = None
        if context.plan is not None:
            by_id = {item.step_id: item for item in context.plan.steps}
            candidates = [
                item
                for item in context.plan.steps
                if item.action_type == "experiment"
                and item.capability_id == proposal.capability_id
                and item.capability_version == proposal.capability_version
                and item.provider_id == proposal.provider_id
                and item.status in {"pending", "ready"}
                and all(
                    by_id[parent].status in {"completed", "skipped"}
                    for parent in item.dependencies
                    if parent in by_id
                )
            ]
            if len(candidates) != 1:
                raise ValueError("action proposal must bind exactly one dependency-ready plan node")
            plan_step_id = candidates[0].step_id
            plan_node_revision = context.plan.revision
        experiment_material = {
            "objective_id": proposal.experiment_id or investigation.request.objective.objective_id,
            "capability_id": proposal.capability_id,
            "capability_version": proposal.capability_version,
            "provider_id": proposal.provider_id,
            "provider_revision": proposal.provider_revision,
            "build_ref": (
                investigation.request.build_ref.model_dump(mode="json")
                if investigation.request.build_ref
                else None
            ),
            "parameters": proposal.parameters,
            "target_selector": proposal.target_selector.model_dump(mode="json"),
        }
        experiment_id = proposal.experiment_id or (
            "exp-"
            + hashlib.sha256(
                json.dumps(
                    experiment_material,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()[:24]
        )
        identity = f"{plan_revision:03d}-{ordinal:03d}"
        action_id = f"act-{investigation.investigation_id.removeprefix('inv-')[:16]}-{identity}"
        return ActionSpec(
            tenant_id=investigation.request.tenant_id,
            project_id=investigation.request.project_id,
            investigation_id=investigation.investigation_id,
            action_id=action_id,
            idempotency_key=f"{investigation.investigation_id}/{action_id}",
            capability_id=proposal.capability_id,
            capability_version=proposal.capability_version,
            provider_id=proposal.provider_id,
            provider_revision=proposal.provider_revision,
            build_ref=investigation.request.build_ref,
            parameters=proposal.parameters,
            target_selector=proposal.target_selector,
            constraints=proposal.constraints,
            expected_observation=proposal.expected_observation,
            plan_revision=plan_revision,
            plan_step_id=plan_step_id,
            plan_node_revision=plan_node_revision,
            repeat_index=None,
            experiment_id=experiment_id,
            run_sequence=None,
        )
