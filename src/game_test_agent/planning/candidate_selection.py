from __future__ import annotations

from game_test_agent.planning.controls import execution_controls

import hashlib
import json
import math
from itertools import combinations

from jsonschema import Draft202012Validator

from game_test_agent.contracts.models import (
    AnalysisRequest,
    CandidateAssessment,
    CandidateDesign,
    CandidateDraft,
    CandidateOutcomeRecord,
    ContextBundle,
    ExperimentCandidate,
    HypothesisOutcome,
    HypothesisStatus,
    NextStep,
    ObservablePrediction,
    ReadRequest,
    RunResult,
    SelectionRecord,
)
from game_test_agent.planning.decision_validator import (
    DecisionValidationError,
    DecisionValidator,
)
from game_test_agent.planning.metric_comparison import MetricComparisonService
from game_test_agent.storage import ArtifactStore, Repository, payload_hash


class CandidateSelectionService:
    """Compare at most three real, bounded next operations using explicit evidence gaps."""

    VERSION = "candidate-selector-v1"

    def __init__(self, repository: Repository) -> None:
        self.repository = repository
        self.validator = DecisionValidator()

    def _candidate_events(self, investigation_id: str) -> list[dict]:
        events: list[dict] = []
        cursor = 0
        while True:
            page = self.repository.list_events(investigation_id, after=cursor, limit=500)
            events.extend(
                event for event in page if event["event_type"].startswith("candidate.")
            )
            if len(page) < 500:
                return events
            cursor = page[-1]["cursor"]

    def choose(
        self, context: ContextBundle, proposed: NextStep, *, plan_revision: int,
        design: CandidateDesign | None = None,
    ) -> tuple[NextStep, SelectionRecord | None]:
        if context.investigation.request.constraints.get("enable_experiment_selection") is not True:
            return proposed, None
        if proposed.kind == "request_input":
            return proposed, None
        active = context.working_state.active_hypotheses
        gaps = [gap for item in active for gap in item.missing_evidence]
        if len(active) < 2 and not gaps:
            return proposed, None
        candidates, dropped = self._generate(context, proposed, design)
        if not candidates:
            return proposed, None
        assessments = [self._assess(context, item, gaps) for item in candidates]
        assessments.extend(
            CandidateAssessment(
                candidate_id=f"draft-{item.candidate_id}",
                executable=False,
                reasons=["candidate_limit"],
            ) for item in dropped
        )
        eligible = [
            (candidate, assessment)
            for candidate, assessment in zip(candidates, assessments[:len(candidates)])
            if assessment.executable
        ]
        chosen = max(eligible, key=lambda item: self._rank(item[0], item[1]))[0] if eligible else None
        for assessment in assessments:
            if not assessment.executable:
                continue
            assessment.reasons.append(
                "selected" if chosen and assessment.candidate_id == chosen.candidate_id
                else "lower_rank_than_selected"
            )
        record = SelectionRecord(
            investigation_id=context.investigation.investigation_id,
            plan_revision=plan_revision,
            selected_candidate_id=chosen.candidate_id if chosen else None,
            candidates=candidates,
            assessments=assessments,
            selector_version=self.VERSION,
        )
        if chosen is not None:
            return chosen.decision, record
        if proposed.kind == "report":
            return proposed, record
        return NextStep(
            kind="report",
            objective_id=context.investigation.request.objective.objective_id,
            expected_observation="No executable candidate remains under current constraints.",
            reason_summary="candidate_selection:no_executable_candidate",
        ), record

    def record(self, selection: SelectionRecord | None) -> None:
        if selection is None:
            return
        for event in self._candidate_events(selection.investigation_id):
            if (
                event["event_type"] == "candidate.selection"
                and event["data"].get("plan_revision") == selection.plan_revision
            ):
                if event["data"] != selection.model_dump(mode="json"):
                    raise ValueError("plan revision already has another candidate selection")
                return
        self.repository.record_investigation_event(
            selection.investigation_id,
            "candidate.selection",
            selection.model_dump(mode="json"),
        )

    def observe_analysis(
        self, investigation_id: str, plan_revision: int, evidence_id: str,
        artifacts: ArtifactStore,
    ) -> CandidateOutcomeRecord | None:
        events = self._candidate_events(investigation_id)
        selected_event = next(
            (
                event for event in reversed(events)
                if event["event_type"] == "candidate.selection"
                and event["data"].get("plan_revision") == plan_revision
            ), None,
        )
        if selected_event is None:
            return None
        selection = SelectionRecord.model_validate(selected_event["data"])
        candidate = next(
            (item for item in selection.candidates
             if item.candidate_id == selection.selected_candidate_id), None
        )
        if candidate is None or candidate.decision.kind != "analyze":
            return None
        decision_record = self.repository.get_decision_record(investigation_id, plan_revision)
        if decision_record is None or decision_record.decision != candidate.decision:
            raise ValueError("analysis result does not belong to the selected decision")
        prior = next(
            (
                event for event in events
                if event["event_type"] == "candidate.outcome"
                and event["data"].get("plan_revision") == plan_revision
                and event["data"].get("evidence_id") == evidence_id
            ), None,
        )
        if prior is not None:
            return CandidateOutcomeRecord.model_validate(prior["data"])
        evidence = self.repository.get_evidence(evidence_id)
        if evidence_id not in {
            item.evidence_id for item in self.repository.list_current_evidence(investigation_id)
        } or evidence.source_type != "analysis":
            raise ValueError("candidate outcome requires current derived evidence")
        if evidence.derived_from != candidate.decision.analysis_request.input_evidence_ids:
            raise ValueError("analysis output does not match selected candidate inputs")
        if (
            evidence.kind != (
                f"analysis_recipe/{candidate.decision.analysis_request.recipe_id}"
            )
            or evidence.producer_version != candidate.decision.analysis_request.recipe_version
        ):
            raise ValueError("analysis output does not match selected recipe")
        source = self.repository.get_evidence(evidence.derived_from[0])
        if not any(
            event["data"].get("decision_id") == decision_record.decision_id
            and event["data"].get("recipe_id") == (
                candidate.decision.analysis_request.recipe_id
            )
            and event["data"].get("recipe_version") == (
                candidate.decision.analysis_request.recipe_version
            )
            and event["data"].get("input_evidence_ids") == [source.evidence_id]
            and event["data"].get("input_sha256") == [source.sha256]
            and event["data"].get("output_evidence_id") == evidence.evidence_id
            and event["data"].get("output_sha256") == evidence.sha256
            for event in self.repository.list_recent_events_by_type(
                investigation_id, "analysis.recipe_completed", limit=500
            )
        ):
            raise ValueError("analysis output lacks a matching completed recipe event")
        content = artifacts.read_bytes(evidence.artifact_ref)
        if hashlib.sha256(content).hexdigest() != evidence.sha256:
            raise IOError("analysis output hash mismatch")
        output = json.loads(content)
        if not isinstance(output, dict):
            raise ValueError("analysis output must be a JSON object")
        metric_batch = MetricComparisonService(self.repository, artifacts).collect(
            investigation_id, evidence, output, candidate
        )
        samples = (
            metric_batch.samples if metric_batch is not None
            else [(evidence_id, output)]
        )
        limitations = list(metric_batch.limitations) if metric_batch else []
        compared_evidence_ids = (
            metric_batch.compared_evidence_ids if metric_batch else [evidence_id]
        )
        hypotheses = {
            item.hypothesis_id: item
            for item in self.repository.list_hypotheses(investigation_id)
        }
        outcomes: list[HypothesisOutcome] = []
        conflicting_repeats = False
        for hypothesis_id, predictions in candidate.predictions.items():
            checks_by_sample = [
                [self._matches(sample_output, prediction) for prediction in predictions]
                for _, sample_output in samples
            ]
            sample_matches = [
                None if not checks or None in checks else all(checks)
                for checks in checks_by_sample
            ]
            result = (
                "unknown" if limitations or not sample_matches or None in sample_matches
                else "supports" if all(sample_matches)
                else "weakens" if not any(sample_matches) else "unknown"
            )
            if not limitations and True in sample_matches and False in sample_matches:
                conflicting_repeats = True
            outcomes.append(HypothesisOutcome(
                hypothesis_id=hypothesis_id,
                result=result,
                observation_keys=[item.observation_key for item in predictions],
            ))
            current = hypotheses.get(hypothesis_id)
            if (
                current is not None and not limitations
                and True in sample_matches and False in sample_matches
            ):
                supporting = list(current.supporting_evidence_ids)
                contradicting = list(current.contradicting_evidence_ids)
                for (sample_evidence_id, _), matches in zip(samples, sample_matches):
                    if matches is True and sample_evidence_id not in contradicting:
                        if sample_evidence_id not in supporting:
                            supporting.append(sample_evidence_id)
                    elif matches is False and sample_evidence_id not in supporting:
                        if sample_evidence_id not in contradicting:
                            contradicting.append(sample_evidence_id)
                if (
                    supporting != current.supporting_evidence_ids
                    or contradicting != current.contradicting_evidence_ids
                    or current.status != HypothesisStatus.UNRESOLVED
                ):
                    from game_test_agent.evidence.ledger import HypothesisLedger

                    HypothesisLedger(self.repository).apply(current.model_copy(update={
                        "revision": current.revision + 1,
                        "supporting_evidence_ids": supporting,
                        "contradicting_evidence_ids": contradicting,
                        "status": HypothesisStatus.UNRESOLVED,
                    }))
                continue
            if current is None or result == "unknown":
                continue
            supporting = list(current.supporting_evidence_ids)
            contradicting = list(current.contradicting_evidence_ids)
            target = supporting if result == "supports" else contradicting
            opposite = contradicting if result == "supports" else supporting
            if any(item in opposite for item in compared_evidence_ids):
                outcomes[-1] = outcomes[-1].model_copy(update={"result": "unknown"})
                continue
            new_evidence_ids = [
                item for item in compared_evidence_ids if item not in target
            ]
            if not new_evidence_ids:
                continue
            target.extend(new_evidence_ids)
            status = (
                HypothesisStatus.SUPPORTED
                if result == "supports" and evidence.quality in {"verified", "partial"}
                else HypothesisStatus.UNRESOLVED
            )
            updated = current.model_copy(update={
                "revision": current.revision + 1,
                "supporting_evidence_ids": supporting,
                "contradicting_evidence_ids": contradicting,
                "status": status,
            })
            from game_test_agent.evidence.ledger import HypothesisLedger

            HypothesisLedger(self.repository).apply(updated)
        if conflicting_repeats:
            limitations.append("conflicting_comparable_runs")
        record = CandidateOutcomeRecord(
            investigation_id=investigation_id,
            plan_revision=plan_revision,
            candidate_id=candidate.candidate_id,
            evidence_id=evidence_id,
            observation_ref=f"evidence://{evidence_id}",
            action_id=metric_batch.action_id if metric_batch else None,
            comparison_key=metric_batch.comparison_key if metric_batch else None,
            compared_evidence_ids=compared_evidence_ids,
            outcomes=outcomes,
            evaluator_version=(
                "candidate-metric-outcome-v1" if metric_batch
                else "candidate-outcome-v1"
            ),
            limitations=limitations,
        )
        self.repository.record_investigation_event(
            investigation_id, "candidate.outcome", record.model_dump(mode="json")
        )
        return record

    def observe_run(
        self, investigation_id: str, plan_revision: int, result: RunResult
    ) -> CandidateOutcomeRecord | None:
        events = self._candidate_events(investigation_id)
        selected_event = next(
            (
                event for event in reversed(events)
                if event["event_type"] == "candidate.selection"
                and event["data"].get("plan_revision") == plan_revision
            ), None,
        )
        if selected_event is None:
            return None
        selection = SelectionRecord.model_validate(selected_event["data"])
        candidate = next(
            (item for item in selection.candidates
             if item.candidate_id == selection.selected_candidate_id), None
        )
        if candidate is None or candidate.decision.kind != "experiment":
            return None
        observation_ref = (
            f"run://{result.run_ref.provider_id}/{result.run_ref.project_id}/"
            f"{result.run_ref.run_id}@{result.revision}"
        )
        prior = next(
            (
                event for event in events
                if event["event_type"] == "candidate.outcome"
                and event["data"].get("plan_revision") == plan_revision
                and event["data"].get("observation_ref") == observation_ref
            ), None,
        )
        if prior is not None:
            return CandidateOutcomeRecord.model_validate(prior["data"])
        investigation = self.repository.get_investigation(investigation_id)
        snapshot = self.repository.get_run_result_by_ref(
            investigation.request.tenant_id, result.run_ref, result.revision
        )
        planned = self.repository.get_planned_action(investigation_id, plan_revision)
        decision = self.repository.get_decision_record(investigation_id, plan_revision)
        if (
            snapshot is None or snapshot.result != result or planned is None
            or decision is None or planned.decision_id != decision.decision_id
            or decision.decision != candidate.decision
            or planned.action.action_id != snapshot.action_id
        ):
            raise ValueError("Run result does not belong to the selected planned action")
        sources = sorted(
            (
                item for item in self.repository.list_current_evidence(investigation_id)
                if item.run_ref == result.run_ref and item.retrieval_status == "ready"
                and item.source_type == "run"
                and item.action_id == planned.action.action_id
                and item.source_revision == result.revision
                and item.build_ref == result.build_ref
                and item.environment_fingerprint == result.environment_fingerprint
                and item.execution_source == result.execution_source
            ),
            key=lambda item: (item.quality not in {"verified", "partial"}, item.evidence_id),
        )
        evidence_id = sources[0].evidence_id if sources else None
        limitations: list[str] = []
        if snapshot.ingestion_state != "ingested" or not sources:
            limitations.append("run_evidence_not_ingested")
        if sources and not any(item.quality in {"verified", "partial"} for item in sources):
            limitations.append("run_evidence_not_trusted")
        if result.execution_source != "real":
            limitations.append("execution_not_real")
        expected_build = investigation.request.build_ref
        if expected_build is None or result.build_ref != expected_build:
            limitations.append("build_not_comparable")
        expected_environment = investigation.request.constraints.get("environment_fingerprint")
        if not expected_environment or result.environment_fingerprint != expected_environment:
            limitations.append("environment_not_comparable")
        if planned.action.parameters != candidate.parameters:
            limitations.append("parameters_changed_after_selection")
        if result.execution_status not in {"succeeded", "failed"}:
            limitations.append("run_not_terminal")
        if result.test_verdict in {"not_run", "unknown"} and any(
            prediction.observation_key == "test_verdict"
            for predictions in candidate.predictions.values() for prediction in predictions
        ):
            limitations.append("test_verdict_unavailable")
        minimum_runs = investigation.request.constraints.get("min_comparable_runs", 1)
        if (
            not isinstance(minimum_runs, int) or isinstance(minimum_runs, bool)
            or not 1 <= minimum_runs <= 3
            or minimum_runs > investigation.request.budget.max_real_runs
        ):
            limitations.append("invalid_repeat_requirement")
            minimum_runs = 1
        comparison_key = payload_hash({
            "capability_id": planned.action.capability_id,
            "capability_version": planned.action.capability_version,
            "provider_id": planned.action.provider_id,
            "provider_revision": planned.action.provider_revision,
            "test_ref": planned.action.test_ref,
            "parameters": planned.action.parameters,
            "target_selector": planned.action.target_selector,
            "constraints": execution_controls(planned.action.constraints),
            "build_ref": result.build_ref,
            "environment_fingerprint": result.environment_fingerprint,
            "predictions": candidate.predictions,
        })
        observable: dict[str, str | None] = {
            "test_verdict": (
                None if result.test_verdict in {"not_run", "unknown"}
                else result.test_verdict
            ),
            "execution_status": result.execution_status,
        }
        current_evidence = {
            item.evidence_id for item in self.repository.list_current_evidence(investigation_id)
        }
        comparable = [
            CandidateOutcomeRecord.model_validate(event["data"])
            for event in events if event["event_type"] == "candidate.outcome"
            and event["data"].get("comparison_key") == comparison_key
            and event["data"].get("action_id") != planned.action.action_id
            and event["data"].get("observation_ref") != observation_ref
            and event["data"].get("evidence_id") in current_evidence
            and not (set(event["data"].get("limitations", []))
                     - {"insufficient_comparable_runs", "conflicting_comparable_runs"})
        ] if not limitations and minimum_runs > 1 else []
        seen_refs: set[str] = set()
        unique_comparable: list[CandidateOutcomeRecord] = []
        for item in comparable:
            if item.observation_ref and item.observation_ref not in seen_refs:
                unique_comparable.append(item)
                seen_refs.add(item.observation_ref)
        samples = [
            (item.action_id, item.evidence_id, item.observed_values)
            for item in unique_comparable
        ]
        if not limitations and evidence_id is not None:
            samples.append((planned.action.action_id, evidence_id, observable))
        distinct: dict[str, tuple[str, dict[str, str | None]]] = {}
        for action_id, sample_evidence_id, values in samples:
            if action_id and sample_evidence_id:
                distinct[action_id] = (sample_evidence_id, values)
        samples = [(action_id, *value) for action_id, value in distinct.items()]
        if not limitations and len(samples) < minimum_runs:
            limitations.append("insufficient_comparable_runs")
        compared_evidence_ids = sorted({item[1] for item in samples})
        outcomes: list[HypothesisOutcome] = []
        conflicting_repeats = False
        hypotheses = {
            item.hypothesis_id: item
            for item in self.repository.list_hypotheses(investigation_id)
        }
        for hypothesis_id, predictions in candidate.predictions.items():
            checks_by_run = [
                [self._matches(values, prediction) for prediction in predictions]
                for _, _, values in samples
            ]
            run_matches = [
                None if not checks or None in checks else all(checks)
                for checks in checks_by_run
            ]
            verdict = (
                "unknown" if limitations or not run_matches or None in run_matches
                else "supports" if all(run_matches)
                else "weakens" if not any(run_matches) else "unknown"
            )
            if not limitations and True in run_matches and False in run_matches:
                conflicting_repeats = True
            outcomes.append(HypothesisOutcome(
                hypothesis_id=hypothesis_id,
                result=verdict,
                observation_keys=[item.observation_key for item in predictions],
            ))
            current = hypotheses.get(hypothesis_id)
            if (
                current is not None and not limitations
                and True in run_matches and False in run_matches
            ):
                supporting = list(current.supporting_evidence_ids)
                contradicting = list(current.contradicting_evidence_ids)
                for (_, sample_evidence_id, _), matches in zip(samples, run_matches):
                    if matches is True and sample_evidence_id not in contradicting:
                        if sample_evidence_id not in supporting:
                            supporting.append(sample_evidence_id)
                    elif matches is False and sample_evidence_id not in supporting:
                        if sample_evidence_id not in contradicting:
                            contradicting.append(sample_evidence_id)
                if (
                    supporting != current.supporting_evidence_ids
                    or contradicting != current.contradicting_evidence_ids
                    or current.status != HypothesisStatus.UNRESOLVED
                ):
                    from game_test_agent.evidence.ledger import HypothesisLedger

                    HypothesisLedger(self.repository).apply(current.model_copy(update={
                        "revision": current.revision + 1,
                        "supporting_evidence_ids": supporting,
                        "contradicting_evidence_ids": contradicting,
                        "status": HypothesisStatus.UNRESOLVED,
                    }))
                continue
            if current is None or verdict == "unknown" or evidence_id is None:
                continue
            supporting = list(current.supporting_evidence_ids)
            contradicting = list(current.contradicting_evidence_ids)
            target = supporting if verdict == "supports" else contradicting
            opposite = contradicting if verdict == "supports" else supporting
            if any(item in opposite for item in compared_evidence_ids):
                outcomes[-1] = outcomes[-1].model_copy(update={"result": "unknown"})
                continue
            new_evidence_ids = [
                item for item in compared_evidence_ids if item not in target
            ]
            if not new_evidence_ids:
                continue
            target.extend(new_evidence_ids)
            updated = current.model_copy(update={
                "revision": current.revision + 1,
                "supporting_evidence_ids": supporting,
                "contradicting_evidence_ids": contradicting,
                "status": (
                    HypothesisStatus.SUPPORTED if verdict == "supports"
                    else HypothesisStatus.UNRESOLVED
                ),
            })
            from game_test_agent.evidence.ledger import HypothesisLedger

            HypothesisLedger(self.repository).apply(updated)
        if conflicting_repeats:
            limitations.append("conflicting_comparable_runs")
        record = CandidateOutcomeRecord(
            investigation_id=investigation_id,
            plan_revision=plan_revision,
            candidate_id=candidate.candidate_id,
            evidence_id=evidence_id,
            observation_ref=observation_ref,
            action_id=planned.action.action_id,
            comparison_key=comparison_key,
            observed_values=observable,
            compared_evidence_ids=compared_evidence_ids,
            outcomes=outcomes,
            evaluator_version="candidate-run-outcome-v1",
            limitations=limitations,
        )
        self.repository.record_investigation_event(
            investigation_id, "candidate.outcome", record.model_dump(mode="json")
        )
        return record

    @staticmethod
    def _matches(output: dict, prediction: ObservablePrediction) -> bool | None:
        value = output
        parts = prediction.observation_key.split(".")
        for part in parts:
            if not isinstance(value, dict) or part not in value:
                return None
            value = value[part]
        if value is None:
            return None
        if prediction.unit is not None:
            if len(parts) < 3 or parts[0] != "columns":
                return None
            column = output.get("columns", {}).get(parts[1], {})
            if column.get("unit") != prediction.unit:
                return None
        expected = prediction.value
        if (
            len(parts) == 3 and parts[0] == "columns"
            and parts[2] in {"min", "max", "median", "p95"}
            and prediction.comparator in {"eq", "gt", "lt"}
        ):
            column = output.get("columns", {}).get(parts[1], {})
            lower = column.get("min")
            upper = column.get("max")
            count = column.get("samples")
            if (
                not isinstance(count, int) or isinstance(count, bool)
                or count < prediction.minimum_samples
                or not all(isinstance(item, (int, float)) and not isinstance(item, bool)
                           and math.isfinite(item) for item in (value, expected, lower, upper))
                or lower > upper or not lower <= value <= upper
            ):
                return None
            tolerance = prediction.absolute_tolerance
            if prediction.comparator == "gt":
                return (True if lower > expected + tolerance else
                        False if upper <= expected - tolerance else None)
            if prediction.comparator == "lt":
                return (True if upper < expected - tolerance else
                        False if lower >= expected + tolerance else None)
            return (True if lower >= expected - tolerance
                    and upper <= expected + tolerance else
                    False if upper < expected - tolerance
                    or lower > expected + tolerance else None)
        if prediction.comparator == "eq":
            if (
                isinstance(value, (int, float)) and not isinstance(value, bool)
                and isinstance(expected, (int, float)) and not isinstance(expected, bool)
            ):
                if not math.isfinite(value) or not math.isfinite(expected):
                    return None
                return abs(value - expected) <= prediction.absolute_tolerance
            return value == expected
        if prediction.comparator == "contains":
            return isinstance(value, str) and isinstance(expected, str) and expected in value
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not isinstance(expected, (int, float))
            or isinstance(expected, bool)
        ):
            return None
        if not math.isfinite(value) or not math.isfinite(expected):
            return None
        tolerance = prediction.absolute_tolerance
        if prediction.comparator == "gt":
            return (True if value > expected + tolerance else
                    False if value <= expected - tolerance else None)
        return (True if value < expected - tolerance else
                False if value >= expected + tolerance else None)

    def _generate(
        self, context: ContextBundle, proposed: NextStep,
        design: CandidateDesign | None = None,
    ) -> tuple[list[ExperimentCandidate], list[CandidateDraft]]:
        objective_id = context.investigation.request.objective.objective_id
        active = context.working_state.active_hypotheses
        hypothesis_ids = [item.hypothesis_id for item in active]
        question = "Which unresolved explanation matches the next observation?"
        model_step: ExperimentCandidate | None = None
        if proposed.kind not in {"report", "request_input"}:
            model_step = self._candidate(context, "model-step", question, proposed)
        automatic: list[ExperimentCandidate] = []
        derived = {
            (item.kind, tuple(item.derived_from))
            for item in context.evidence if item.source_type == "analysis"
        }
        for source in reversed(context.evidence):
            if len(automatic) >= 3:
                break
            if source.retrieval_status != "ready":
                continue
            mime = source.mime_type.split(";", 1)[0].strip().lower()
            if mime == "text/csv" and (
                "analysis_recipe/csv_summary_v1", (source.evidence_id,)
            ) not in derived:
                decision = NextStep(
                    kind="analyze", objective_id=objective_id,
                    hypothesis_ids=hypothesis_ids,
                    evidence_ids=[source.evidence_id],
                    analysis_request=AnalysisRequest(
                        recipe_id="csv_summary_v1", input_evidence_ids=[source.evidence_id]
                    ),
                    expected_observation="bounded numeric summary of existing CSV evidence",
                    reason_summary="Use existing measurements before requesting another Run.",
                )
                automatic.append(self._candidate(context, f"analyze-{source.evidence_id}", question, decision))
            elif mime == "text/plain":
                prior = [
                    item for item in context.excerpts if item.evidence_id == source.evidence_id
                    and item.source_sha256 == source.sha256
                ]
                if prior and not prior[-1].truncated:
                    continue
                offset = prior[-1].next_byte_offset if prior else 0
                decision = NextStep(
                    kind="retrieve_evidence", objective_id=objective_id,
                    hypothesis_ids=hypothesis_ids,
                    evidence_ids=[source.evidence_id],
                    read_request=ReadRequest(
                        evidence_id=source.evidence_id,
                        purpose="inspect existing log body for unresolved evidence",
                        byte_offset=offset or 0,
                    ),
                    expected_observation="a bounded excerpt of current log evidence",
                    reason_summary="Read the existing log before requesting another Run.",
                )
                automatic.append(self._candidate(context, f"read-{source.evidence_id}-{offset or 0}", question, decision))
        if design is None or not design.candidates:
            candidates = ([model_step] if model_step else []) + automatic
            return candidates[:3], []
        candidates = automatic[:1]
        room_for_drafts = max(0, 3 - len(candidates) - int(model_step is not None))
        accepted_drafts = design.candidates[:room_for_drafts]
        candidates.extend(self._from_draft(context, item) for item in accepted_drafts)
        if model_step is not None:
            candidates.append(model_step)
        return candidates[:3], design.candidates[room_for_drafts:]

    def _from_draft(
        self, context: ContextBundle, draft: CandidateDraft
    ) -> ExperimentCandidate:
        common = {
            "objective_id": context.investigation.request.objective.objective_id,
            "hypothesis_ids": draft.hypothesis_ids,
            "expected_observation": draft.expected_observation,
            "reason_summary": draft.reason_summary,
        }
        if draft.kind == "experiment":
            decision = NextStep(
                kind="experiment", capability_id=draft.capability_id,
                provider_id=draft.provider_id, **common,
            )
        elif draft.kind == "retrieve_evidence":
            decision = NextStep(
                kind="retrieve_evidence", evidence_ids=[draft.evidence_id],
                read_request=ReadRequest(
                    evidence_id=draft.evidence_id, purpose=draft.question
                ), **common,
            )
        elif draft.kind == "analyze":
            decision = NextStep(
                kind="analyze", evidence_ids=[draft.evidence_id],
                analysis_request=AnalysisRequest(
                    recipe_id=draft.recipe_id,
                    input_evidence_ids=[draft.evidence_id],
                ), **common,
            )
        elif draft.kind == "query_run":
            decision = NextStep(
                kind="query_run", target_action_id=draft.target_action_id,
                query_purpose=draft.question, **common,
            )
        else:
            decision = NextStep(
                kind="search_evidence", search_query=draft.search_query, **common,
            )
        candidate = self._candidate(
            context, f"draft-{draft.candidate_id}", draft.question, decision
        )
        return candidate.model_copy(update={
            "hypothesis_ids": draft.hypothesis_ids,
            "parameters": draft.parameters if draft.kind == "experiment" else {},
            "predictions": draft.predictions,
        })

    @staticmethod
    def _candidate(
        context: ContextBundle, candidate_id: str, question: str, decision: NextStep
    ) -> ExperimentCandidate:
        active = context.working_state.active_hypotheses
        capability = next(
            (
                item for item in context.capabilities
                if item.capability_id == decision.capability_id
                and item.provider_id == decision.provider_id
            ), None,
        )
        parameters = context.investigation.request.constraints.get("action_parameters")
        target = context.investigation.request.constraints.get("action_target_selector")
        expected_env = context.investigation.request.constraints.get("environment_fingerprint")
        environment_controlled = (
            isinstance(target, dict)
            and target.get("environment_fingerprint") == expected_env
            and isinstance(expected_env, str)
            and bool(expected_env)
            and capability is not None
            and "environment_fingerprint" in capability.controllable_variables
        )
        build_controlled = (
            capability is not None
            and "build_ref" in capability.controllable_variables
            and context.investigation.request.build_ref is not None
        )
        prediction_scope = decision.capability_id or (
            decision.analysis_request.recipe_id if decision.analysis_request else decision.kind
        )
        predictions = {
            hypothesis.hypothesis_id: [
                prediction for prediction in hypothesis.observable_predictions
                if prediction.capability_id == prediction_scope
                and decision.kind != "retrieve_evidence"
            ]
            for hypothesis in active
        }
        return ExperimentCandidate(
            candidate_id=candidate_id,
            question=question,
            decision=decision,
            hypothesis_ids=[item.hypothesis_id for item in active],
            capability_version=capability.capability_version if capability else None,
            parameters=parameters if isinstance(parameters, dict) and decision.kind == "experiment" else {},
            controlled_variables=(
                (["build_ref"] if build_controlled else [])
                + (["environment_fingerprint"] if environment_controlled else [])
            ) if decision.kind == "experiment" else [],
            changed_variables=[],
            predictions=predictions,
            evidence_requirements=["current evidence", "source version"],
            comparability_fields=["build_ref", "environment_fingerprint"] if decision.kind == "experiment" else [],
            estimated_tool_reads=1 if decision.kind in {"retrieve_evidence", "search_evidence", "analyze", "query_run", "execute_code"} else 0,
            estimated_real_runs=1 if decision.kind == "experiment" else 0,
            estimated_device_minutes=max(
                0.0, float(capability.estimated_cost.get("device_minutes", 0))
            ) if capability else 0,
            stop_rule="Stop when the requested observation is obtained or the operation fails.",
        )

    def _assess(
        self, context: ContextBundle, candidate: ExperimentCandidate, gaps: list[str]
    ) -> CandidateAssessment:
        reasons: list[str] = []
        decision = candidate.decision
        current_hypotheses = {
            item.hypothesis_id: item
            for item in context.working_state.active_hypotheses
        }
        if not set(candidate.hypothesis_ids).issubset(current_hypotheses):
            reasons.append("unknown_hypothesis")
        if not set(candidate.predictions).issubset(candidate.hypothesis_ids):
            reasons.append("prediction_outside_candidate_hypotheses")
        prediction_scope = decision.capability_id or (
            decision.analysis_request.recipe_id if decision.analysis_request else decision.kind
        )
        for hypothesis_id, predictions in candidate.predictions.items():
            known = current_hypotheses.get(hypothesis_id)
            if known is None or any(
                prediction not in known.observable_predictions
                or prediction.capability_id != prediction_scope
                for prediction in predictions
            ):
                reasons.append("prediction_not_valid_for_operation")
                break
        try:
            self.validator.validate(context, decision)
        except DecisionValidationError as error:
            reasons.append(str(error))
        budget = context.investigation.request.budget
        usage = context.budget
        if int(usage.get("tool_reads", 0)) + candidate.estimated_tool_reads > budget.max_tool_reads:
            reasons.append("tool_read_budget_exhausted")
        if int(usage.get("real_runs", 0)) + candidate.estimated_real_runs > budget.max_real_runs:
            reasons.append("real_run_budget_exhausted")
        if float(usage.get("reserved_device_minutes", 0)) + candidate.estimated_device_minutes > budget.max_device_minutes:
            reasons.append("device_minute_budget_exhausted")
        if decision.kind == "experiment":
            if not any(candidate.predictions.values()):
                reasons.append("no_checkable_prediction")
            minimum_runs = context.investigation.request.constraints.get(
                "min_comparable_runs", 1
            )
            if (
                not isinstance(minimum_runs, int) or isinstance(minimum_runs, bool)
                or not 1 <= minimum_runs <= 3
                or minimum_runs > budget.max_real_runs
            ):
                reasons.append("invalid_repeat_requirement")
            capability = next(
                (item for item in context.capabilities
                 if item.capability_id == decision.capability_id
                 and item.provider_id == decision.provider_id), None
            )
            params = context.investigation.request.constraints.get("action_parameters")
            if candidate.parameters != params:
                reasons.append("parameters_not_executable_as_selected")
            if capability is None or capability.availability != "ready":
                reasons.append("capability_unavailable")
            elif (
                "device_minutes" not in capability.estimated_cost
                or not math.isfinite(capability.estimated_cost["device_minutes"])
                or capability.estimated_cost["device_minutes"] < 0
            ):
                reasons.append("device_cost_unknown")
            elif not isinstance(params, dict):
                reasons.append("parameters_not_prevalidated")
            else:
                try:
                    Draft202012Validator(capability.input_schema).validate(params)
                except Exception:
                    reasons.append("parameters_fail_capability_schema")
            if context.investigation.request.build_ref is None:
                reasons.append("build_ref_missing_for_comparison")
            elif "build_ref" not in candidate.controlled_variables:
                reasons.append("build_control_unavailable")
            if not context.investigation.request.constraints.get("environment_fingerprint"):
                reasons.append("environment_fingerprint_missing_for_comparison")
            elif "environment_fingerprint" not in candidate.controlled_variables:
                reasons.append("environment_control_unavailable")
        pairs = self._discriminated_pairs(candidate.predictions)
        return CandidateAssessment(
            candidate_id=candidate.candidate_id,
            executable=not reasons,
            reasons=reasons,
            fills_evidence_gap=bool(gaps) and decision.kind in {
                "retrieve_evidence", "analyze", "query_run", "execute_code"
            },
            discriminated_pairs=pairs,
        )

    @staticmethod
    def _discriminated_pairs(predictions: dict[str, list[ObservablePrediction]]) -> int:
        count = 0
        for (_, left), (_, right) in combinations(sorted(predictions.items()), 2):
            if any(
                CandidateSelectionService._disjoint(a, b)
                for a in left for b in right
            ):
                count += 1
        return count

    @staticmethod
    def _disjoint(left: ObservablePrediction, right: ObservablePrediction) -> bool:
        if left.observation_key != right.observation_key or left.unit != right.unit:
            return False
        if left.comparator == right.comparator == "eq":
            if isinstance(left.value, str) or isinstance(right.value, str):
                return left.value != right.value
            return abs(left.value - right.value) > (
                left.absolute_tolerance + right.absolute_tolerance
            )
        if not isinstance(left.value, (int, float)) or not isinstance(right.value, (int, float)):
            return False
        if {left.comparator, right.comparator} == {"gt", "lt"}:
            lower = (
                left.value + left.absolute_tolerance if left.comparator == "gt"
                else right.value + right.absolute_tolerance
            )
            upper = (
                left.value - left.absolute_tolerance if left.comparator == "lt"
                else right.value - right.absolute_tolerance
            )
            return lower >= upper
        if left.comparator == "eq" and right.comparator == "gt":
            return (left.value + left.absolute_tolerance
                    <= right.value + right.absolute_tolerance)
        if left.comparator == "eq" and right.comparator == "lt":
            return (left.value - left.absolute_tolerance
                    >= right.value - right.absolute_tolerance)
        if right.comparator == "eq":
            return CandidateSelectionService._disjoint(right, left)
        return False

    @staticmethod
    def _rank(candidate: ExperimentCandidate, assessment: CandidateAssessment) -> tuple:
        return (
            int(assessment.fills_evidence_gap),
            assessment.discriminated_pairs,
            -candidate.estimated_real_runs,
            -candidate.estimated_device_minutes,
            -candidate.estimated_tool_reads,
            candidate.candidate_id,
        )
