from __future__ import annotations

from datetime import datetime, timezone
import asyncio

import pytest

from game_test_agent.activities import ActionService, ReasoningService
from game_test_agent.capabilities import CapabilityRegistry
from game_test_agent.context.tool_loop import InvestigationToolLoop
from game_test_agent.contracts.models import (
    BuildRef,
    CapabilityDescriptor,
    CandidateDesign,
    CandidateDraft,
    EvidenceLocator,
    EvidenceRecord,
    Hypothesis,
    InvestigationRequest,
    NextStep,
    ObservablePrediction,
    RunRef,
    RunResult,
    TestObjective as Objective,
)
from game_test_agent.evidence import ContextBuilder, EvidenceManifestIngestor, EvidenceService
from game_test_agent.mcp_gateway import ActionDispatcher
from game_test_agent.planning import (
    ActionPlanner, CandidateDesignService, CandidateSelectionService, DurableActionPlanner,
)
from game_test_agent.planning.metric_comparison import MetricComparisonService
from game_test_agent.policies import PolicyEngine
from game_test_agent.reports import ReportService
from game_test_agent.storage import LocalArtifactStore
from game_test_agent.workflows import InvestigationController
from game_test_agent.workflows.worker import CoreActivities
from game_test_agent.runtime import InvestigationKernel


def _setup(
    repository, settings, *, mime="text/csv", parameters=None, build=True, env=True,
    include_evidence=True, model_design=False, min_comparable_runs=1,
    metric_source_selector=None,
):
    constraints = {"enable_experiment_selection": True}
    if min_comparable_runs != 1:
        constraints["min_comparable_runs"] = min_comparable_runs
    if metric_source_selector is not None:
        constraints["metric_source_selector"] = metric_source_selector
    if model_design:
        constraints["enable_model_candidate_design"] = True
    if parameters is not None:
        constraints["action_parameters"] = parameters
    if env:
        constraints["environment_fingerprint"] = "windows-gpu-a"
        constraints["action_target_selector"] = {
            "environment_fingerprint": "windows-gpu-a"
        }
    view, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective=Objective(objective_id="diagnose", description="distinguish two causes"),
            build_ref=BuildRef(id="build-1") if build else None,
            constraints=constraints,
        ),
        "candidate-selection-" + mime.replace("/", "-") + str(build) + str(env)
        + str(include_evidence) + str(model_design) + str(min_comparable_runs)
        + str(metric_source_selector),
    )
    registry = CapabilityRegistry(repository)
    registry.register(CapabilityDescriptor.model_validate({
        "capability_id": "test.run",
        "capability_version": "1",
        "provider_id": "reference",
        "provider_revision": "1",
        "tool_name": "submit",
        "project_ids": ["game-a"],
        "input_schema": {
            "type": "object",
            "properties": {"case": {"type": "string"}},
            "required": ["case"],
            "additionalProperties": False,
        },
        "risk_level": "bounded_execution",
        "required_scopes": [],
        "execution_owner": "reference",
        "resource_domain": "device",
        "estimated_cost": {"device_minutes": 2},
        "controllable_variables": ["build_ref", "environment_fingerprint"],
    }))
    artifacts = LocalArtifactStore(settings.artifact_root)
    content = b"frame_ms\n15\n15\n15\n" if mime == "text/csv" else b"normal\nERROR shader\n"
    stored = artifacts.put_bytes("studio-a", "game-a", content)
    evidence = EvidenceRecord(
        evidence_id="source-1",
        tenant_id="studio-a",
        project_id="game-a",
        artifact_ref=stored.ref,
        kind="metrics" if mime == "text/csv" else "runtime_log",
        mime_type=mime,
        sha256=stored.sha256,
        producer="test",
        producer_version="1",
        collected_at=datetime.now(timezone.utc),
        locator=EvidenceLocator(kind="sample_window", value="all"),
        quality="verified",
        source_type="user_upload",
        source_evidence_id="source-1",
    )
    if include_evidence:
        EvidenceService(repository).ingest(view.investigation_id, evidence)
    for hypothesis_id, value in (("cpu", 15.0), ("gpu", 20.0)):
        observation_key = (
            "test_verdict" if not include_evidence else
            "columns.frame_ms.median" if mime == "text/csv" else "log.error"
        )
        observation_value = (
            ("failed" if hypothesis_id == "cpu" else "passed")
            if not include_evidence else value if mime == "text/csv" else hypothesis_id
        )
        unit = "ms" if include_evidence and mime == "text/csv" else None
        read_capability = "csv_summary_v1" if mime == "text/csv" else "retrieve_evidence"
        repository.save_hypothesis(Hypothesis(
            hypothesis_id=hypothesis_id,
            investigation_id=view.investigation_id,
            revision=1,
            statement=f"{hypothesis_id} caused the anomaly",
            scope="build-1",
            missing_evidence=["independent metric summary"],
            falsifiable_prediction=f"frame_ms near {value}",
            observable_predictions=([] if not include_evidence else [
                ObservablePrediction(
                    observation_key=observation_key,
                    comparator="eq",
                    value=observation_value,
                    unit=unit,
                    capability_id=read_capability,
                )]) + [ObservablePrediction(
                    observation_key=observation_key,
                    comparator="eq",
                    value=observation_value,
                    unit=unit,
                    capability_id="test.run",
                ),
            ],
        ))
    context = ContextBuilder(repository, registry).build(view.investigation_id, scopes=set())
    return view.investigation_id, context


def _experiment():
    return NextStep(
        kind="experiment",
        objective_id="diagnose",
        capability_id="test.run",
        provider_id="reference",
        evidence_ids=["source-1"],
        expected_observation="collect another frame metric",
        reason_summary="repeat on the same target",
    )


def _draft(context, *, candidate_id="run-check", parameters=None):
    predictions = {
        item.hypothesis_id: [
            prediction for prediction in item.observable_predictions
            if prediction.capability_id == "test.run"
        ]
        for item in context.working_state.active_hypotheses
    }
    return CandidateDraft(
        candidate_id=candidate_id,
        question="Which hypothesis predicts the verdict?",
        kind="experiment",
        hypothesis_ids=list(predictions),
        capability_id="test.run",
        provider_id="reference",
        parameters={"case": "perf"} if parameters is None else parameters,
        predictions=predictions,
        expected_observation="A structured test verdict",
        reason_summary="Compare the two registered predictions",
    )


def test_model_candidate_design_is_separate_cached_and_selectable(repository, settings):
    investigation_id, context = _setup(
        repository, settings, parameters={"case": "perf"}, include_evidence=False
    )
    context.investigation.request.constraints["enable_model_candidate_design"] = True

    class Designer:
        model_name = "scripted-designer"
        calls = 0

        async def design_candidates(self, _context):
            self.calls += 1
            return CandidateDesign(candidates=[_draft(context)])

    designer = Designer()
    service = CandidateDesignService(repository, designer)
    design = asyncio.run(service.design(context, plan_revision=1))
    assert asyncio.run(service.design(context, plan_revision=1)) == design
    assert designer.calls == 1
    selected, record = CandidateSelectionService(repository).choose(
        context,
        NextStep(
            kind="search_evidence", objective_id="diagnose", search_query="test failure",
            expected_observation="Find indexed failures", reason_summary="Search first",
        ),
        plan_revision=1,
        design=design,
    )
    assert selected.kind == "experiment"
    assert record.selected_candidate_id == "draft-run-check"
    assert any(event["event_type"] == "candidate.design"
               for event in repository.list_recent_events(investigation_id))


def test_model_drafts_cannot_invent_parameters_or_predictions(repository, settings):
    _, context = _setup(
        repository, settings, parameters={"case": "perf"}, include_evidence=False
    )
    invented_parameters = _draft(context, parameters={"unknown_knob": True})
    invalid_scope = _draft(context, candidate_id="wrong-scope")
    invalid_scope = invalid_scope.model_copy(update={
        "kind": "search_evidence", "search_query": "existing evidence",
        "parameters": {},
    })
    selected, record = CandidateSelectionService(repository).choose(
        context, NextStep(
            kind="search_evidence", objective_id="diagnose", search_query="existing evidence",
            expected_observation="Relevant evidence IDs", reason_summary="Search inventory",
        ), plan_revision=1,
        design=CandidateDesign(candidates=[invented_parameters, invalid_scope]),
    )
    assert selected.kind == "search_evidence"
    by_id = {item.candidate_id: item for item in record.assessments}
    assert "parameters_not_executable_as_selected" in by_id["draft-run-check"].reasons
    assert "prediction_not_valid_for_operation" in by_id["draft-wrong-scope"].reasons
    assert not by_id["draft-run-check"].executable
    assert not by_id["draft-wrong-scope"].executable


def test_missing_structured_verdict_cannot_weaken_a_hypothesis():
    prediction = ObservablePrediction(
        observation_key="test_verdict", comparator="eq", value="failed",
        capability_id="test.run",
    )
    assert CandidateSelectionService._matches({"test_verdict": None}, prediction) is None


def test_candidate_limit_and_model_failure_are_audited(repository, settings):
    investigation_id, context = _setup(
        repository, settings, parameters={"case": "perf"}, include_evidence=False
    )
    context.investigation.request.constraints["enable_model_candidate_design"] = True

    class FailingDesigner:
        model_name = "scripted-failure"
        calls = 0

        async def design_candidates(self, _context):
            self.calls += 1
            raise TimeoutError("designer unavailable")

    designer = FailingDesigner()
    service = CandidateDesignService(repository, designer)
    assert asyncio.run(service.design(context, plan_revision=1)).candidates == []
    assert asyncio.run(service.design(context, plan_revision=1)).candidates == []
    assert designer.calls == 1
    assert any(event["event_type"] == "candidate.design_failed"
               for event in repository.list_recent_events(investigation_id))

    design = CandidateDesign(candidates=[
        _draft(context, candidate_id=f"run-{number}") for number in range(3)
    ])
    _, record = CandidateSelectionService(repository).choose(
        context,
        NextStep(
            kind="search_evidence", objective_id="diagnose", search_query="failures",
            expected_observation="Indexed failure evidence", reason_summary="Search inventory",
        ),
        plan_revision=2, design=design,
    )
    assert len(record.candidates) == 3
    assert any(item.candidate_id == "draft-run-2"
               and item.reasons == ["candidate_limit"] for item in record.assessments)


def test_cheaper_existing_analysis_wins_when_discrimination_is_equal(repository, settings):
    investigation_id, context = _setup(repository, settings, parameters={"case": "perf"})
    selector = CandidateSelectionService(repository)
    selected, record = selector.choose(context, _experiment(), plan_revision=1)
    assert selected.kind == "analyze"
    assert selected.analysis_request.recipe_id == "csv_summary_v1"
    assert record.selected_candidate_id == "analyze-source-1"
    assert {item.discriminated_pairs for item in record.assessments} == {1}
    selector.record(record)
    selector.record(record)
    assert len([event for event in repository.list_recent_events(investigation_id)
                if event["event_type"] == "candidate.selection"]) == 1


def test_unsupported_parameters_and_missing_comparability_reject_run(repository, settings):
    _, context = _setup(
        repository, settings, parameters={"invented": True}, build=False, env=False
    )
    selected, record = CandidateSelectionService(repository).choose(
        context, _experiment(), plan_revision=1
    )
    assert selected.kind == "analyze"
    rejected = next(item for item in record.assessments if item.candidate_id == "model-step")
    assert not rejected.executable
    assert "parameters_fail_capability_schema" in rejected.reasons
    assert "build_ref_missing_for_comparison" in rejected.reasons
    assert "environment_fingerprint_missing_for_comparison" in rejected.reasons


def test_log_body_is_read_before_an_unnecessary_repeat(repository, settings):
    _, context = _setup(repository, settings, mime="text/plain", parameters={"case": "functional"})
    selected, record = CandidateSelectionService(repository).choose(
        context, _experiment(), plan_revision=1
    )
    assert selected.kind == "retrieve_evidence"
    assert record.selected_candidate_id == "read-source-1-0"
    assert next(item for item in record.assessments
                if item.candidate_id == "read-source-1-0").discriminated_pairs == 0


def test_local_controller_executes_selected_analysis_without_new_run(repository, settings):
    investigation_id, context = _setup(
        repository, settings, parameters={"case": "perf"}, model_design=True
    )

    class RepeatThenReport:
        model_name = "scripted-candidates"
        calls = 0
        design_calls = 0

        async def design_candidates(self, _context):
            self.design_calls += 1
            return CandidateDesign(candidates=[CandidateDraft(
                candidate_id="summarize-csv",
                question="Which frame time prediction matches the data?",
                kind="analyze",
                hypothesis_ids=[item.hypothesis_id
                                for item in context.working_state.active_hypotheses],
                evidence_id="source-1",
                recipe_id="csv_summary_v1",
                predictions={
                    item.hypothesis_id: [prediction for prediction
                                         in item.observable_predictions
                                         if prediction.capability_id == "csv_summary_v1"]
                    for item in context.working_state.active_hypotheses
                },
                expected_observation="CSV summary with frame time median",
                reason_summary="Use existing measurements",
            )])

        async def reason_next_step(self, context):
            self.calls += 1
            if self.calls == 1:
                return _experiment()
            assert any(
                item["kind"] == "analysis_recipe/csv_summary_v1"
                for item in context["evidence"]
            )
            return NextStep(
                kind="report", objective_id="diagnose",
                expected_observation="report bounded observations",
                reason_summary="analysis_complete",
            )

    registry = CapabilityRegistry(repository)
    investigator = RepeatThenReport()
    controller = InvestigationController(
        repository, registry,
        ReasoningService(repository, investigator),
        ActionPlanner("disabled", enabled=False),
        ActionService(repository, PolicyEngine(repository, registry), ActionDispatcher()),
        EvidenceManifestIngestor(repository, LocalArtifactStore(settings.artifact_root)),
        ReportService(repository),
        provider_lookup=lambda _provider_id: None,
    )
    report = asyncio.run(
        controller.run(investigation_id, principal_id="candidate-test", scopes=set())
    )
    assert report is not None and report.stop_reason == "analysis_complete"
    assert investigator.design_calls == 1
    assert repository.list_actions(investigation_id) == []
    assert any(event["event_type"] == "candidate.selection"
               for event in repository.list_recent_events(investigation_id))
    assert any(event["event_type"] == "candidate.design"
               for event in repository.list_recent_events(investigation_id))
    outcome_event = next(
        event for event in repository.list_recent_events(investigation_id)
        if event["event_type"] == "candidate.outcome"
    )
    results = {
        item["hypothesis_id"]: item["result"]
        for item in outcome_event["data"]["outcomes"]
    }
    assert results == {"cpu": "supports", "gpu": "weakens"}
    states = {item.hypothesis_id: item for item in repository.list_hypotheses(investigation_id)}
    assert states["cpu"].status == "supported"
    assert states["gpu"].status == "unresolved"
    assert outcome_event["data"]["evidence_id"] in states["cpu"].supporting_evidence_ids


def test_temporal_activity_uses_same_selection_and_recipe(repository, settings):
    investigation_id, _ = _setup(repository, settings, parameters={"case": "perf"})

    class ProposeRepeat:
        model_name = "scripted-temporal-candidates"

        async def reason_next_step(self, _context):
            return _experiment()

    activities = CoreActivities(repository, settings)
    activities.investigator = ProposeRepeat()
    activities.reasoning = ReasoningService(repository, activities.investigator)
    selected = asyncio.run(activities.reason_next_step(investigation_id, 1))
    assert selected["kind"] == "analyze"
    outcome = asyncio.run(activities.execute_readonly_decision(investigation_id, 1))
    assert outcome["kind"] == "analyze"
    replayed = asyncio.run(activities.execute_readonly_decision(investigation_id, 1))
    assert replayed == outcome
    assert repository.get_evidence(outcome["evidence_id"]).kind == (
        "analysis_recipe/csv_summary_v1"
    )
    assert repository.list_actions(investigation_id) == []
    assert len([event for event in repository.list_recent_events(investigation_id)
                if event["event_type"] == "candidate.outcome"]) == 1


def test_temporal_activity_uses_model_designed_candidate(repository, settings):
    investigation_id, context = _setup(
        repository, settings, parameters={"case": "perf"},
        include_evidence=False, model_design=True,
    )

    class Designer:
        model_name = "scripted-temporal-designer"
        design_calls = 0

        async def reason_next_step(self, _context):
            return NextStep(
                kind="search_evidence", objective_id="diagnose",
                search_query="failure", expected_observation="Matching evidence",
                reason_summary="Search inventory",
            )

        async def design_candidates(self, _context):
            self.design_calls += 1
            return CandidateDesign(candidates=[_draft(context)])

    activities = CoreActivities(repository, settings)
    investigator = Designer()
    activities.investigator = investigator
    activities.reasoning = ReasoningService(repository, investigator)
    activities.candidate_design = CandidateDesignService(repository, investigator)
    selected = asyncio.run(activities.reason_next_step(investigation_id, 1))
    assert selected["kind"] == "experiment"
    assert investigator.design_calls == 1
    assert repository.get_decision_record(investigation_id, 1).decision.kind == "experiment"
    assert any(event["event_type"] == "candidate.design"
               for event in repository.list_recent_events(investigation_id))


def test_prediction_coverage_ignores_uncheckable_text_and_mismatched_units():
    selector = CandidateSelectionService._discriminated_pairs
    assert selector({"a": [], "b": []}) == 0
    assert selector({
        "a": [ObservablePrediction(
            observation_key="columns.frame_ms.median", comparator="eq",
            value=10, unit="ms", capability_id="csv_summary_v1",
        )],
        "b": [ObservablePrediction(
            observation_key="columns.frame_ms.median", comparator="eq",
            value=20, unit="fps", capability_id="csv_summary_v1",
        )],
    }) == 0
    assert selector({
        "a": [ObservablePrediction(
            observation_key="columns.frame_ms.median", comparator="eq",
            value=15, unit="ms", capability_id="csv_summary_v1",
            absolute_tolerance=2,
        )],
        "b": [ObservablePrediction(
            observation_key="columns.frame_ms.median", comparator="eq",
            value=18, unit="ms", capability_id="csv_summary_v1",
            absolute_tolerance=2,
        )],
    }) == 0


def test_numeric_summary_requires_enough_samples_and_nonoverlapping_range():
    match = CandidateSelectionService._matches
    prediction = ObservablePrediction(
        observation_key="columns.frame_ms.median", comparator="gt",
        value=20, unit="ms", capability_id="csv_summary_v1",
        absolute_tolerance=1,
    )

    def summary(low, high, count=5, unit="ms"):
        return {"columns": {"frame_ms": {
            "min": low, "max": high, "median": (low + high) / 2,
            "samples": count, "unit": unit,
        }}}

    assert match(summary(22, 26), prediction) is True
    assert match(summary(12, 18), prediction) is False
    assert match(summary(18, 22), prediction) is None
    assert match(summary(22, 26, count=2), prediction) is None
    assert match(summary(22, 26, unit="fps"), prediction) is None
    assert match(summary(22, 26, count=5), prediction.model_copy(
        update={"minimum_samples": 6}
    )) is None


def test_numeric_equality_needs_full_range_inside_tolerance():
    prediction = ObservablePrediction(
        observation_key="columns.frame_ms.median", comparator="eq",
        value=15, unit="ms", capability_id="csv_summary_v1",
        absolute_tolerance=1,
    )
    match = CandidateSelectionService._matches
    assert match({"columns": {"frame_ms": {
        "min": 14.5, "max": 15.5, "median": 15,
        "samples": 4, "unit": "ms",
    }}}, prediction) is True
    assert match({"columns": {"frame_ms": {
        "min": 14, "max": 17, "median": 15,
        "samples": 4, "unit": "ms",
    }}}, prediction) is None


def test_selection_switch_off_preserves_original_decision(repository, settings):
    _, context = _setup(repository, settings, parameters={"case": "perf"})
    context.investigation.request.constraints["enable_experiment_selection"] = False
    proposed = _experiment()
    chosen, record = CandidateSelectionService(repository).choose(
        context, proposed, plan_revision=1
    )
    assert chosen == proposed
    assert record is None


def test_no_executable_candidate_cannot_fall_through_to_new_run(repository, settings):
    _, context = _setup(
        repository, settings, parameters={"unsupported": True}, build=False, env=False
    )
    context.evidence = []
    chosen, record = CandidateSelectionService(repository).choose(
        context, _experiment().model_copy(update={"evidence_ids": []}), plan_revision=1
    )
    assert chosen.kind == "report"
    assert chosen.reason_summary == "candidate_selection:no_executable_candidate"
    assert record.selected_candidate_id is None


def test_repeat_requirement_must_fit_run_budget(repository, settings):
    _, context = _setup(
        repository, settings, parameters={"case": "perf"}, include_evidence=False
    )
    context.investigation.request.constraints["min_comparable_runs"] = 4
    chosen, record = CandidateSelectionService(repository).choose(
        context, _experiment().model_copy(update={"evidence_ids": []}), plan_revision=1
    )
    assert chosen.kind == "report"
    assert "invalid_repeat_requirement" in record.assessments[0].reasons


def test_real_run_outcome_updates_hypotheses_only_when_provenance_matches(
    repository, settings,
):
    investigation_id, context = _setup(
        repository, settings, parameters={"case": "functional"}, include_evidence=False
    )
    selector = CandidateSelectionService(repository)
    proposed = _experiment().model_copy(update={"evidence_ids": []})
    selected, selection = selector.choose(context, proposed, plan_revision=1)
    assert selected.kind == "experiment"
    kernel = InvestigationKernel(repository)
    prepared = kernel.prepare_round(
        context, plan_revision=1, model_name="test", prompt_version="test"
    )
    assert prepared.snapshot is not None
    decision = kernel.accept_decision(prepared.snapshot, selected)
    selector.record(selection)
    planned = asyncio.run(
        DurableActionPlanner(repository, ActionPlanner("disabled", enabled=False)).plan(
            prepared.snapshot.context, decision
        )
    )
    registry = CapabilityRegistry(repository)
    PolicyEngine(repository, registry).reserve(
        planned.action, principal_id="test", scopes=set()
    )
    run_ref = RunRef(provider_id="reference", project_id="game-a", run_id="run-real")
    result = RunResult(
        run_ref=run_ref,
        revision=1,
        execution_status="succeeded",
        test_verdict="passed",
        execution_source="real",
        provider_status="PASSED",
        build_ref=context.investigation.request.build_ref,
        environment_fingerprint="windows-gpu-a",
    )
    repository.record_run_result(planned.action.action_id, result)
    artifacts = LocalArtifactStore(settings.artifact_root)
    stored = artifacts.put_bytes("studio-a", "game-a", b"verified provider result")
    EvidenceService(repository).ingest(investigation_id, EvidenceRecord(
        evidence_id="real-run-evidence",
        tenant_id="studio-a",
        project_id="game-a",
        run_ref=run_ref,
        build_ref=result.build_ref,
        environment_fingerprint=result.environment_fingerprint,
        execution_source="real",
        artifact_ref=stored.ref,
        kind="test_report",
        mime_type="text/plain",
        sha256=stored.sha256,
        producer="reference",
        producer_version="1",
        collected_at=datetime.now(timezone.utc),
        locator=EvidenceLocator(kind="sample_window", value="all"),
        quality="verified",
        source_type="run",
        source_evidence_id="real-run-report",
        action_id=planned.action.action_id,
    ))
    repository.mark_run_result_ingestion(planned.action.action_id, 1, "ingested")
    outcome = selector.observe_run(investigation_id, 1, result)
    assert outcome is not None
    assert {item.hypothesis_id: item.result for item in outcome.outcomes} == {
        "cpu": "weakens", "gpu": "supports"
    }
    assert outcome.evidence_id == "real-run-evidence"
    assert selector.observe_run(investigation_id, 1, result) == outcome

    mismatched = result.model_copy(update={
        "revision": 2, "environment_fingerprint": "other-environment",
        "test_verdict": "unknown",
    })
    repository.record_run_result(planned.action.action_id, mismatched)
    repository.mark_run_result_ingestion(planned.action.action_id, 2, "ingested")
    unknown = selector.observe_run(investigation_id, 1, mismatched)
    assert unknown is not None
    assert "environment_not_comparable" in unknown.limitations
    assert "test_verdict_unavailable" in unknown.limitations
    assert all(item.result == "unknown" for item in unknown.outcomes)


def test_repeated_real_runs_require_agreement_and_preserve_conflicts(repository, settings):
    investigation_id, context = _setup(
        repository, settings, parameters={"case": "functional"},
        include_evidence=False, min_comparable_runs=2,
    )
    selector = CandidateSelectionService(repository)
    selected, selection = selector.choose(
        context, _experiment().model_copy(update={"evidence_ids": []}), plan_revision=1
    )
    assert selected.kind == "experiment"
    kernel = InvestigationKernel(repository)
    registry = CapabilityRegistry(repository)
    planner = DurableActionPlanner(repository, ActionPlanner("disabled", enabled=False))
    artifacts = LocalArtifactStore(settings.artifact_root)
    outcomes = []

    for revision, verdict in enumerate(("failed", "failed", "passed"), start=1):
        current_context = ContextBuilder(repository, registry).build(
            investigation_id, scopes=set()
        )
        if revision == 2:
            assert "insufficient_comparable_runs" in (
                current_context.working_state.recent_candidate_outcomes[-1].limitations
            )
        prepared = kernel.prepare_round(
            current_context, plan_revision=revision,
            model_name="test", prompt_version="repeat-test",
        )
        assert prepared.snapshot is not None
        decision = kernel.accept_decision(prepared.snapshot, selected)
        selector.record(selection.model_copy(update={"plan_revision": revision}))
        planned = asyncio.run(planner.plan(prepared.snapshot.context, decision))
        PolicyEngine(repository, registry).reserve(
            planned.action, principal_id="test", scopes=set()
        )
        run_ref = RunRef(
            provider_id="reference", project_id="game-a", run_id=f"repeat-{revision}"
        )
        result = RunResult(
            run_ref=run_ref, revision=1, execution_status="succeeded",
            test_verdict=verdict, execution_source="real",
            provider_status=verdict.upper(),
            build_ref=current_context.investigation.request.build_ref,
            environment_fingerprint="windows-gpu-a",
        )
        repository.record_run_result(planned.action.action_id, result)
        stored = artifacts.put_bytes(
            "studio-a", "game-a", f"provider result {revision}".encode()
        )
        evidence_id = f"repeat-evidence-{revision}"
        EvidenceService(repository).ingest(investigation_id, EvidenceRecord(
            evidence_id=evidence_id, tenant_id="studio-a", project_id="game-a",
            run_ref=run_ref, build_ref=result.build_ref,
            environment_fingerprint=result.environment_fingerprint,
            execution_source="real", artifact_ref=stored.ref, kind="test_report",
            mime_type="text/plain", sha256=stored.sha256, producer="reference",
            producer_version="1", collected_at=datetime.now(timezone.utc),
            locator=EvidenceLocator(kind="sample_window", value="all"),
            quality="verified", source_type="run",
            source_evidence_id=evidence_id, action_id=planned.action.action_id,
        ))
        repository.mark_run_result_ingestion(planned.action.action_id, 1, "ingested")
        outcomes.append(selector.observe_run(investigation_id, revision, result))
        if revision == 1:
            for index in range(505):
                repository.record_investigation_event(
                    investigation_id, "test.noise", {"index": index}
                )

    assert "insufficient_comparable_runs" in outcomes[0].limitations
    assert all(item.result == "unknown" for item in outcomes[0].outcomes)
    assert {item.hypothesis_id: item.result for item in outcomes[1].outcomes} == {
        "cpu": "supports", "gpu": "weakens",
    }
    assert outcomes[1].compared_evidence_ids == [
        "repeat-evidence-1", "repeat-evidence-2",
    ]
    assert "conflicting_comparable_runs" in outcomes[2].limitations
    assert all(item.result == "unknown" for item in outcomes[2].outcomes)
    hypotheses = {
        item.hypothesis_id: item for item in repository.list_hypotheses(investigation_id)
    }
    assert hypotheses["cpu"].status == "unresolved"
    assert "repeat-evidence-3" in hypotheses["cpu"].contradicting_evidence_ids


@pytest.mark.parametrize(
    "case", [
        "consistent", "different_source", "configured_selector",
        "ambiguous_selector", "conflict", "noisy_sample",
    ]
)
def test_csv_summaries_compare_independent_real_runs(
    repository, settings, case,
):
    investigation_id, _ = _setup(
        repository, settings, parameters={"case": "perf"},
        include_evidence=False, min_comparable_runs=2,
        metric_source_selector=(
            {"kind": "metrics", "mime_type": "text/csv", "producer": "reference",
             "producer_version": "1"}
            if case in {"configured_selector", "ambiguous_selector"} else None
        ),
    )
    for hypothesis in repository.list_hypotheses(investigation_id):
        repository.save_hypothesis(hypothesis.model_copy(update={
            "revision": 2,
            "observable_predictions": [ObservablePrediction(
                observation_key="columns.frame_ms.median",
                comparator="gt" if hypothesis.hypothesis_id == "cpu" else "lt",
                value=20, unit="ms", capability_id="csv_summary_v1",
            )],
        }))
    registry = CapabilityRegistry(repository)
    artifacts = LocalArtifactStore(settings.artifact_root)
    kernel = InvestigationKernel(repository)
    planner = DurableActionPlanner(repository, ActionPlanner("disabled", enabled=False))
    selector = CandidateSelectionService(repository)
    tool_loop = InvestigationToolLoop(repository, artifacts)
    outcomes = []

    for run_number in (1, 2):
        run_revision = run_number * 2 - 1
        context = ContextBuilder(repository, registry).build(
            investigation_id, scopes=set()
        )
        if run_number == 2:
            assert "insufficient_comparable_runs" in (
                context.working_state.recent_candidate_outcomes[-1].limitations
            )
        prepared = kernel.prepare_round(
            context, plan_revision=run_revision,
            model_name="test", prompt_version="metric-repeat-test",
        )
        assert prepared.snapshot is not None
        decision = kernel.accept_decision(
            prepared.snapshot,
            _experiment().model_copy(update={"evidence_ids": []}),
        )
        planned = asyncio.run(planner.plan(prepared.snapshot.context, decision))
        PolicyEngine(repository, registry).reserve(
            planned.action, principal_id="test", scopes=set()
        )
        run_ref = RunRef(
            provider_id="reference", project_id="game-a", run_id=f"metric-{run_number}"
        )
        result = RunResult(
            run_ref=run_ref, revision=1, execution_status="succeeded",
            test_verdict="failed", execution_source="real",
            provider_status="FAILED", build_ref=context.investigation.request.build_ref,
            environment_fingerprint="windows-gpu-a",
        )
        repository.record_run_result(planned.action.action_id, result)
        samples = (
            b"frame_ms\n15\n15\n15\n" if case == "conflict" and run_number == 2
            else b"frame_ms\n15\n25\n25\n" if case == "noisy_sample" and run_number == 2
            else b"frame_ms\n25\n25\n25\n"
        )
        stored = artifacts.put_bytes("studio-a", "game-a", samples)
        source_id = f"metric-source-{run_number}"
        EvidenceService(repository).ingest(investigation_id, EvidenceRecord(
            evidence_id=source_id, tenant_id="studio-a", project_id="game-a",
            run_ref=run_ref, build_ref=result.build_ref,
            environment_fingerprint=result.environment_fingerprint,
            execution_source="real", artifact_ref=stored.ref, kind="metrics",
            mime_type="text/csv", sha256=stored.sha256, producer="reference",
            producer_version="1", collected_at=datetime.now(timezone.utc),
            locator=EvidenceLocator(kind="sample_window", value="all"),
            quality="verified", source_type="run",
            source_evidence_id=(
                "other-frame-csv" if case in {"different_source", "configured_selector"}
                and run_number == 2
                else "frame-csv"
            ),
            action_id=planned.action.action_id,
        ))
        repository.mark_run_result_ingestion(planned.action.action_id, 1, "ingested")
        source = repository.get_evidence(source_id)
        assert MetricComparisonService(repository, artifacts)._signature(
            investigation_id, source
        ) is not None

        analysis_revision = run_revision + 1
        context = ContextBuilder(repository, registry).build(
            investigation_id, scopes=set()
        )
        selected, selection = selector.choose(
            context,
            NextStep(
                kind="search_evidence", objective_id="diagnose",
                search_query="frame samples", expected_observation="metric sources",
                reason_summary="find metric evidence",
            ),
            plan_revision=analysis_revision,
        )
        assert selected.kind == "analyze"
        assert selected.analysis_request.input_evidence_ids == [source_id]
        prepared = kernel.prepare_round(
            context, plan_revision=analysis_revision,
            model_name="test", prompt_version="metric-repeat-test",
        )
        assert prepared.snapshot is not None
        decision = kernel.accept_decision(prepared.snapshot, selected)
        selector.record(selection)
        output_id = tool_loop.analyze(
            investigation_id, decision.decision_id, selected.analysis_request
        )
        if case == "ambiguous_selector" and run_number == 2:
            EvidenceService(repository).ingest(investigation_id, EvidenceRecord(
                evidence_id="metric-source-2-extra", tenant_id="studio-a",
                project_id="game-a", run_ref=run_ref, build_ref=result.build_ref,
                environment_fingerprint=result.environment_fingerprint,
                execution_source="real", artifact_ref=stored.ref, kind="metrics",
                mime_type="text/csv", sha256=stored.sha256, producer="reference",
                producer_version="1", collected_at=datetime.now(timezone.utc),
                locator=EvidenceLocator(kind="sample_window", value="all"),
                quality="verified", source_type="run",
                source_evidence_id="extra-frame-csv",
                action_id=planned.action.action_id,
            ))
        outcomes.append(selector.observe_analysis(
            investigation_id, analysis_revision, output_id, artifacts
        ))

    assert "insufficient_comparable_runs" in outcomes[0].limitations
    assert all(item.result == "unknown" for item in outcomes[0].outcomes)
    if case in {"consistent", "configured_selector"}:
        assert {item.hypothesis_id: item.result for item in outcomes[1].outcomes} == {
            "cpu": "supports", "gpu": "weakens",
        }
        assert len(outcomes[1].compared_evidence_ids) == 2
        assert outcomes[1].comparison_key is not None
        fake_content = (
            b'{"columns":{"frame_ms":{"samples":3,"min":25,"max":25,'
            b'"median":25,"p95":25,"unit":"ms"}}}'
        )
        fake_artifact = artifacts.put_bytes("studio-a", "game-a", fake_content)
        EvidenceService(repository).ingest(investigation_id, EvidenceRecord(
            evidence_id="fake-summary", tenant_id="studio-a", project_id="game-a",
            artifact_ref=fake_artifact.ref, kind="analysis_recipe/csv_summary_v1",
            mime_type="application/json", sha256=fake_artifact.sha256,
            producer="analysis-recipe", producer_version="1", parser_version="1",
            collected_at=datetime.now(timezone.utc),
            locator=EvidenceLocator(kind="sample_window", value="complete output"),
            quality="verified", source_type="analysis",
            source_evidence_id="fake-summary-output", analysis_job_id="fake-job",
            derived_from=["metric-source-2"],
        ))
        with pytest.raises(ValueError, match="matching completed recipe event"):
            selector.observe_analysis(investigation_id, 4, "fake-summary", artifacts)
    else:
        assert all(item.result == "unknown" for item in outcomes[1].outcomes)
        if case == "different_source":
            assert "insufficient_comparable_runs" in outcomes[1].limitations
        if case == "ambiguous_selector":
            assert "run_metric_source_not_comparable" in outcomes[1].limitations
        if case == "conflict":
            assert "conflicting_comparable_runs" in outcomes[1].limitations
            hypotheses = {
                item.hypothesis_id: item
                for item in repository.list_hypotheses(investigation_id)
            }
            assert hypotheses["cpu"].status == "unresolved"
            assert hypotheses["cpu"].contradicting_evidence_ids
        if case == "noisy_sample":
            assert "conflicting_comparable_runs" not in outcomes[1].limitations
