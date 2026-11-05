from __future__ import annotations

from game_test_agent.contracts.models import ContextBundle, NextStep


class DecisionValidationError(ValueError):
    """A model decision is syntactically valid but unsafe or impossible to execute."""


class DecisionValidator:
    VERSION = "decision-validator-v1"

    def validate(self, context: ContextBundle, decision: NextStep) -> NextStep:
        objective_id = context.investigation.request.objective.objective_id
        if decision.objective_id != objective_id:
            raise DecisionValidationError("decision objective is outside the investigation")
        known_evidence = {item.evidence_id for item in context.evidence}
        unknown_evidence = set(decision.evidence_ids) - known_evidence
        if unknown_evidence:
            raise DecisionValidationError("decision references unknown evidence")
        if decision.read_request is not None and decision.kind != "retrieve_evidence":
            raise DecisionValidationError("read_request is only valid for retrieve_evidence")
        if decision.search_query is not None and decision.kind != "search_evidence":
            raise DecisionValidationError("search_query is only valid for search_evidence")
        if decision.analysis_request is not None and decision.kind != "analyze":
            raise DecisionValidationError("analysis_request is only valid for analyze")
        if decision.code_request is not None and decision.kind != "execute_code":
            raise DecisionValidationError("code_request is only valid for execute_code")
        if decision.kind == "execute_code":
            config = context.diagnostic_guidance.get("code_execution", {})
            if not config.get("enabled") or config.get("jobs_remaining", 0) < 1:
                raise DecisionValidationError("code execution is unavailable or its budget is exhausted")
            if decision.code_request is None:
                raise DecisionValidationError("execute_code requires code_request")
            if not set(decision.code_request.input_evidence_ids).issubset(known_evidence):
                raise DecisionValidationError("code request references unknown evidence")
            if any(item.retrieval_status != "ready" for item in context.evidence
                   if item.evidence_id in decision.code_request.input_evidence_ids):
                raise DecisionValidationError("code request requires locally readable evidence")
        if decision.kind in {"query", "query_run"}:
            if not decision.target_action_id:
                raise DecisionValidationError(
                    "query_run requires target_action_id; a query cannot create a run"
                )
            if not decision.query_purpose:
                raise DecisionValidationError("query_run requires query_purpose")
            candidates = [
                item
                for item in context.actions
                if item.get("action", {}).get("action_id") == decision.target_action_id
            ]
            if not candidates:
                raise DecisionValidationError("query_run target action is not in scope")
            target = candidates[0]
            if not target.get("operation_ref"):
                raise DecisionValidationError("query_run target has no accepted operation")
            operation_run = target["operation_ref"].get("run_ref")
            if (
                decision.target_run_ref is not None
                and operation_run != decision.target_run_ref.model_dump(mode="json")
            ):
                raise DecisionValidationError("query_run target run does not match its action")
            return decision.model_copy(update={"kind": "query_run"})
        if decision.kind == "experiment":
            candidates = [
                item
                for item in context.capabilities
                if item.capability_id == decision.capability_id
                and item.provider_id == decision.provider_id
            ]
            if not candidates:
                raise DecisionValidationError("experiment does not bind an available capability")
            if candidates[0].operation_kind != "experiment" or not candidates[0].creates_run:
                raise DecisionValidationError("selected capability cannot create an experiment run")
            if decision.target_action_id or decision.target_run_ref:
                raise DecisionValidationError("experiment cannot masquerade as a run query")
        if decision.kind == "retrieve_evidence":
            if decision.read_request is None:
                raise DecisionValidationError("retrieve_evidence requires read_request")
            if decision.read_request.evidence_id not in known_evidence:
                raise DecisionValidationError("read request references unknown evidence")
            source = next(
                item for item in context.evidence
                if item.evidence_id == decision.read_request.evidence_id
            )
            if source.retrieval_status != "ready":
                raise DecisionValidationError("read request requires ready evidence")
        if decision.kind == "search_evidence" and not decision.search_query:
            raise DecisionValidationError("search_evidence requires search_query")
        if decision.kind == "analyze":
            request = decision.analysis_request
            if request is None:
                raise DecisionValidationError("analyze requires analysis_request")
            if len(request.input_evidence_ids) != 1:
                raise DecisionValidationError("registered analysis requires one input")
            if request.parameters:
                raise DecisionValidationError("registered analysis does not accept parameters")
            if not set(request.input_evidence_ids).issubset(known_evidence):
                raise DecisionValidationError("analysis references unknown evidence")
            source = next(
                item for item in context.evidence
                if item.evidence_id == request.input_evidence_ids[0]
            )
            required_mime = (
                "text/csv" if request.recipe_id == "csv_summary_v1" else "text/plain"
            )
            if source.mime_type.split(";", 1)[0].strip().lower() != required_mime:
                raise DecisionValidationError("analysis input MIME type does not match recipe")
            if source.retrieval_status != "ready":
                raise DecisionValidationError("analysis input is not ready")
        if decision.kind == "request_input" and not decision.missing_fields:
            raise DecisionValidationError("request_input must name the missing fields")
        return decision
