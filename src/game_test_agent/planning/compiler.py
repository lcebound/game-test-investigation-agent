from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from game_test_agent.contracts.models import (
    CapabilityDescriptor,
    EvidenceRequirement,
    FieldOrigin,
    InvestigationRequest,
    ObjectiveCriterionIR,
    ObjectiveIR,
    ObjectiveSpec,
    PlanCandidate,
    PlanFailureHandling,
    PlanInputBinding,
    PlanOutputContract,
    PlanResourceRequirement,
    PlanScore,
    PlanStep,
    TestPlan,
)
from game_test_agent.planning.validator import PlanValidator
from game_test_agent.storage import canonical_json


@dataclass(frozen=True, slots=True)
class ObjectiveCompilation:
    objective_ir: ObjectiveIR
    candidates: list[PlanCandidate]
    selected: PlanCandidate | None


class ObjectiveCompiler:
    """Deterministic fast compiler for the first supported objective templates."""

    VERSION = "objective-compiler-v1"

    def __init__(self, validator: PlanValidator | None = None) -> None:
        self.validator = validator or PlanValidator()

    def compile(
        self,
        investigation_id: str,
        request: InvestigationRequest,
        capabilities: list[CapabilityDescriptor],
        *,
        revision: int = 1,
    ) -> ObjectiveCompilation:
        objective_ir = self._build_ir(request)
        candidates = self._candidate_plans(
            investigation_id,
            request,
            objective_ir,
            capabilities,
            revision,
        )
        validated: list[PlanCandidate] = []
        for candidate in candidates:
            result = self.validator.validate(
                candidate.plan,
                capabilities,
                max_device_minutes=request.budget.max_device_minutes,
            )
            validated.append(candidate.model_copy(update={"validation_errors": result.errors}))
        accepted = [item for item in validated if item.accepted]
        selected = max(
            accepted, key=lambda item: (item.score.total, item.candidate_id), default=None
        )
        return ObjectiveCompilation(objective_ir, validated, selected)

    def _build_ir(self, request: InvestigationRequest) -> ObjectiveIR:
        constraints = request.constraints
        explicit_goal_type = constraints.get("goal_type")
        goal_type = (
            explicit_goal_type
            if explicit_goal_type in {"verification", "comparison", "diagnosis", "coverage"}
            else self._infer_goal_type(request.objective.description)
        )
        criteria: list[ObjectiveCriterionIR] = []
        requirements: list[EvidenceRequirement] = []
        open_questions: list[str] = []
        raw_spec = constraints.get("objective_spec")
        if raw_spec is not None:
            spec = ObjectiveSpec.model_validate(raw_spec)
            criteria.append(
                ObjectiveCriterionIR(
                    criterion_id=spec.criterion_id,
                    description=(
                        f"{spec.metric}.{spec.statistic} {spec.comparison} "
                        f"{spec.threshold:g} {spec.unit}"
                    ),
                    evaluator="metric_threshold",
                    evaluator_version=spec.rule_version,
                    parameters=spec.model_dump(mode="json"),
                )
            )
            requirements.append(
                EvidenceRequirement(
                    requirement_id=f"evidence-{spec.criterion_id}",
                    evidence_types=[spec.metric],
                    min_independent_runs=spec.min_independent_runs,
                    allowed_sources=list(spec.allowed_execution_sources),
                    comparability_keys=["build_ref", "environment_fingerprint"],
                )
            )
        else:
            for index, text in enumerate(request.objective.acceptance_criteria, start=1):
                criteria.append(
                    ObjectiveCriterionIR(
                        criterion_id=f"criterion-{index}",
                        description=text,
                        evaluator="explicit_acceptance_text",
                        evaluator_version="v1",
                    )
                )
            if not criteria:
                open_questions.append("A measurable acceptance criterion is required.")
            requirements.append(
                EvidenceRequirement(
                    requirement_id="objective-evidence",
                    evidence_types=list(constraints.get("required_evidence_types", [])),
                    min_independent_runs=int(constraints.get("min_independent_runs", 1)),
                )
            )

        scope: dict[str, Any] = {
            "tenant_id": request.tenant_id,
            "project_id": request.project_id,
        }
        if request.build_ref is not None:
            scope["build_ref"] = request.build_ref.model_dump(mode="json")
        elif goal_type in {"verification", "comparison"}:
            open_questions.append("Build identity is not yet confirmed.")
        return ObjectiveIR(
            objective_id=request.objective.objective_id,
            goal_type=goal_type,
            scope=scope,
            criteria=criteria,
            evidence_requirements=requirements,
            constraints={
                "budget": request.budget.model_dump(mode="json"),
                "user_constraints": constraints,
            },
            field_origins={
                "objective_id": FieldOrigin(source="user", reference="request.objective"),
                "scope": FieldOrigin(source="user", reference="request"),
                "criteria": FieldOrigin(
                    source="user",
                    reference=(
                        "request.constraints.objective_spec"
                        if raw_spec is not None
                        else "request.objective.acceptance_criteria"
                    ),
                ),
                "goal_type": FieldOrigin(
                    source="user" if explicit_goal_type else "default",
                    reference="request.constraints.goal_type"
                    if explicit_goal_type
                    else self.VERSION,
                ),
            },
            open_questions=list(dict.fromkeys(open_questions)),
            compiler_version=self.VERSION,
        )

    def _candidate_plans(
        self,
        investigation_id: str,
        request: InvestigationRequest,
        objective_ir: ObjectiveIR,
        capabilities: list[CapabilityDescriptor],
        revision: int,
    ) -> list[PlanCandidate]:
        ready = [item for item in capabilities if item.availability == "ready"]
        query = next((item for item in ready if item.operation_kind == "query_run"), None)
        experiments = [
            item for item in ready if item.operation_kind == "experiment" and item.creates_run
        ]
        variants: list[CapabilityDescriptor | None] = experiments[:3] or [None]
        candidates: list[PlanCandidate] = []
        for experiment in variants:
            steps: list[PlanStep] = []
            prior: list[str] = []
            if request.build_ref is None and query is not None:
                query_minutes = float(query.estimated_cost.get("device_minutes", 0))
                steps.append(
                    PlanStep(
                        step_id="confirm-source",
                        objective_id=objective_ir.objective_id,
                        action_type="query",
                        capability_id=query.capability_id,
                        capability_version=query.capability_version,
                        provider_id=query.provider_id,
                        required_inputs=["existing_run_ref"],
                        outputs=[
                            PlanOutputContract(
                                output_name="source_identity", output_type="source_identity"
                            )
                        ],
                        preconditions=["existing_run_ref is authorized and resolvable"],
                        completion_conditions=[
                            "build and environment identity are recorded as evidence"
                        ],
                        resource_requirements=[
                            PlanResourceRequirement(
                                resource_domain=query.resource_domain,
                                amount=max(query_minutes, 0.001),
                                unit="device_minutes",
                                exclusive=False,
                            )
                        ],
                        expected_evidence=["build_identity", "environment_identity"],
                        estimated_device_minutes=query_minutes,
                        cost_limit_device_minutes=query_minutes,
                        failure_handling=PlanFailureHandling(strategy="request_input"),
                        status="ready",
                    )
                )
                prior = ["confirm-source"]
            if experiment is not None:
                minutes = float(experiment.estimated_cost.get("device_minutes", 0))
                steps.append(
                    PlanStep(
                        step_id="collect-evidence",
                        objective_id=objective_ir.objective_id,
                        dependencies=prior,
                        action_type="experiment",
                        capability_id=experiment.capability_id,
                        capability_version=experiment.capability_version,
                        provider_id=experiment.provider_id,
                        input_bindings=(
                            [
                                PlanInputBinding(
                                    input_name="source_identity",
                                    source_step_id="confirm-source",
                                    source_output="source_identity",
                                    expected_type="source_identity",
                                )
                            ]
                            if prior
                            else []
                        ),
                        outputs=[
                            PlanOutputContract(
                                output_name="evidence_bundle", output_type="evidence[]"
                            )
                        ],
                        preconditions=["capability snapshot is current", "budget is available"],
                        completion_conditions=[
                            "run reaches a terminal state and its evidence manifest is ingested"
                        ],
                        resource_requirements=[
                            PlanResourceRequirement(
                                resource_domain=experiment.resource_domain,
                                amount=max(minutes, 0.001),
                                unit="device_minutes",
                                exclusive=True,
                            )
                        ],
                        expected_evidence=(
                            experiment.evidence_types
                            or [
                                item
                                for req in objective_ir.evidence_requirements
                                for item in req.evidence_types
                            ]
                            or ["normalized_run_result"]
                        ),
                        estimated_device_minutes=minutes,
                        cost_limit_device_minutes=minutes,
                        failure_handling=PlanFailureHandling(strategy="replan", max_attempts=2),
                        status="ready" if not prior else "pending",
                    )
                )
                prior = ["collect-evidence"]
            elif not prior:
                steps.append(
                    PlanStep(
                        step_id="request-required-input",
                        objective_id=objective_ir.objective_id,
                        action_type="request_input",
                        required_inputs=objective_ir.open_questions
                        or ["authorized execution capability"],
                        outputs=[
                            PlanOutputContract(output_name="user_input", output_type="user_input")
                        ],
                        preconditions=["required information is not available in current context"],
                        completion_conditions=["requested user input is recorded"],
                        failure_handling=PlanFailureHandling(strategy="stop"),
                        status="ready",
                    )
                )
                prior = ["request-required-input"]
            steps.extend(
                [
                    PlanStep(
                        step_id="evaluate-objective",
                        objective_id=objective_ir.objective_id,
                        dependencies=prior,
                        action_type="evaluate",
                        input_bindings=[
                            PlanInputBinding(
                                input_name="objective_input",
                                source_step_id=prior[0],
                                source_output=(
                                    "evidence_bundle"
                                    if prior[0] == "collect-evidence"
                                    else "source_identity"
                                    if prior[0] == "confirm-source"
                                    else "user_input"
                                ),
                                expected_type=(
                                    "evidence[]"
                                    if prior[0] == "collect-evidence"
                                    else "source_identity"
                                    if prior[0] == "confirm-source"
                                    else "user_input"
                                ),
                            )
                        ],
                        outputs=[
                            PlanOutputContract(
                                output_name="objective_assessment",
                                output_type="criterion_assessment",
                            )
                        ],
                        preconditions=["required upstream output is current"],
                        completion_conditions=["every objective criterion has a verdict or gap"],
                        failure_handling=PlanFailureHandling(strategy="replan"),
                    ),
                    PlanStep(
                        step_id="publish-report",
                        objective_id=objective_ir.objective_id,
                        dependencies=["evaluate-objective"],
                        action_type="report",
                        input_bindings=[
                            PlanInputBinding(
                                input_name="objective_assessment",
                                source_step_id="evaluate-objective",
                                source_output="objective_assessment",
                                expected_type="criterion_assessment",
                            )
                        ],
                        outputs=[
                            PlanOutputContract(
                                output_name="investigation_report",
                                output_type="investigation_report",
                            )
                        ],
                        preconditions=["objective assessment is current"],
                        completion_conditions=["report revision is published"],
                        failure_handling=PlanFailureHandling(strategy="retry", max_attempts=2),
                    ),
                ]
            )
            plan = TestPlan(investigation_id=investigation_id, revision=revision, steps=steps)
            identity = hashlib.sha256(canonical_json(plan).encode()).hexdigest()[:16]
            risk = 0.0 if experiment is None or experiment.risk_level == "read_only" else 1.0
            cost = sum(item.estimated_device_minutes for item in steps) / max(
                request.budget.max_device_minutes, 1
            )
            candidates.append(
                PlanCandidate(
                    candidate_id=f"plan-{identity}",
                    plan=plan,
                    score=PlanScore(
                        gap_coverage=2.0 if experiment is not None else 0.5,
                        hypothesis_discrimination=(
                            1.0 if objective_ir.goal_type == "diagnosis" and experiment else 0
                        ),
                        cost_penalty=cost,
                        risk_penalty=risk,
                    ),
                )
            )
        return candidates

    @staticmethod
    def _infer_goal_type(description: str) -> str:
        value = description.casefold()
        if any(token in value for token in ("compare", "comparison", "对比", "比较")):
            return "comparison"
        if any(token in value for token in ("diagnose", "root cause", "诊断", "根因", "分析")):
            return "diagnosis"
        if any(token in value for token in ("coverage", "覆盖率", "覆盖")):
            return "coverage"
        return "verification"
