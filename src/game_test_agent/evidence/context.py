from __future__ import annotations

from game_test_agent.capabilities import CapabilityRegistry
from game_test_agent.contracts.models import (
    ActionSpec,
    ActionState,
    CandidateOutcomeRecord,
    ContextBundle,
    ContextManifest,
    ContextManifestItem,
    ContextWindowUsage,
    EvidenceExcerpt,
    HypothesisStatus,
    InvestigationWorkingState,
    RunRef,
    WorkingAnalysisSummary,
    WorkingActionSummary,
)
from game_test_agent.planning import PlanRuntimeProjector, strategy_for
from game_test_agent.memory import CaseMemoryService
from game_test_agent.storage import Repository, canonical_json


class ContextBuilder:
    """Builds a bounded metadata context; raw artifacts are loaded only on demand."""

    def __init__(
        self,
        repository: Repository,
        registry: CapabilityRegistry,
        *,
        window_limit_tokens: int = 24_000,
        reserved_output_tokens: int = 4_000,
        code_execution=None,
    ) -> None:
        self.repository = repository
        self.registry = registry
        self.window_limit_tokens = window_limit_tokens
        self.reserved_output_tokens = reserved_output_tokens
        self.code_execution = code_execution
        self.case_memory = CaseMemoryService(repository)
        self.plan_runtime = PlanRuntimeProjector()

    def build(
        self,
        investigation_id: str,
        *,
        scopes: set[str],
        max_evidence_records: int = 100,
        max_actions: int = 100,
        include_historical_cases: bool = True,
        capability_ids: set[str] | None = None,
        capability_intent: str | None = None,
        step_id: str | None = None,
        window_limit_tokens: int | None = None,
        reserved_output_tokens: int | None = None,
    ) -> ContextBundle:
        if window_limit_tokens is None:
            window_limit_tokens = self.window_limit_tokens
        if reserved_output_tokens is None:
            reserved_output_tokens = self.reserved_output_tokens
        if window_limit_tokens < 1 or reserved_output_tokens < 0:
            raise ValueError("context token limits must be non-negative")
        if reserved_output_tokens >= window_limit_tokens:
            raise ValueError("reserved output tokens must be below the input limit")
        investigation = self.repository.get_investigation(investigation_id)
        all_evidence = self.repository.list_evidence(investigation_id)
        authoritative_evidence = self.repository.list_current_evidence(investigation_id)
        current_by_id = {item.evidence_id: item for item in authoritative_evidence}
        recent_events = self.repository.list_recent_events(investigation_id)
        search_ids: list[str] = []
        for event in recent_events:
            if event["event_type"] == "evidence.search_completed":
                search_ids = event["data"].get("evidence_ids", [])[:20]
        excerpts: list[EvidenceExcerpt] = []
        for event in recent_events:
            if event["event_type"] != "evidence.excerpt_read":
                continue
            excerpt = EvidenceExcerpt.model_validate(event["data"]["excerpt"])
            source = current_by_id.get(excerpt.evidence_id)
            if source is None or source.sha256 != excerpt.source_sha256:
                continue
            excerpts.append(excerpt)
        excerpts = list({
            (item.evidence_id, item.source_sha256, item.byte_offset, item.returned_bytes): item
            for item in excerpts
        }.values())[-4:]
        authoritative_actions = self.repository.list_actions(investigation_id)
        analysis_jobs = self.repository.list_analysis_jobs(investigation_id)
        evidence = authoritative_evidence
        actions = authoritative_actions
        facts = self.repository.list_current_facts(investigation_id)
        hypotheses = self.repository.list_hypotheses(investigation_id)
        superseded_evidence = self.repository.superseded_evidence_ids(investigation_id)
        if superseded_evidence:
            projected_hypotheses = []
            for hypothesis in hypotheses:
                supporting = [
                    item
                    for item in hypothesis.supporting_evidence_ids
                    if item not in superseded_evidence
                ]
                contradicting = [
                    item
                    for item in hypothesis.contradicting_evidence_ids
                    if item not in superseded_evidence
                ]
                changed = (
                    supporting != hypothesis.supporting_evidence_ids
                    or contradicting != hypothesis.contradicting_evidence_ids
                )
                projected_hypotheses.append(
                    hypothesis.model_copy(
                        update={
                            "supporting_evidence_ids": supporting,
                            "contradicting_evidence_ids": contradicting,
                            "missing_evidence": (
                                [
                                    *hypothesis.missing_evidence,
                                    "previous hypothesis evidence was superseded",
                                ]
                                if changed
                                else hypothesis.missing_evidence
                            ),
                            "status": (
                                HypothesisStatus.UNRESOLVED
                                if changed
                                and hypothesis.status
                                in {HypothesisStatus.SUPPORTED, HypothesisStatus.CONFIRMED}
                                else hypothesis.status
                            ),
                        }
                    )
                )
            hypotheses = projected_hypotheses
        all_capabilities = self.registry.discover(investigation.request.project_id, scopes=scopes)
        capability_summaries = self.registry.discover_summaries(
            investigation.request.project_id,
            scopes=scopes,
            intent=capability_intent,
        )
        allowed_providers = investigation.request.constraints.get("provider_allowlist")
        if allowed_providers is not None:
            all_capabilities = [item for item in all_capabilities
                                if item.provider_id in allowed_providers]
            capability_summaries = [item for item in capability_summaries
                                    if item.provider_id in allowed_providers]
        capabilities = (
            all_capabilities
            if capability_ids is None
            else [item for item in all_capabilities if item.capability_id in capability_ids]
        )
        user_inputs = self.repository.list_user_inputs(investigation_id)
        plan = self.plan_runtime.project(
            self.repository.get_latest_plan(investigation_id),
            actions=authoritative_actions,
            assessment=self.repository.latest_assessment(investigation_id),
            has_user_input=bool(user_inputs),
            report_published=investigation.status == "completed",
        )
        limitations: list[str] = []
        if len(all_evidence) != len(authoritative_evidence):
            limitations.append("superseded_evidence_omitted_from_reasoning_context")
        if len(evidence) > max_evidence_records:
            evidence = evidence[-max_evidence_records:]
            if search_ids:
                selected = [current_by_id[eid] for eid in search_ids if eid in current_by_id]
                evidence = list({item.evidence_id: item for item in [*evidence, *selected]}.values())[
                    -max_evidence_records:
                ]
            limitations.append("older_evidence_omitted_from_reasoning_context")
        if len(actions) > max_actions:
            actions = actions[-max_actions:]
            limitations.append("older_actions_omitted_from_reasoning_context")
        budget = self.repository.budget_usage(investigation_id)
        budget.update(
            {
                "max_device_minutes": investigation.request.budget.max_device_minutes,
                "max_real_runs": investigation.request.budget.max_real_runs,
                "max_tool_reads": investigation.request.budget.max_tool_reads,
            }
        )
        strategy = strategy_for(investigation.request.constraints.get("anomaly_kind"))
        diagnostic_guidance = strategy.as_context() if strategy else {}
        from game_test_agent.storage.feedback import FeedbackStore
        subscribed_feedback = FeedbackStore(self.repository).inline(investigation_id)
        if subscribed_feedback:
            diagnostic_guidance["external_run_feedback"] = subscribed_feedback
        from game_test_agent.memory.recall import RecallService
        history = RecallService(self.repository).inline(investigation)
        if history:
            diagnostic_guidance["historical_sources"] = history
        feedback = [event["data"] for event in recent_events
                    if event["event_type"] == "execution.rejected"][-2:]
        if feedback:
            diagnostic_guidance["execution_feedback"] = feedback
        constraints = investigation.request.constraints
        diagnostic_guidance["code_execution"] = (
            self.code_execution.description(investigation_id)
            if self.code_execution else {"enabled": False}
        )
        memory_query = constraints.get("memory_query") or constraints.get("anomaly_kind")
        historical_cases = (
            self.case_memory.search(
                investigation.request.tenant_id,
                investigation.request.project_id,
                query=str(memory_query) if memory_query else None,
                engine_family=(
                    str(constraints["engine_family"]) if constraints.get("engine_family") else None
                ),
                engine_version=(
                    str(constraints["engine_version"])
                    if constraints.get("engine_version")
                    else None
                ),
                build_ref=investigation.request.build_ref,
                environment_fingerprint=(
                    str(constraints["environment_fingerprint"])
                    if constraints.get("environment_fingerprint")
                    else None
                ),
                limit=5,
            )
            if include_historical_cases
            else []
        )
        if historical_cases:
            limitations.append("historical_cases_are_planning_hints_not_current_evidence")
        working_state = self._build_working_state(
            investigation,
            actions=authoritative_actions,
            evidence=authoritative_evidence,
            facts=facts,
            hypotheses=hypotheses,
            capabilities=capabilities,
            budget=budget,
            analysis_jobs=analysis_jobs,
        )
        evidence, facts, hypotheses, actions, historical_cases, context_window = (
            self._fit_context_window(
                investigation=investigation,
                working_state=working_state,
                plan=plan,
                capabilities=capabilities,
                evidence=evidence,
                facts=facts,
                hypotheses=hypotheses,
                actions=actions,
                user_inputs=user_inputs,
                budget=budget,
                diagnostic_guidance=diagnostic_guidance,
                historical_cases=historical_cases,
                limitations=limitations,
                window_limit_tokens=window_limit_tokens,
                reserved_output_tokens=reserved_output_tokens,
                original_counts={
                    "evidence": len(authoritative_evidence),
                    "facts": len(facts),
                    "hypotheses": len(hypotheses),
                    "actions": len(authoritative_actions),
                    "historical_cases": len(historical_cases),
                },
            )
        )
        if context_window.over_budget:
            limitations.append("mandatory_context_exceeds_token_budget")
        if any(context_window.omitted_records.values()):
            limitations.append("optional_records_omitted_for_context_budget")
        manifest_items = [
            ContextManifestItem(
                object_type="working_state",
                object_id=investigation_id,
                revision=str(working_state.state_revision),
                reason="mandatory current investigation state",
                freshness="authoritative",
                estimated_tokens=self._estimate_tokens(working_state),
            )
        ]
        manifest_items.extend(
            ContextManifestItem(
                object_type="evidence",
                object_id=item.evidence_id,
                revision=str(item.source_revision),
                reason="selected within the evidence and token filters",
                freshness=("current" if item.retrieval_status == "ready" else "stale"),
                estimated_tokens=self._estimate_tokens(item),
            )
            for item in evidence
        )
        manifest_items.extend(
            ContextManifestItem(
                object_type="fact",
                object_id=item.fact_id,
                revision=item.extractor_version,
                reason="derived fact relevant to the current objective",
                freshness="current",
                estimated_tokens=self._estimate_tokens(item),
            )
            for item in facts
        )
        manifest_items.extend(
            ContextManifestItem(
                object_type="evidence_excerpt",
                object_id=f"{item.evidence_id}:{item.byte_offset}:{item.returned_bytes}",
                revision=item.source_sha256 or str(item.source_revision),
                reason="body bytes actually read by the investigation",
                freshness="current",
                estimated_tokens=self._estimate_tokens(item),
            )
            for item in excerpts
        )
        manifest = ContextManifest(
            investigation_id=investigation_id,
            goal_id=investigation.request.objective.objective_id,
            step_id=step_id,
            state_revision=working_state.state_revision,
            source_cursor=self.repository.latest_event_cursor(investigation_id),
            permission_scope=sorted(scopes),
            included=manifest_items,
            omitted=dict(context_window.omitted_records),
            capability_summaries=capability_summaries,
            loaded_capability_ids=sorted({item.capability_id for item in capabilities}),
            estimated_tokens=context_window.estimated_input_tokens,
        )
        return ContextBundle(
            investigation=investigation,
            working_state=working_state,
            plan=plan,
            capabilities=capabilities,
            evidence=evidence,
            excerpts=excerpts,
            facts=facts,
            hypotheses=hypotheses,
            actions=actions,
            user_inputs=user_inputs,
            budget=budget,
            diagnostic_guidance=diagnostic_guidance,
            limitations=limitations,
            context_window=context_window,
            historical_cases=historical_cases,
            context_manifest=manifest,
        )

    def _fit_context_window(
        self,
        *,
        investigation,
        working_state,
        plan,
        capabilities,
        evidence,
        facts,
        hypotheses,
        actions,
        user_inputs,
        budget,
        diagnostic_guidance,
        historical_cases,
        limitations,
        window_limit_tokens: int,
        reserved_output_tokens: int,
        original_counts: dict[str, int],
    ):
        optional = {
            "evidence": list(evidence),
            "facts": list(facts),
            "hypotheses": list(hypotheses),
            "actions": list(actions),
            "historical_cases": list(historical_cases),
        }
        mandatory_material = {
            "investigation": investigation,
            "working_state": working_state,
            "plan": plan,
            "capabilities": capabilities,
            "user_inputs": user_inputs,
            "budget": budget,
            "diagnostic_guidance": diagnostic_guidance,
            "limitations": limitations,
        }
        # Reserve a small envelope for field names and ContextWindowUsage itself.
        mandatory_tokens = self._estimate_tokens(mandatory_material) + 256
        available_tokens = window_limit_tokens - reserved_output_tokens
        optional_tokens = self._estimate_tokens(optional)
        removal_order = ("historical_cases", "actions", "hypotheses", "evidence", "facts")
        while optional_tokens > max(0, available_tokens - mandatory_tokens):
            for key in removal_order:
                if optional[key]:
                    optional[key].pop(0)
                    break
            else:
                break
            optional_tokens = self._estimate_tokens(optional)

        omitted = {key: original_counts[key] - len(optional[key]) for key in original_counts}
        estimated_input = mandatory_tokens + optional_tokens
        usage = ContextWindowUsage(
            window_limit_tokens=window_limit_tokens,
            reserved_output_tokens=reserved_output_tokens,
            mandatory_tokens=mandatory_tokens,
            optional_tokens=optional_tokens,
            estimated_input_tokens=estimated_input,
            over_budget=estimated_input > available_tokens,
            omitted_records=omitted,
        )
        return (
            optional["evidence"],
            optional["facts"],
            optional["hypotheses"],
            optional["actions"],
            optional["historical_cases"],
            usage,
        )

    @staticmethod
    def _estimate_tokens(value) -> int:
        encoded_size = len(canonical_json(value).encode("utf-8"))
        return (encoded_size + 1) // 2

    def _build_working_state(
        self,
        investigation,
        *,
        actions,
        evidence,
        facts,
        hypotheses,
        capabilities,
        budget,
        analysis_jobs,
    ) -> InvestigationWorkingState:
        summaries: list[WorkingActionSummary] = []
        for item in actions:
            action = ActionSpec.model_validate(item["action"])
            operation = item.get("operation_ref") or {}
            latest = self.repository.get_latest_run_result(action.action_id)
            result = latest.result if latest else None
            summaries.append(
                WorkingActionSummary(
                    action_id=action.action_id,
                    state=ActionState(item["state"]),
                    capability_id=action.capability_id,
                    provider_id=action.provider_id,
                    operation_kind=action.operation_kind,
                    expected_observation=action.expected_observation,
                    run_ref=(
                        RunRef.model_validate(operation["run_ref"])
                        if operation.get("run_ref")
                        else None
                    ),
                    execution_status=(
                        result.execution_status if result else operation.get("execution_status")
                    ),
                    test_verdict=result.test_verdict if result else None,
                    execution_source=result.execution_source if result else None,
                    result_revision=result.revision if result else None,
                    result_ingestion_state=latest.ingestion_state if latest else None,
                    missing_fields=result.missing_fields if result else [],
                )
            )

        completed = [item for item in summaries if item.state == ActionState.SUCCEEDED]
        failed = [
            item for item in summaries if item.state in {ActionState.FAILED, ActionState.CANCELED}
        ]
        in_flight = [
            item
            for item in summaries
            if item.state
            in {
                ActionState.RESERVED,
                ActionState.DISPATCHING,
                ActionState.ACCEPTED,
                ActionState.WAITING,
                ActionState.UNKNOWN,
            }
        ]
        actions_with_evidence = {item.action_id for item in evidence if item.action_id}
        pending_evidence = [
            item.action_id for item in completed if item.action_id not in actions_with_evidence
        ]
        analysis_summaries = []
        for job in analysis_jobs:
            result = self.repository.get_latest_analysis_result(job.spec.job_id)
            analysis_summaries.append(
                WorkingAnalysisSummary(
                    job_id=job.spec.job_id,
                    state=job.state,
                    purpose=job.spec.purpose,
                    input_evidence_ids=job.spec.input_evidence_ids,
                    runner_id=job.spec.environment.runner_id,
                    operation_id=(job.operation_ref.operation_id if job.operation_ref else None),
                    result_ingestion_state=(result.ingestion_state if result else None),
                    exit_code=result.result.exit_code if result else None,
                    failure_kind=result.result.failure_kind if result else None,
                    output_evidence_ids=[e.evidence_id for e in evidence
                                         if e.analysis_job_id == job.spec.job_id],
                )
            )
        assessment = self.repository.latest_assessment(investigation.investigation_id)
        active_hypotheses = [
            item
            for item in hypotheses
            if item.status not in {HypothesisStatus.REFUTED, HypothesisStatus.CONFIRMED}
        ]
        refuted_hypotheses = [
            item for item in hypotheses if item.status == HypothesisStatus.REFUTED
        ]
        unresolved = list(assessment.missing_requirements if assessment else [])
        for hypothesis in active_hypotheses:
            unresolved.extend(hypothesis.missing_evidence)
        unresolved = list(dict.fromkeys(unresolved))

        request_budget = investigation.request.budget
        remaining = {
            "real_runs": max(0, request_budget.max_real_runs - int(budget.get("real_runs", 0))),
            "tool_reads": max(0, request_budget.max_tool_reads - int(budget.get("tool_reads", 0))),
            "device_minutes": max(
                0.0,
                request_budget.max_device_minutes - float(budget.get("reserved_device_minutes", 0)),
            ),
        }
        candidates: list[str] = []
        if assessment and assessment.is_complete:
            candidates.append("report")
        else:
            if in_flight:
                candidates.append("query_run")
            if pending_evidence:
                candidates.append("retrieve_evidence")
            if evidence or facts:
                candidates.append("analyze")
            if any(item.operation_kind == "experiment" for item in capabilities):
                candidates.append("experiment")
            if not candidates:
                candidates.append("request_input")
            candidates.append("report")

        return InvestigationWorkingState(
            investigation_id=investigation.investigation_id,
            state_revision=investigation.revision,
            objective=investigation.request.objective,
            assessment=assessment,
            active_hypotheses=active_hypotheses,
            refuted_hypotheses=refuted_hypotheses,
            completed_actions=completed,
            failed_actions=failed,
            in_flight_actions=in_flight,
            analysis_jobs=analysis_summaries,
            recent_candidate_outcomes=[
                CandidateOutcomeRecord.model_validate(event["data"])
                for event in self.repository.list_recent_events_by_type(
                    investigation.investigation_id, "candidate.outcome", limit=3
                )
            ],
            pending_evidence_action_ids=pending_evidence,
            unresolved_questions=unresolved,
            budget_remaining=remaining,
            next_candidates=candidates,
        )
