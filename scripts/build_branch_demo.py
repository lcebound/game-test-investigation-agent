"""Build reproducible training examples; these are not held-out benchmark tasks."""

from datetime import datetime, timezone
import hashlib
from pathlib import Path

from game_test_agent.contracts.models import (
    AnalysisRequest, BuildRef, CapabilityDescriptor, ContextBundle, ContextWindowUsage,
    EvidenceLocator, EvidenceRecord, Hypothesis, InvestigationRequest, InvestigationView,
    InvestigationWorkingState, NextStep, ObservablePrediction, ReadRequest,
    TestObjective as Objective,
)
from game_test_agent.evals.branching import (
    BranchCost, BranchNode, BranchObservation, BranchOperation, BranchScenario,
    BranchTransition, ModeledOperation,
)


def build_examples() -> list[BranchScenario]:
    examples = []
    timestamp = datetime(2026, 9, 30, tzinfo=timezone.utc)
    for case_id, category, mime, sufficient in (
        ("demo-performance", "performance", "text/csv", False),
        ("demo-functional", "functional", "text/plain", False),
        ("demo-sufficient", "functional", "text/plain", True),
        ("demo-missing-build", "missing_build", None, False),
    ):
        request = InvestigationRequest(
            tenant_id="offline", project_id="demo",
            objective=Objective(objective_id="diagnose", description="Classify fixture observations"),
            build_ref=BuildRef(id="fixture-build") if mime else None,
            constraints={
                "action_parameters": {"case": "demo"},
                "environment_fingerprint": "fixture-env",
                "action_target_selector": {"environment_fingerprint": "fixture-env"},
            },
        )
        capability = CapabilityDescriptor.model_validate({
            "capability_id": "test.run", "capability_version": "1",
            "provider_id": "offline", "provider_revision": "1", "tool_name": "submit",
            "project_ids": ["demo"],
            "input_schema": {"type": "object", "properties": {"case": {"type": "string"}},
                             "required": ["case"], "additionalProperties": False},
            "risk_level": "bounded_execution", "required_scopes": [],
            "execution_owner": "offline", "resource_domain": "device",
            "estimated_cost": {"device_minutes": 2},
            "controllable_variables": ["build_ref", "environment_fingerprint"],
        })
        source_id = "source-1"
        evidence = []
        if mime:
            content = b"frame_ms\n15\n15\n15\n" if mime == "text/csv" else b"ERROR ASSET_MISSING\n"
            evidence = [EvidenceRecord(
                evidence_id=source_id, tenant_id="offline", project_id="demo",
                artifact_ref="offline-fixture/source-1", kind="metrics" if mime == "text/csv"
                else "runtime_log", mime_type=mime, sha256=hashlib.sha256(content).hexdigest(),
                producer="offline-fixture", producer_version="1", collected_at=timestamp,
                locator=EvidenceLocator(kind="sample_window", value="all"),
                quality="verified", source_type="user_upload", source_evidence_id=source_id,
            )]
        hypotheses = []
        labels = ("cpu", "gpu") if mime == "text/csv" else ("asset", "network")
        for index, label in enumerate(labels):
            prediction = ObservablePrediction(
                observation_key="columns.frame_ms.median" if mime == "text/csv" else "test_verdict",
                comparator="eq", value=(15 if index else 25) if mime == "text/csv"
                else ("failed" if index == 0 else "passed"),
                unit="ms" if mime == "text/csv" else None, capability_id="test.run",
            )
            predictions = [prediction]
            if mime == "text/csv":
                predictions.append(prediction.model_copy(update={"capability_id": "csv_summary_v1"}))
            hypotheses.append(Hypothesis(
                hypothesis_id=label, investigation_id=case_id, revision=1,
                statement=f"Fixture alternative {label}", scope="fixture-build",
                missing_evidence=["inspect existing measurement"],
                falsifiable_prediction="Use only the declared fixture observation",
                observable_predictions=predictions,
            ))
        context = ContextBundle(
            investigation=InvestigationView(
                investigation_id=case_id, workflow_id=case_id, status="planning", revision=1,
                transition_seq=1, request=request, created_at=timestamp, updated_at=timestamp,
            ),
            working_state=InvestigationWorkingState(
                investigation_id=case_id, state_revision=1, objective=request.objective,
                active_hypotheses=hypotheses,
            ),
            capabilities=[capability], evidence=evidence, facts=[], hypotheses=hypotheses,
            actions=[], budget={"tool_reads": 0, "real_runs": 0, "reserved_device_minutes": 0},
            context_window=ContextWindowUsage(
                window_limit_tokens=24000, reserved_output_tokens=4000,
                mandatory_tokens=0, optional_tokens=0, estimated_input_tokens=0,
            ),
        )
        experiment = ModeledOperation(operation=BranchOperation(decision=NextStep(
            kind="experiment", objective_id="diagnose", capability_id="test.run",
            provider_id="offline", expected_observation="New fixture measurement",
            reason_summary="Scripted baseline repeats the test",
        ), parameters={"case": "demo"}), cost=BranchCost(simulated_runs=1, device_minutes=2))
        operations = [experiment]
        if mime:
            step = NextStep(
                kind="analyze" if mime == "text/csv" else "retrieve_evidence",
                objective_id="diagnose", evidence_ids=[source_id],
                expected_observation="Inspect existing fixture evidence", reason_summary="Read first",
                analysis_request=AnalysisRequest(recipe_id="csv_summary_v1", input_evidence_ids=[source_id])
                if mime == "text/csv" else None,
                read_request=ReadRequest(evidence_id=source_id, purpose="Inspect log")
                if mime == "text/plain" else None,
            )
            operations.append(ModeledOperation(operation=BranchOperation(decision=step),
                                               cost=BranchCost(tool_reads=1)))
        findings = {"frame_ms": 15, "sample_count": 3, "unit": "ms"} if mime == "text/csv" else {
            "error_code": "ASSET_MISSING",
        }
        report_label = "gpu" if mime == "text/csv" else "asset"
        initial_findings = findings if sufficient else {}
        start = BranchNode(
            node_id="start", observation=BranchObservation(
                observation_id="observation-0", evidence_ids=[source_id] if mime else [],
                findings=initial_findings,
            ),
            transitions=[BranchTransition(operation_key=item.operation.key(),
                                           status="ok" if mime else "unavailable",
                                           next_node="observed" if mime else None)
                         for item in operations],
            supported_reports={report_label: [source_id]} if sufficient else
            {"unresolved": []} if not mime else {},
            unnecessary_experiment=bool(mime),
        )
        nodes = [start]
        if mime:
            nodes.append(BranchNode(
                node_id="observed", observation=BranchObservation(
                    observation_id="observation-1", evidence_ids=[source_id], findings=findings,
                ),
                transitions=[BranchTransition(operation_key=item.operation.key(), next_node="observed")
                             for item in operations],
                supported_reports={report_label: [source_id]}, unnecessary_experiment=True,
            ))
        examples.append(BranchScenario(
            case_id=case_id, category=category, split="train", split_group=case_id,
            initial_context=context, operations=operations, initial_node="start", nodes=nodes,
        ))
    return examples


if __name__ == "__main__":
    import json

    target = Path(__file__).resolve().parents[1] / "evals" / "branch-demo.json"
    target.write_text(json.dumps([case.model_dump(mode="json", round_trip=True, by_alias=True)
                                 for case in build_examples()],
                                 ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
