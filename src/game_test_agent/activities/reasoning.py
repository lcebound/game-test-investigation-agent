from __future__ import annotations

import hashlib
from typing import Any

from game_test_agent.agents import Investigator
from game_test_agent.contracts.models import ModelProfile, NextStep, ReasoningStep
from game_test_agent.models import ModelCallError, ModelGateway, ModelProviderResponse
from game_test_agent.storage import Repository, canonical_json


class ReasoningService:
    def __init__(
        self,
        repository: Repository,
        investigator: Investigator,
        *,
        gateway: ModelGateway | None = None,
        profile_id: str = "investigator",
        execution_limits=None,
    ) -> None:
        self.repository = repository
        self.investigator = investigator
        self.profile_id = profile_id
        if gateway is None:
            profile = ModelProfile(
                profile_id=profile_id,
                profile_version="1",
                provider="configured",
                model_name=investigator.model_name,
                api_mode=(
                    getattr(investigator, "model_api", "callable")
                    if getattr(investigator, "model_api", "callable")
                    in {"responses", "chat_completions"}
                    else "callable"
                ),
                output_mode=(
                    "json_schema"
                    if getattr(investigator, "output_mode", "json_schema") == "json_schema"
                    else "json_object"
                ),
                timeout_seconds=getattr(investigator, "timeout_seconds", 60),
                max_output_tokens=getattr(investigator, "max_output_tokens", 4096),
                max_attempts=1,
            )

            async def call(_profile: ModelProfile, request: dict[str, Any]):
                response_method = getattr(investigator, "reason_next_step_response", None)
                if response_method is not None:
                    return await response_method(request)
                output = await investigator.reason_next_step(request)
                return ModelProviderResponse(content=output)

            from game_test_agent.observability.tracing import InvestigationTracer
            gateway = ModelGateway([profile], call, execution_limits=execution_limits,
                                   tracer=InvestigationTracer(repository))
        self.gateway = gateway

    async def reason(
        self,
        investigation_id: str,
        plan_revision: int,
        reasoning_step_id: str,
        context: dict[str, Any],
        *,
        prompt_version: str = "investigator-v1",
    ) -> ReasoningStep:
        digest = hashlib.sha256(canonical_json(context).encode()).hexdigest()
        cached = self.repository.get_reasoning_step(
            investigation_id, plan_revision, reasoning_step_id
        )
        if cached:
            if cached.context_hash != digest:
                raise ValueError("cached reasoning step context does not match")
            return cached
        investigation = self.repository.get_investigation(investigation_id)
        self.repository.reserve_counter(
            investigation_id,
            "reasoning_rounds",
            f"{plan_revision}:{reasoning_step_id}",
            investigation.request.budget.max_rounds,
        )
        try:
            result = await self.gateway.invoke(self.profile_id, context, NextStep,
                investigation_id=investigation_id,
                project_scope=(investigation.request.tenant_id, investigation.request.project_id))
        except ModelCallError as error:
            if error.last_exception is not None:
                raise error.last_exception
            raise
        step = ReasoningStep(
            investigation_id=investigation_id,
            plan_revision=plan_revision,
            reasoning_step_id=reasoning_step_id,
            model_name=self.investigator.model_name,
            prompt_version=prompt_version,
            context_hash=digest,
            output=result.output,
            model_profile_id=self.profile_id,
            model_profile_version=self.gateway.profile(self.profile_id).profile_version,
            raw_output_ref=result.raw_output_ref,
            normalization_log=result.normalization_log,
            attempts=[item.model_dump(mode="json") for item in result.attempts],
            usage={
                **result.usage.model_dump(mode="json"),
                "attempt_count": len(result.attempts),
                "raw_output_ref": result.raw_output_ref,
                "normalization_count": len(result.normalization_log),
            },
        )
        persisted, _ = self.repository.save_reasoning_step(step)
        return persisted
