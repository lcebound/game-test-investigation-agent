from __future__ import annotations

from typing import Any

from game_test_agent.contracts.models import CandidateDesign, ContextBundle, ModelProfile
from game_test_agent.models import ModelCallError, ModelGateway, ModelProviderResponse
from game_test_agent.storage import ConflictError, Repository, payload_hash


class CandidateDesignService:
    """One cached, budgeted model design call for a complex investigation round."""

    VERSION = "candidate-design-v1"

    def __init__(self, repository: Repository, investigator: object, *, execution_limits=None) -> None:
        self.repository = repository
        self.investigator = investigator
        output_mode = getattr(investigator, "output_mode", "json_schema")
        profile = ModelProfile(
            profile_id="candidate-design",
            profile_version=self.VERSION,
            provider="configured",
            model_name=getattr(investigator, "model_name", "unknown"),
            api_mode=(
                getattr(investigator, "model_api", "callable")
                if getattr(investigator, "model_api", "callable")
                in {"responses", "chat_completions"} else "callable"
            ),
            output_mode="json_schema" if output_mode == "json_schema" else "json_object",
            timeout_seconds=getattr(investigator, "timeout_seconds", 60),
            max_output_tokens=min(getattr(investigator, "max_output_tokens", 4096), 4096),
            max_attempts=1,
        )
        from game_test_agent.observability.tracing import InvestigationTracer
        self.gateway = ModelGateway([profile], self._call, execution_limits=execution_limits,
                                    tracer=InvestigationTracer(repository))

    async def design(
        self, context: ContextBundle, *, plan_revision: int
    ) -> CandidateDesign:
        constraints = context.investigation.request.constraints
        if (
            constraints.get("enable_experiment_selection") is not True
            or constraints.get("enable_model_candidate_design") is not True
            or not context.working_state.active_hypotheses
        ):
            return CandidateDesign()
        active = context.working_state.active_hypotheses
        if len(active) < 2 and not any(item.missing_evidence for item in active):
            return CandidateDesign()
        if not (
            hasattr(self.investigator, "design_candidates_response")
            or hasattr(self.investigator, "design_candidates")
        ):
            return CandidateDesign()
        if getattr(self.investigator, "enabled", True) is False:
            return CandidateDesign()
        investigation_id = context.investigation.investigation_id
        context_hash = payload_hash(context)
        cursor = 0
        while True:
            page = self.repository.list_events(investigation_id, after=cursor, limit=500)
            for event in page:
                if (
                    event["event_type"] in {"candidate.design", "candidate.design_failed"}
                    and event["data"].get("plan_revision") == plan_revision
                ):
                    if event["data"].get("context_hash") != context_hash:
                        raise ValueError("candidate design context changed within the same round")
                    return (
                        CandidateDesign.model_validate(event["data"]["design"])
                        if event["event_type"] == "candidate.design"
                        else CandidateDesign()
                    )
            if len(page) < 500:
                break
            cursor = page[-1]["cursor"]
        try:
            self.repository.reserve_counter(
                investigation_id, "candidate_design_calls", f"round-{plan_revision}",
                context.investigation.request.budget.max_rounds,
            )
        except ConflictError:
            self.repository.record_investigation_event(
                investigation_id, "candidate.design_failed", {
                    "plan_revision": plan_revision,
                    "context_hash": context_hash,
                    "error_type": "candidate_design_budget_exhausted",
                    "attempt_count": 0,
                },
            )
            return CandidateDesign()
        payload = context.model_dump(mode="json", by_alias=True)
        try:
            response = await self.gateway.invoke("candidate-design", payload, CandidateDesign, investigation_id=investigation_id, project_scope=(context.investigation.request.tenant_id, context.investigation.request.project_id))
        except ModelCallError as error:
            self.repository.record_investigation_event(
                investigation_id, "candidate.design_failed", {
                    "plan_revision": plan_revision,
                    "context_hash": context_hash,
                    "error_type": type(error.last_exception).__name__
                    if error.last_exception else type(error).__name__,
                    "attempt_count": len(error.attempts),
                },
            )
            return CandidateDesign()
        design = response.output
        self.repository.record_investigation_event(
            investigation_id, "candidate.design", {
                "plan_revision": plan_revision,
                "context_hash": context_hash,
                "design": design.model_dump(mode="json"),
                "raw_output_ref": response.raw_output_ref,
                "usage": response.usage.model_dump(mode="json"),
                "attempts": [item.model_dump(mode="json") for item in response.attempts],
                "profile_version": self.VERSION,
            },
        )
        return design

    async def _call(
        self, _profile: ModelProfile, context: dict[str, Any]
    ) -> ModelProviderResponse:
        response_method = getattr(self.investigator, "design_candidates_response", None)
        if response_method is not None:
            return await response_method(context)
        method = getattr(self.investigator, "design_candidates")
        output = await method(context)
        return ModelProviderResponse(content=output)
