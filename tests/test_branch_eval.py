from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
from zipfile import ZipFile

import pytest

from game_test_agent.contracts.models import NextStep
from game_test_agent.evals.branch_demo import create_demo_policy
from game_test_agent.evals.branching import (
    BranchDecision, BranchManifest, BranchRunConfig, BranchScenario, BranchTransition,
    freeze_branch_suite, load_branch_scenarios, main, run_branch_scenario, run_paired_branches,
)


FIXTURES = Path(__file__).resolve().parents[1] / "evals" / "branch-demo.json"
CONFIG = BranchRunConfig(policy_id="scripted-selector-demo-v1", model_name="scripted-fixture")


@pytest.fixture
def cases():
    return load_branch_scenarios(FIXTURES)


class Policy:
    def __init__(self, callback):
        self.callback = callback

    def decide(self, view):
        return self.callback(view)


def report(view, conclusion, evidence_ids=()):
    return BranchDecision(decision=NextStep(
        kind="report", objective_id=view.context.investigation.request.objective.objective_id,
        evidence_ids=list(evidence_ids), expected_observation="Stop", reason_summary="Fixture report",
    ), conclusion=conclusion)


def test_paired_driver_uses_production_selector_with_frozen_identical_start(cases):
    manifest = freeze_branch_suite(cases, CONFIG)
    result = run_paired_branches(cases, manifest, create_demo_policy)
    assert len(result.traces) == 8
    assert all(trace.quality_pass for trace in result.traces)
    for without, with_selection in zip(result.traces[::2], result.traces[1::2]):
        assert without.initial_view_sha256 == with_selection.initial_view_sha256
    baseline, selected = result.traces[:2]
    assert baseline.turns[0].choice.decision.kind == "experiment"
    assert selected.turns[0].choice.decision.kind == "analyze"
    assert selected.turns[0].choice.selection.selected_candidate_id == "analyze-source-1"
    assert baseline.cost.simulated_runs == baseline.unnecessary_runs == 1
    assert selected.cost.simulated_runs == selected.unnecessary_runs == 0
    assert selected.cost.tool_reads == 1
    functional = result.traces[3]
    assert functional.turns[0].choice.decision.kind == "retrieve_evidence"
    assert set(result.metrics_by_split) == {"train"}
    assert result.metrics_by_split["train"]["with_selection"]["mean_model_calls"] == 0
    assert result.evidence_tier == "offline_branch_simulation"
    assert result.deltas_by_split["train"]["quality_pass_rate"] == 0
    assert result.deltas_by_split["train"]["mean_simulated_runs"] == -0.5


def test_private_labels_are_absent_and_callback_mutations_do_not_affect_environment(cases):
    scenario = cases[0]
    scenario.nodes[0].supported_reports = {"PRIVATE_SENTINEL": []}
    seen = []

    def callback(view):
        assert "PRIVATE_SENTINEL" not in view.model_dump_json()
        assert "supported_reports" not in view.model_dump_json()
        assert "transitions" not in view.model_dump_json()
        seen.append(view.context.investigation.request.budget.max_rounds)
        view.cost.simulated_runs = 1000
        view.context.investigation.request.budget.max_rounds = 0
        view.operations.clear()
        return report(view, "unresolved")

    trace = run_branch_scenario(scenario, Policy(callback), selection_enabled=False)
    assert seen == [4]
    assert trace.cost.simulated_runs == 0
    assert not trace.quality_pass
    assert len(scenario.operations) == 2
    assert scenario.initial_context.investigation.request.budget.max_rounds == 4


def test_unmodeled_parameters_are_unsupported_and_never_reveal_favorable_result(cases):
    def callback(view):
        operation = view.operations[0].operation
        return BranchDecision(decision=operation.decision, parameters={"case": "invented"})

    trace = run_branch_scenario(cases[0], Policy(callback), selection_enabled=False)
    assert all(turn.response.status == "unsupported" for turn in trace.turns)
    assert all(turn.response.observation is None for turn in trace.turns)
    assert trace.cost.simulated_runs == 0
    assert trace.stop_reason == "turn_budget"
    assert not trace.quality_pass


def test_different_modeled_parameters_follow_different_branches(cases):
    scenario = cases[0]
    alternative = scenario.operations[0].model_copy(deep=True)
    alternative.operation.parameters = {"case": "alternative"}
    scenario.operations.append(alternative)
    alternate_node = scenario.nodes[1].model_copy(deep=True)
    alternate_node.node_id = "alternate"
    alternate_node.observation.observation_id = "observation-2"
    alternate_node.observation.findings = {"frame_ms": 25}
    alternate_node.supported_reports = {"cpu": ["source-1"]}
    scenario.nodes.append(alternate_node)
    for node in scenario.nodes:
        node.transitions.append(BranchTransition(
            operation_key=alternative.operation.key(), next_node="alternate",
        ))
    scenario = BranchScenario.model_validate(
        scenario.model_dump(mode="json", round_trip=True, by_alias=True)
    )
    policy = create_demo_policy(CONFIG)

    def callback(view):
        if not view.history:
            return BranchDecision(decision=alternative.operation.decision,
                                  parameters=alternative.operation.parameters)
        return policy.decide(view)

    trace = run_branch_scenario(scenario, Policy(callback), selection_enabled=False)
    assert trace.turns[0].response.observation.findings["frame_ms"] == 25
    assert trace.turns[1].choice.conclusion == "cpu"
    assert trace.quality_pass


@pytest.mark.parametrize("resource", ["max_real_runs", "max_device_minutes", "max_tool_reads"])
def test_budget_denial_does_not_advance_environment_or_charge_execution(cases, resource):
    scenario = cases[0]
    setattr(scenario.initial_context.investigation.request.budget, resource, 0)
    index = 1 if resource == "max_tool_reads" else 0

    def callback(view):
        operation = view.operations[index].operation
        return BranchDecision(decision=operation.decision, parameters=operation.parameters)

    trace = run_branch_scenario(scenario, Policy(callback), selection_enabled=False)
    assert all(turn.response.status == "budget_exhausted" for turn in trace.turns)
    assert trace.cost.simulated_runs == trace.cost.tool_reads == trace.cost.device_minutes == 0


@pytest.mark.parametrize("conclusion,evidence_ids", [("cpu", ["source-1"]), ("gpu", [])])
def test_correct_answer_requires_supported_current_result_and_citations(cases, conclusion, evidence_ids):
    scenario = cases[0]
    trace = run_branch_scenario(scenario, Policy(lambda view: report(view, conclusion, evidence_ids)),
                                selection_enabled=False)
    assert trace.false_claim and not trace.quality_pass


def test_out_of_scope_references_do_not_execute(cases):
    def callback(view):
        operation = view.operations[0].operation
        decision = operation.decision.model_copy(update={"evidence_ids": ["invisible"]})
        return BranchDecision(decision=decision, parameters=operation.parameters)

    trace = run_branch_scenario(cases[0], Policy(callback), selection_enabled=False)
    assert all(turn.response.status == "invalid" for turn in trace.turns)
    assert trace.cost.simulated_runs == 0


def test_input_request_is_scored_against_private_missing_fields(cases):
    scenario = cases[-1]
    scenario.nodes[0].acceptable_missing_fields = ["build_ref"]

    def callback(view):
        return BranchDecision(decision=NextStep(
            kind="request_input", objective_id="diagnose", missing_fields=["build_ref"],
            expected_observation="Build", reason_summary="Build is missing",
        ))

    trace = run_branch_scenario(scenario, Policy(callback), selection_enabled=True)
    assert trace.quality_pass and trace.stop_reason == "request_input"


def test_environment_requires_full_coverage_and_valid_targets(cases):
    data = cases[0].model_dump(mode="json", round_trip=True, by_alias=True)
    data["nodes"][0]["transitions"].pop()
    with pytest.raises(ValueError, match="cover every modeled operation"):
        BranchScenario.model_validate(data)
    data = cases[0].model_dump(mode="json", round_trip=True, by_alias=True)
    data["nodes"][0]["transitions"][0]["next_node"] = "missing"
    with pytest.raises(ValueError, match="missing node"):
        BranchScenario.model_validate(data)


def test_manifest_rejects_task_source_and_runtime_drift(cases):
    manifest = freeze_branch_suite(cases, CONFIG)
    for field in ("scenario_hashes", "source_hashes", "runtime"):
        modified = manifest.model_copy(deep=True)
        mapping = getattr(modified, field)
        mapping[next(iter(mapping))] = "tampered"
        with pytest.raises(ValueError, match="frozen scenarios, source or runtime changed"):
            run_paired_branches(cases, modified, create_demo_policy)
    cases[0].nodes[0].observation.findings["new"] = True
    with pytest.raises(ValueError, match="frozen scenarios, source or runtime changed"):
        run_paired_branches(cases, manifest, create_demo_policy)


def test_fresh_budgets_and_unique_suite_ids_are_required(cases):
    with pytest.raises(ValueError, match="unique"):
        freeze_branch_suite([cases[0], cases[0]], CONFIG)
    data = cases[0].model_dump(mode="json", by_alias=True)
    data["initial_context"]["budget"]["real_runs"] = 1
    with pytest.raises(ValueError, match="fresh execution budgets"):
        BranchScenario.model_validate(data)


def test_terminal_cannot_carry_an_execution_request(cases):
    def callback(view):
        choice = report(view, "asset", ["source-1"])
        choice.decision.read_request = view.operations[1].operation.decision.read_request
        return choice

    trace = run_branch_scenario(cases[2], Policy(callback), selection_enabled=False)
    assert not trace.quality_pass and trace.false_claim


def test_split_leak_and_duplicate_cases_are_rejected(tmp_path, cases):
    path = tmp_path / "cases.json"
    data = [case.model_dump(mode="json", round_trip=True, by_alias=True) for case in cases]
    data[1]["split_group"] = data[0]["split_group"]
    data[1]["split"] = "test"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="leaks across splits"):
        load_branch_scenarios(path)
    data[1]["case_id"] = data[0]["case_id"]
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="unique"):
        load_branch_scenarios(path)


def test_policy_error_is_a_failed_trace(cases):
    def callback(view):
        raise RuntimeError("private exception payload")

    trace = run_branch_scenario(cases[0], Policy(callback), selection_enabled=False)
    assert trace.stop_reason == "policy_error" and not trace.quality_pass
    assert trace.violations == ["policy_error:RuntimeError"]


def test_cli_freezes_runs_and_preserves_existing_manifest(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    output = tmp_path / "result.json"
    snapshot = tmp_path / "snapshot.zip"
    assert main(["freeze", str(FIXTURES), "--manifest", str(manifest_path),
                 "--snapshot", str(snapshot)]) == 0
    manifest = BranchManifest.model_validate_json(manifest_path.read_text())
    assert manifest.config == CONFIG
    with ZipFile(snapshot) as archive:
        for name, sha256 in manifest.source_hashes.items():
            assert hashlib.sha256(archive.read(f"src/game_test_agent/{name}")).hexdigest() == sha256
        assert len(json.loads(archive.read("evals/scenarios.json"))) == 4
    with pytest.raises(SystemExit):
        main(["freeze", str(FIXTURES), "--manifest", str(manifest_path)])
    assert main(["run", str(FIXTURES), "--manifest", str(manifest_path),
                 "--output", str(output)]) == 0
    assert len(json.loads(output.read_text())["traces"]) == 8


def test_module_cli_uses_same_contract_classes_as_policy_adapter(tmp_path):
    project = FIXTURES.parent.parent
    env = dict(os.environ, PYTHONPATH=str(project / "src"))
    manifest = tmp_path / "manifest.json"
    result = tmp_path / "result.json"
    for arguments in (
        ["freeze", str(FIXTURES), "--manifest", str(manifest)],
        ["run", str(FIXTURES), "--manifest", str(manifest), "--output", str(result)],
    ):
        process = subprocess.run(
            [sys.executable, "-m", "game_test_agent.evals.branching", *arguments],
            cwd=project, env=env, capture_output=True, text=True,
        )
        assert process.returncode == 0, process.stderr
    assert all(trace["quality_pass"] for trace in json.loads(result.read_text())["traces"])
