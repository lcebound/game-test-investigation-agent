"""Provider-neutral correctness checks; no game, model or platform calls."""
import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from game_test_agent.contracts.models import (
    EvidenceAssertion, EvidenceRecord, Fact, InvestigationRequest, InvestigationStatus, ObjectiveSpec,
)
from game_test_agent.evidence.assertions import AssertionVerifier
from game_test_agent.objectives import ObjectiveEvaluator
from game_test_agent.reports import ReportService
from game_test_agent.storage import LocalArtifactStore, canonical_json
from game_test_agent.workflows.native_context import NativeMemory
from game_test_agent.workflows.native_evidence import text_consistency_errors
from game_test_agent.workflows.native_reporting import NativeDiagnosis, NativeReportPublisher


def investigation(repository, **request_fields):
    return repository.create_investigation(InvestigationRequest(
        tenant_id="studio-a", project_id="game-a",
        objective={"objective_id": "o", "description": "Evaluate recorded measurements"},
        **request_fields), "judgment-case")[0]


def evidence(repository, settings, view, eid, payload, *, run="r", build=None, environment=None, source="real"):
    store = LocalArtifactStore(settings.artifact_root)
    stored = store.put_bytes(view.request.tenant_id, view.request.project_id,
                             payload if isinstance(payload, bytes) else json.dumps(payload).encode())
    record = EvidenceRecord(evidence_id=eid, tenant_id=view.request.tenant_id, project_id=view.request.project_id,
        artifact_ref=stored.ref, sha256=stored.sha256, kind="metrics", mime_type="application/json",
        producer="recorded-provider", producer_version="1", quality="verified", execution_source=source,
        collected_at=datetime.now(timezone.utc), locator={"kind": "region", "value": "whole"},
        run_ref={"provider_id": "recorded-provider", "project_id": view.request.project_id, "run_id": run},
        build_ref=build, environment_fingerprint=environment)
    repository.add_evidence(view.investigation_id, record)
    return store, record


def metric_fact(repository, view, fid, ids, value, **data):
    item = Fact(fact_id=fid, investigation_id=view.investigation_id, statement="Recorded metric",
                evidence_ids=ids, extractor="fixture", extractor_version="1",
                data={"column": "work_units", "unit": "units", "p95": value, **data})
    repository.add_fact(item)
    return item


def specification(**fields):
    return {"criterion_id": "c", "objective_id": "o", "metric": "work_units", "unit": "units",
            "statistic": "p95", "comparison": "le", "threshold": 12, **fields}


@pytest.mark.parametrize("build,environment", [({"id": "other"}, "target-env"),
    (None, "target-env"), ({"id": "target", "revision": "old"}, "target-env"),
    ({"id": "target", "revision": "new"}, None), ({"id": "target", "revision": "new"}, "other-env")])
def test_target_scope_cannot_be_satisfied_by_missing_or_mismatched_metadata(repository, settings, build, environment):
    view = investigation(repository, build_ref={"id": "target", "revision": "new"},
                         constraints={"environment_fingerprint": "target-env", "objective_spec": specification()})
    evidence(repository, settings, view, "e", {}, build=build, environment=environment)
    metric_fact(repository, view, "f", ["e"], 4)
    result = ObjectiveEvaluator(repository).evaluate(view.investigation_id)
    assert result.verdict == "insufficient" and not result.supporting_fact_ids


def test_only_target_runs_count_and_matching_failure_still_fails(repository, settings):
    view = investigation(repository, build_ref={"id": "target"}, constraints={"objective_spec": specification()})
    evidence(repository, settings, view, "history", {}, run="history", build={"id": "old"})
    evidence(repository, settings, view, "current", {}, run="current", build={"id": "target"})
    metric_fact(repository, view, "historical-fact", ["history"], 999)
    metric_fact(repository, view, "current-fact", ["current"], 4)
    assert ObjectiveEvaluator(repository).evaluate(view.investigation_id).verdict == "passed"
    evidence(repository, settings, view, "failing", {}, run="failing", build={"id": "target"})
    metric_fact(repository, view, "failing-fact", ["failing"], 40)
    assert ObjectiveEvaluator(repository).evaluate(view.investigation_id).verdict == "failed"


def test_conflict_cannot_be_hidden_by_an_unrelated_passing_run(repository, settings):
    view = investigation(repository, constraints={"objective_spec": specification()})
    for eid in ("one", "two"):
        evidence(repository, settings, view, eid, {}, run=eid)
    metric_fact(repository, view, "good", ["one"], 4)
    metric_fact(repository, view, "a", ["two"], 4)
    metric_fact(repository, view, "b", ["two"], 40)
    result = ObjectiveEvaluator(repository).evaluate(view.investigation_id)
    assert result.verdict == "insufficient" and any("conflicting" in x for x in result.missing_requirements)


def test_explicit_fact_filter_selects_a_window_without_silently_discarding_conflict(repository, settings):
    view = investigation(repository, constraints={"objective_spec": specification(fact_filters={"phase": "baseline"})})
    evidence(repository, settings, view, "e", {})
    metric_fact(repository, view, "baseline", ["e"], 4, phase="baseline")
    metric_fact(repository, view, "load", ["e"], 40, phase="load")
    result = ObjectiveEvaluator(repository).evaluate(view.investigation_id)
    assert result.verdict == "passed" and result.supporting_fact_ids == ["baseline"]


@pytest.mark.parametrize("value", [True, False, "4"])
def test_measurement_types_do_not_coerce_to_numbers(repository, settings, value):
    view = investigation(repository, constraints={"objective_spec": specification()})
    evidence(repository, settings, view, "e", {})
    metric_fact(repository, view, "bad", ["e"], value)
    assert ObjectiveEvaluator(repository).evaluate(view.investigation_id).verdict == "insufficient"


def test_all_fact_sources_are_checked_not_only_first_record(repository, settings):
    view = investigation(repository, constraints={"objective_spec": specification()})
    evidence(repository, settings, view, "a", {}, build={"id": "a"})
    evidence(repository, settings, view, "b", {}, build={"id": "b"})
    metric_fact(repository, view, "mixed", ["a", "b"], 4)
    assert ObjectiveEvaluator(repository).evaluate(view.investigation_id).verdict == "insufficient"


@pytest.mark.parametrize("operation,expected", [("count", 4), ("min", 2), ("max", 14),
    ("mean", 8), ("p50", 8), ("p95", 13.4), ("p99", 13.88)])
def test_statistics_use_original_numbers_and_documented_interpolation(repository, settings, operation, expected):
    view = investigation(repository)
    store, _ = evidence(repository, settings, view, "e", {"a/b": {"~samples": [2, 6, 10, 14]}})
    assertion = EvidenceAssertion(evidence_id="e", pointer="/a~1b/~0samples", operation=operation, expected=expected)
    assert AssertionVerifier(repository, store, view).verify(assertion)


@pytest.mark.parametrize("payload,assertion", [
    ({"v": False}, {"pointer": "/v", "expected": True}),
    ({"v": False}, {"pointer": "/v", "expected": 0}),
    ({"v": 9}, {"pointer": "/v", "operator": "lt", "expected": 2}),
    ({"v": [2, True]}, {"pointer": "/v", "operation": "mean", "expected": 1.5}),
    ({"v": []}, {"pointer": "/v", "operation": "p95", "expected": 0}),
    ({"v": [4]}, {"pointer": "/v/-1", "expected": 4}),
    (b'{"v":1,"v":2}', {"pointer": "/v", "expected": 2}),
])
def test_false_ambiguous_or_invalid_assertions_are_rejected(repository, settings, payload, assertion):
    view = investigation(repository)
    store, _ = evidence(repository, settings, view, "e", payload)
    with pytest.raises(ValueError):
        AssertionVerifier(repository, store, view).verify(EvidenceAssertion(evidence_id="e", **assertion))


def test_json_strings_keep_whitespace_and_count_is_scoped_to_one_array(repository, settings):
    view = investigation(repository)
    store, _ = evidence(repository, settings, view, "e", {"items": [], "cursor": "more", "value": " value "})
    verifier = AssertionVerifier(repository, store, view)
    assert verifier.verify(EvidenceAssertion(evidence_id="e", pointer="/value", expected=" value "))
    rendered = verifier.verify(EvidenceAssertion(evidence_id="e", pointer="/items", operation="count", expected=0))
    assert "count=0" in rendered and "/items" in rendered
    assert "不存在" not in rendered


def test_structured_report_renders_verified_fields_and_publish_rechecks_the_proof(repository, settings):
    view = investigation(repository)
    store, _ = evidence(repository, settings, view, "e", {"completed": False, "latency": 9})
    repository.transition(view.investigation_id, InvestigationStatus.DISCOVERING)
    repository.transition(view.investigation_id, InvestigationStatus.PLANNING)
    reports = ReportService(repository, store)
    publisher = NativeReportPublisher(repository, store, reports, lambda _: None)
    diagnosis = NativeDiagnosis(summary="Model wording is not certified.", findings=[{
        "finding_id": "s", "text": "All work is complete and the cause is confirmed.",
        "citations": [{"evidence_id": "e", "quote": '"completed": false'}],
        "assertions": [{"evidence_id": "e", "pointer": "/completed", "expected": False}]}])
    report = publisher.finish(view, diagnosis, "native_mcp_investigation_complete")
    claim = next(c for c in report.claims if c.assertions)
    assert "false" in report.summary and "All work is complete" not in report.summary
    assert claim.verification_details.support == "deterministic"
    assert claim.verification_details.causality == "not_verified"
    claim.text = "The cause is confirmed."
    with pytest.raises(ValueError, match="independently computed"):
        reports.publish(report)


@pytest.mark.parametrize("statement", ["目前无法确认根因。", "并非每帧都失败。", "Root cause confirmed: false"])
def test_negated_or_uncertain_language_is_not_treated_as_confirmation(statement):
    assert not text_consistency_errors(statement, ["A limited observation."])


def test_negative_quote_cannot_license_positive_confirmation():
    assert text_consistency_errors("Root cause confirmed.", ["Root cause confirmed: false"])


def test_counterevidence_survives_ordinary_note_churn_and_omissions_are_visible():
    memory = NativeMemory(messages=[{"role": "system", "content": ""}, {"role": "user", "content": ""},
                                    {"role": "user", "content": "{}"}],
        hypotheses=[{"contradicting_evidence_ids": ["counter"], "supporting_evidence_ids": []}])
    for i in range(20):
        eid = "counter" if i == 0 else str(i)
        memory.remember_search({"evidence_id": eid, "matched": True, "result": "x" * 1100})
    assert any(n.get("evidence_id") == "counter" for n in memory.notes.values())
    assert memory.omitted_notes and len(canonical_json(list(memory.notes.values())).encode()) <= 6000


def test_search_coverage_keeps_cursor_ranges_separate_through_checkpoint(repository, settings):
    view = investigation(repository)
    store = LocalArtifactStore(settings.artifact_root)
    memory = NativeMemory(messages=[{"role": "system", "content": ""}, {"role": "user", "content": ""},
                                    {"role": "user", "content": "{}"}])
    memory.remember_search_scope({"query": "failure"}, {"complete": False, "next_cursor": {"byte_offset": 500}})
    memory.remember_search_scope({"query": "failure", "byte_offset": 500}, {"complete": True})
    memory.save(repository, store, view)
    restored = NativeMemory.restore(repository, store, view)
    scopes = list(restored.search_coverage.values())
    assert len(scopes) == 2 and not scopes[0]["complete"] and scopes[1]["complete"]
    assert scopes[1]["byte_offset"] == 500


def test_invalid_pointer_and_nonfinite_assertion_constants_fail_schema_validation():
    for fields in ({"pointer": "/bad~2pointer", "expected": 1}, {"pointer": "/v", "expected": float("inf")}):
        with pytest.raises(ValidationError):
            EvidenceAssertion(evidence_id="e", **fields)


@pytest.mark.parametrize("field,value", [("threshold", True), ("threshold", float("nan")),
                                         ("max_spread", float("inf"))])
def test_invalid_numeric_policy_is_rejected(field, value):
    with pytest.raises(ValidationError):
        ObjectiveSpec(**specification(**{field: value}))


def test_native_json_schema_is_enabled_after_collection_and_assertions_publish(repository, settings):
    import asyncio
    from mcp import types
    from native_plan_helpers import initial_plan_call
    from test_native_reliability import setup, response, call, run

    async def scenario():
        view, controller, provider = setup(repository, settings)
        provider.call_native.return_value = types.CallToolResult(content=[
            types.TextContent(type="text", text='{"queue_depth":7}')])
        count = 0
        async def model(messages, tools):
            nonlocal count
            count += 1
            schema = next(t["function"]["parameters"] for t in tools
                          if t["function"]["name"] == "agent_submit_report")
            if count == 1:
                assert "assertions" not in schema["$defs"]["NativeFinding"]["properties"]
                return response(initial_plan_call(), call())
            assert "assertions" in schema["$defs"]["NativeFinding"]["properties"]
            eid = repository.list_current_evidence(view.investigation_id)[0].evidence_id
            return response(content=json.dumps({"summary": "Recorded queue depth.", "findings": [{
                "finding_id": "q", "text": "Queue observation.",
                "citations": [{"evidence_id": eid, "quote": '"queue_depth":7'}],
                "assertions": [{"evidence_id": eid, "pointer": "/queue_depth", "expected": 7}]}]}))
        controller._model_call = model
        report = await run(controller, view)
        assert report.status == "completed" and count == 2
        assert next(c for c in report.claims if c.assertions).verification_details.support == "deterministic"
        provider.call_native.assert_awaited_once()
    asyncio.run(scenario())


def test_report_cannot_declare_goal_met_without_a_current_assessment(repository, settings):
    from game_test_agent.reports import ReportBuilder
    view = investigation(repository)
    report = ReportBuilder(repository).build(view.investigation_id, "unverified")
    report.resolution = "goal_met"
    report.coverage.completed_objectives = ["o"]
    with pytest.raises(ValueError, match="matching current objective assessment"):
        ReportService(repository).publish(report)


def test_large_integer_and_nonterminating_means_do_not_round_into_false_equality(repository, settings):
    view = investigation(repository)
    base = 10 ** 35
    store, _ = evidence(repository, settings, view, "e", {"large": [base, base + 1], "thirds": [1, 2, 2]})
    verifier = AssertionVerifier(repository, store, view)
    with pytest.raises(ValueError):
        verifier.verify(EvidenceAssertion(evidence_id="e", pointer="/large", operation="mean", expected=base))
    rendered = verifier.verify(EvidenceAssertion(evidence_id="e", pointer="/thirds", operation="mean", operator="gt", expected=1.6))
    assert "5/3" in rendered


@pytest.mark.parametrize("source,expected", [("real", "insufficient"), ("simulated", "passed")])
def test_unresolved_units_block_acceptance_but_excluded_simulation_does_not(repository, settings, source, expected):
    view = investigation(repository, constraints={"objective_spec": specification()})
    evidence(repository, settings, view, "good", {}, run="good")
    evidence(repository, settings, view, "other", {}, run="other", source=source)
    metric_fact(repository, view, "good-fact", ["good"], 4)
    metric_fact(repository, view, "other-fact", ["other"], 50, unit="different-unit")
    assert ObjectiveEvaluator(repository).evaluate(view.investigation_id).verdict == expected
