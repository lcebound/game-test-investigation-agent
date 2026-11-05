from __future__ import annotations

import hashlib
from dataclasses import dataclass

from game_test_agent import __version__
from game_test_agent.contracts.models import (
    ContextBundle,
    ContextSnapshot,
    CriterionAssessment,
    DecisionEnvelope,
    DecisionRecord,
    NextStep,
    ReleaseUnit,
    CapabilityDescriptor,
)
from game_test_agent.objectives import ObjectiveEvaluator
from game_test_agent.memory import InvestigationSummaryService
from game_test_agent.planning import (
    DecisionValidator,
    InvestigationDeltaService,
    ObjectiveCompilation,
    ObjectiveCompiler,
)
from game_test_agent.policies import StopContext, StopPolicy
from game_test_agent.storage import ConflictError, Repository, canonical_json, payload_hash


@dataclass(frozen=True, slots=True)
class KernelPreparation:
    snapshot: ContextSnapshot | None
    assessment: CriterionAssessment | None
    stop_reason: str | None = None

    @property
    def should_stop(self) -> bool:
        return self.stop_reason is not None


class InvestigationKernel:
    """Provider-neutral decision and recovery semantics shared by workflow backends."""

    def __init__(
        self,
        repository: Repository,
        *,
        objective_evaluator: ObjectiveEvaluator | None = None,
        decision_validator: DecisionValidator | None = None,
        objective_compiler: ObjectiveCompiler | None = None,
        stop_policy: StopPolicy | None = None,
    ) -> None:
        self.repository = repository
        self.objective_evaluator = objective_evaluator or ObjectiveEvaluator(repository)
        self.decision_validator = decision_validator or DecisionValidator()
        self.objective_compiler = objective_compiler or ObjectiveCompiler()
        self.delta_service = InvestigationDeltaService(repository)
        self.summary_service = InvestigationSummaryService(repository)
        self.stop_policy = stop_policy or StopPolicy()

    def compile_objective(
        self,
        investigation_id: str,
        capabilities: list[CapabilityDescriptor],
    ) -> ObjectiveCompilation:
        investigation = self.repository.get_investigation(investigation_id)
        current = self.repository.get_latest_plan(investigation_id)
        revision = current.revision + 1 if current is not None else 1
        compilation = self.objective_compiler.compile(
            investigation_id,
            investigation.request,
            capabilities,
            revision=revision,
        )
        if compilation.selected is not None:
            capability_snapshot_hash = payload_hash(
                [
                    item.model_dump(mode="json", by_alias=True)
                    for item in sorted(
                        capabilities,
                        key=lambda value: (
                            value.provider_id,
                            value.capability_id,
                            value.capability_version,
                        ),
                    )
                ]
            )
            self.repository.save_plan(
                compilation.selected.plan,
                capability_snapshot_hash,
            )
        self.repository.record_investigation_event(
            investigation_id,
            "objective.compiled",
            {
                "compiler_version": compilation.objective_ir.compiler_version,
                "objective_ir": compilation.objective_ir.model_dump(mode="json"),
                "selected_candidate_id": (
                    compilation.selected.candidate_id if compilation.selected else None
                ),
                "candidate_errors": {
                    item.candidate_id: item.validation_errors for item in compilation.candidates
                },
            },
        )
        return compilation

    def prepare_round(
        self,
        context: ContextBundle,
        *,
        plan_revision: int,
        model_name: str,
        prompt_version: str,
        tool_schema_version: str = "capability-descriptor-v2",
        stop_context: StopContext | None = None,
    ) -> KernelPreparation:
        investigation_id = context.investigation.investigation_id
        try:
            assessment = self.objective_evaluator.evaluate(investigation_id)
        except ValueError:
            return KernelPreparation(None, None, "objective_spec_invalid")
        if assessment and assessment.is_complete:
            reason = "objective_passed" if assessment.verdict == "passed" else "objective_failed"
            return KernelPreparation(None, assessment, reason)
        if assessment is not None:
            unresolved = list(assessment.missing_requirements)
            for hypothesis in context.working_state.active_hypotheses:
                unresolved.extend(hypothesis.missing_evidence)
            working_state = context.working_state.model_copy(
                update={
                    "assessment": assessment,
                    "unresolved_questions": list(dict.fromkeys(unresolved)),
                }
            )
            context = context.model_copy(update={"working_state": working_state})
        if stop_context is not None:
            decision = self.stop_policy.evaluate(stop_context, context.investigation.request.budget)
            if decision.should_stop:
                return KernelPreparation(None, assessment, decision.reason or "policy_stop")

        summary = self.summary_service.refresh(investigation_id)
        context = self.summary_service.attach(context, summary)
        from game_test_agent.context.budget import fit_final_context

        context = fit_final_context(context)
        if context.context_window.over_budget:
            return KernelPreparation(None, assessment, "mandatory_context_exceeds_token_budget")

        context_hash = payload_hash(context)
        authoritative_state_hash = self.progress_fingerprint(investigation_id)
        authorization = self.repository.get_authorization(investigation_id)
        authorization_hash = payload_hash(
            {
                "principal_id": authorization["principal_id"],
                "scopes": sorted(authorization["scopes"]),
                "policy_profile": authorization["policy_profile"],
            }
        )
        constraints = context.investigation.request.constraints
        release_components = {
            "runtime": __version__,
            "contracts": "2.0",
            "objective_compiler": self.objective_compiler.VERSION,
            "decision_validator": getattr(self.decision_validator, "VERSION", "1"),
            "model_profile": model_name,
            "prompt": prompt_version,
            "tool_schema": tool_schema_version,
            "project_pack": str(constraints.get("project_pack_version", "none")),
            "playbook": str(constraints.get("playbook_version", "none")),
        }
        release_unit = ReleaseUnit(
            release_id=(
                "release-"
                + hashlib.sha256(canonical_json(release_components).encode()).hexdigest()[:32]
            ),
            components=release_components,
        )
        identity = {
            "investigation_id": investigation_id,
            "state_revision": context.investigation.revision,
            "plan_revision": plan_revision,
            "context_hash": context_hash,
            "authoritative_state_hash": authoritative_state_hash,
            "authorization_hash": authorization_hash,
            "model_name": model_name,
            "prompt_version": prompt_version,
            "tool_schema_version": tool_schema_version,
            "release_id": release_unit.release_id,
        }
        snapshot = ContextSnapshot(
            snapshot_id="ctx-" + hashlib.sha256(canonical_json(identity).encode()).hexdigest()[:32],
            investigation_id=investigation_id,
            state_revision=context.investigation.revision,
            plan_revision=plan_revision,
            context_hash=context_hash,
            authoritative_state_hash=authoritative_state_hash,
            authorization_hash=authorization_hash,
            model_name=model_name,
            prompt_version=prompt_version,
            tool_schema_version=tool_schema_version,
            context=context,
            context_manifest=context.context_manifest,
            release_unit=release_unit,
        )
        persisted, _ = self.repository.save_context_snapshot(snapshot)
        return KernelPreparation(persisted, assessment)

    def accept_decision(
        self,
        snapshot: ContextSnapshot,
        proposed: NextStep,
        *,
        model_profile_version: str = "unversioned",
        raw_output_ref: str | None = None,
        normalization_log: list[str] | None = None,
    ) -> DecisionRecord:
        if payload_hash(snapshot.context) != snapshot.context_hash:
            raise ConflictError("frozen decision context failed its integrity check")
        decision = self.decision_validator.validate(snapshot.context, proposed)
        digest = payload_hash(decision)
        existing = self.repository.get_decision_record(
            snapshot.investigation_id, snapshot.plan_revision
        )
        if existing is not None:
            if (
                existing.context_snapshot_id != snapshot.snapshot_id
                or existing.expected_state_revision != snapshot.state_revision
                or existing.decision_hash != digest
            ):
                raise ConflictError("plan revision already has a different decision")
            self.delta_service.apply(snapshot.context, existing.decision)
            return existing

        current = self.repository.get_investigation(snapshot.investigation_id)
        if current.revision != snapshot.state_revision:
            raise ConflictError("investigation changed after the decision context was frozen")
        if (
            snapshot.authoritative_state_hash is not None
            and self.progress_fingerprint(snapshot.investigation_id)
            != snapshot.authoritative_state_hash
        ):
            raise ConflictError("authoritative investigation state changed after context freeze")
        authorization = self.repository.get_authorization(snapshot.investigation_id)
        current_authorization_hash = payload_hash(
            {
                "principal_id": authorization["principal_id"],
                "scopes": sorted(authorization["scopes"]),
                "policy_profile": authorization["policy_profile"],
            }
        )
        if current_authorization_hash != snapshot.authorization_hash:
            raise ConflictError("authorization changed after the decision context was frozen")
        self.delta_service.validate(snapshot.context, decision)
        identity = {
            "investigation_id": snapshot.investigation_id,
            "plan_revision": snapshot.plan_revision,
            "context_snapshot_id": snapshot.snapshot_id,
            "decision_hash": digest,
        }
        decision_id = "dec-" + hashlib.sha256(canonical_json(identity).encode()).hexdigest()[:32]
        constraints = snapshot.context.investigation.request.constraints
        manifest = snapshot.context_manifest or snapshot.context.context_manifest
        record = DecisionRecord(
            decision_id=decision_id,
            investigation_id=snapshot.investigation_id,
            plan_revision=snapshot.plan_revision,
            context_snapshot_id=snapshot.snapshot_id,
            expected_state_revision=snapshot.state_revision,
            decision=decision,
            decision_hash=digest,
            envelope=DecisionEnvelope(
                decision_id=decision_id,
                investigation_id=snapshot.investigation_id,
                state_revision=snapshot.state_revision,
                context_manifest=manifest,
                source_cursor=manifest.source_cursor if manifest is not None else 0,
                release_unit=snapshot.release_unit,
                model_profile=snapshot.model_name,
                model_profile_version=model_profile_version,
                prompt_version=snapshot.prompt_version,
                schema_version_used=decision.schema_version,
                project_pack_version=constraints.get("project_pack_version"),
                playbook_version=constraints.get("playbook_version"),
                raw_output_ref=raw_output_ref,
                normalization_log=normalization_log or [],
                validated_decision=decision,
                budget_snapshot=dict(snapshot.context.budget),
            ),
        )
        persisted, _ = self.repository.save_decision_record(record)
        self.delta_service.apply(snapshot.context, persisted.decision)
        return persisted

    def evaluate_objective(self, investigation_id: str) -> CriterionAssessment | None:
        return self.objective_evaluator.evaluate(investigation_id)

    def progress_fingerprint(self, investigation_id: str) -> str:
        """Hash only authoritative progress, never model prose or list truncation."""
        actions = self.repository.list_actions(investigation_id)
        evidence = self.repository.list_evidence(investigation_id)
        hypotheses = self.repository.list_hypotheses(investigation_id)
        assessment = self.repository.latest_assessment(investigation_id)
        plan = self.repository.get_latest_plan(investigation_id)
        user_inputs = self.repository.list_user_inputs(investigation_id)
        analysis_jobs = self.repository.list_analysis_jobs(investigation_id)
        derived_candidates = self.repository.list_derived_fact_candidates(investigation_id)
        current_hashes = {
            item.evidence_id: item.sha256
            for item in self.repository.list_current_evidence(investigation_id)
        }
        read_ranges = {
            (
                excerpt["evidence_id"],
                excerpt["source_sha256"],
                excerpt["byte_offset"],
                excerpt["returned_bytes"],
            )
            for event in self.repository.list_recent_events(investigation_id)
            if event["event_type"] == "evidence.excerpt_read"
            for excerpt in [event["data"]["excerpt"]]
            if current_hashes.get(excerpt["evidence_id"]) == excerpt["source_sha256"]
        }
        material = {
            "read_ranges": sorted(read_ranges),
            "observations": [
                {
                    "evidence_id": item.evidence_id,
                    "source_type": item.source_type,
                    "source_evidence_id": item.source_evidence_id,
                    "source_revision": item.source_revision,
                    "retrieval_status": item.retrieval_status,
                }
                for item in evidence
            ],
            "runs": [
                {
                    "action_id": item["action"]["action_id"],
                    "state": item["state"],
                    "run_ref": (item.get("operation_ref") or {}).get("run_ref"),
                    "execution_status": (item.get("operation_ref") or {}).get("execution_status"),
                }
                for item in actions
            ],
            "hypotheses": [
                {
                    "hypothesis_id": item.hypothesis_id,
                    "revision": item.revision,
                    "status": item.status,
                    "supporting": item.supporting_evidence_ids,
                    "contradicting": item.contradicting_evidence_ids,
                }
                for item in hypotheses
            ],
            "assessment": (
                {
                    "criterion_id": assessment.criterion_id,
                    "state_revision": assessment.evaluated_state_revision,
                    "verdict": assessment.verdict,
                    "missing": assessment.missing_requirements,
                    "supporting": assessment.supporting_fact_ids,
                    "contradicting": assessment.contradicting_fact_ids,
                }
                if assessment
                else None
            ),
            "plan": plan.model_dump(mode="json") if plan else None,
            "user_inputs": [item["command_id"] for item in user_inputs],
            "analysis_jobs": [
                {
                    "job_id": item.spec.job_id,
                    "state": item.state,
                    "operation_id": (
                        item.operation_ref.operation_id if item.operation_ref else None
                    ),
                    "fencing_token_hash": (
                        payload_hash(item.operation_ref.fencing_token)
                        if item.operation_ref
                        else None
                    ),
                    "result_ingestion_state": (
                        result.ingestion_state
                        if (result := self.repository.get_latest_analysis_result(item.spec.job_id))
                        else None
                    ),
                }
                for item in analysis_jobs
            ],
            "derived_fact_candidates": [
                {
                    "candidate_id": item.candidate_id,
                    "revision": item.revision,
                    "status": item.status,
                }
                for item in derived_candidates
            ],
        }
        return payload_hash(material)
