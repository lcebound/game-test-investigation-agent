"""Decision-driven offline environments with private labels and frozen paired runs.

All operation results and costs are fixture simulations. Policy callbacks receive
only BranchView, never the environment's transition table or scoring labels.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
from importlib.metadata import version
from pathlib import Path
from typing import Any, Callable, Literal, Protocol
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from pydantic import Field, TypeAdapter, model_validator

from game_test_agent.contracts.models import ContextBundle, NextStep, SelectionRecord, StrictModel
from game_test_agent.storage import payload_hash

ENGINE_VERSION = "branch-environment-v1"
VARIANTS = ("without_selection", "with_selection")


class BranchCost(StrictModel):
    simulated_runs: int = Field(default=0, ge=0)
    device_minutes: float = Field(default=0, ge=0, allow_inf_nan=False)
    tool_reads: int = Field(default=0, ge=0)

    def plus(self, other: BranchCost) -> BranchCost:
        return BranchCost(**{
            key: getattr(self, key) + getattr(other, key) for key in type(self).model_fields
        })


class BranchOperation(StrictModel):
    decision: NextStep
    parameters: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def supported_shape(self) -> BranchOperation:
        if self.decision.kind in {"report", "request_input"}:
            raise ValueError("terminal decisions are not environment operations")
        if self.parameters and self.decision.kind != "experiment":
            raise ValueError("only experiments accept action parameters")
        if self.decision.plan_delta is not None or self.decision.hypothesis_delta is not None:
            raise ValueError("offline operations do not apply plan or hypothesis mutations")
        step = self.decision
        for name, kind in (("read_request", "retrieve_evidence"),
                           ("analysis_request", "analyze"), ("search_query", "search_evidence")):
            value = getattr(step, name)
            if (step.kind == kind) != (value is not None):
                raise ValueError(f"{name} must match operation kind")
        if step.missing_fields:
            raise ValueError("missing_fields belongs to an input request")
        return self

    def key(self) -> str:
        # Match execution inputs exactly, while allowing different rationale and citations.
        data = self.decision.model_dump(mode="json", exclude={
            "expected_observation", "reason_summary", "hypothesis_ids", "evidence_ids",
            "query_purpose",
        })
        if data["read_request"] is not None:
            data["read_request"].pop("purpose")
        return payload_hash({"decision": data, "parameters": self.parameters})


class ModeledOperation(StrictModel):
    operation: BranchOperation
    cost: BranchCost

    @model_validator(mode="after")
    def cost_matches_operation(self) -> ModeledOperation:
        experiment = self.operation.decision.kind == "experiment"
        if self.cost.simulated_runs != int(experiment):
            raise ValueError("each experiment costs one simulated run; other operations cost zero")
        if not experiment and self.cost.device_minutes:
            raise ValueError("only experiments consume simulated device minutes")
        if not experiment and self.cost.tool_reads < 1:
            raise ValueError("read, search, analysis and query operations consume tool budget")
        return self


class BranchObservation(StrictModel):
    observation_id: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    findings: dict[str, Any] = Field(default_factory=dict)


class BranchTransition(StrictModel):
    operation_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["ok", "unsupported", "unavailable"] = "ok"
    next_node: str | None = None

    @model_validator(mode="after")
    def explicit_result(self) -> BranchTransition:
        if (self.status == "ok") != (self.next_node is not None):
            raise ValueError("only successful modeled operations advance the environment")
        return self


class BranchNode(StrictModel):
    node_id: str = Field(min_length=1)
    observation: BranchObservation
    transitions: list[BranchTransition]
    # Private adjudication. These fields never appear in BranchView.
    supported_reports: dict[str, list[str]] = Field(default_factory=dict)
    acceptable_missing_fields: list[str] = Field(default_factory=list)
    unnecessary_experiment: bool = False


class BranchScenario(StrictModel):
    case_id: str = Field(min_length=1)
    category: Literal[
        "functional", "crash", "performance", "memory", "infrastructure", "missing_build"
    ]
    split: Literal["train", "validation", "test"]
    split_group: str = Field(min_length=1)
    initial_context: ContextBundle
    operations: list[ModeledOperation]
    initial_node: str
    nodes: list[BranchNode] = Field(min_length=1)

    @model_validator(mode="after")
    def closed_environment(self) -> BranchScenario:
        keys = [item.operation.key() for item in self.operations]
        node_ids = [node.node_id for node in self.nodes]
        if len(keys) != len(set(keys)) or len(node_ids) != len(set(node_ids)):
            raise ValueError("operation inputs and node IDs must be unique")
        if self.initial_node not in node_ids:
            raise ValueError("initial node is missing")
        request = self.initial_context.investigation.request
        if any(item.operation.decision.objective_id != request.objective.objective_id
               for item in self.operations):
            raise ValueError("operation objective is outside the scenario")
        if self.initial_context.historical_cases:
            raise ValueError("selection ablation fixtures must exclude historical memory")
        if request.constraints.get("enable_model_candidate_design") is True:
            raise ValueError("candidate design is a separate ablation")
        if any(self.initial_context.budget.get(key, 0) != 0 for key in (
            "real_runs", "tool_reads", "reserved_device_minutes",
        )):
            raise ValueError("branch fixtures must start with fresh execution budgets")
        for node in self.nodes:
            edge_keys = [edge.operation_key for edge in node.transitions]
            if len(edge_keys) != len(keys) or set(edge_keys) != set(keys):
                raise ValueError("every node must explicitly cover every modeled operation")
            if any(edge.next_node is not None and edge.next_node not in node_ids
                   for edge in node.transitions):
                raise ValueError("transition references a missing node")
        return self


class BranchResponse(StrictModel):
    status: Literal["ok", "unsupported", "unavailable", "budget_exhausted", "invalid"]
    observation: BranchObservation | None = None


class BranchDecision(StrictModel):
    decision: NextStep
    parameters: dict[str, Any] = Field(default_factory=dict)
    # Reports must state a label, including an explicit 'unresolved' when appropriate.
    conclusion: str | None = None
    selection: SelectionRecord | None = None
    # Usage comes from the policy adapter's provider receipt, not model-generated prose.
    model_calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class BranchTurn(StrictModel):
    choice: BranchDecision
    response: BranchResponse | None = None


class BranchView(StrictModel):
    case_id: str
    context: ContextBundle
    operations: list[ModeledOperation]
    observations: list[BranchObservation]
    evidence_ids: list[str]
    history: list[BranchTurn]
    cost: BranchCost
    remaining_turns: int


class BranchPolicy(Protocol):
    def decide(self, view: BranchView) -> BranchDecision: ...


class BranchTrace(StrictModel):
    case_id: str
    split: str
    variant: str
    initial_view_sha256: str
    turns: list[BranchTurn]
    stop_reason: Literal["report", "request_input", "turn_budget", "policy_error"]
    cost: BranchCost
    quality_pass: bool = False
    false_claim: bool = False
    unnecessary_runs: int = 0
    violations: list[str] = Field(default_factory=list)


class BranchRunConfig(StrictModel):
    policy_id: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    seed: int = 0
    parameters: dict[str, Any] = Field(default_factory=dict)


class BranchManifest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    engine_version: str = ENGINE_VERSION
    scenario_hashes: dict[str, str]
    source_hashes: dict[str, str]
    runtime: dict[str, str]
    config: BranchRunConfig


class PairedBranchResult(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    evidence_tier: Literal["offline_branch_simulation"] = "offline_branch_simulation"
    manifest: BranchManifest
    manifest_sha256: str
    traces: list[BranchTrace]
    metrics_by_split: dict[str, dict[str, dict[str, float]]]
    # Positive cost deltas mean the enabled variant cost more.
    deltas_by_split: dict[str, dict[str, float]]


def load_branch_scenarios(path: Path) -> list[BranchScenario]:
    scenarios = TypeAdapter(list[BranchScenario]).validate_json(path.read_text(encoding="utf-8"))
    _validate_suite_groups(scenarios)
    return scenarios


def _validate_suite_groups(scenarios: list[BranchScenario]) -> None:
    if not scenarios:
        raise ValueError("branch suite must contain at least one scenario")
    ids: set[str] = set()
    splits: dict[str, str] = {}
    for scenario in scenarios:
        if scenario.case_id in ids:
            raise ValueError("branch case IDs must be unique")
        ids.add(scenario.case_id)
        if splits.setdefault(scenario.split_group, scenario.split) != scenario.split:
            raise ValueError("branch split_group leaks across splits")


def freeze_branch_suite(
    scenarios: list[BranchScenario], config: BranchRunConfig,
) -> BranchManifest:
    _validate_suite_groups(scenarios)
    source_root = Path(__file__).resolve().parents[1]
    return BranchManifest(
        scenario_hashes={item.case_id: payload_hash(item.model_dump(mode="json"))
                         for item in scenarios},
        source_hashes={path.relative_to(source_root).as_posix():
                      hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in sorted(source_root.rglob("*.py"))},
        runtime={"python": platform.python_version(), "pydantic": version("pydantic"),
                 "jsonschema": version("jsonschema")},
        config=config,
    )


def write_branch_snapshot(
    path: Path, scenarios: list[BranchScenario], manifest: BranchManifest,
) -> None:
    """Archive the hashed Python sources and fixtures for later isolated reproduction."""
    if freeze_branch_suite(scenarios, manifest.config) != manifest:
        raise ValueError("snapshot inputs changed since freeze")
    source_root = Path(__file__).resolve().parents[1]
    entries = {
        f"src/game_test_agent/{name}": (source_root / name).read_bytes()
        for name in manifest.source_hashes
    }
    entries["evals/scenarios.json"] = json.dumps(
        [item.model_dump(mode="json", by_alias=True, round_trip=True) for item in scenarios],
        ensure_ascii=False, indent=2,
    ).encode("utf-8")
    entries["evals/manifest.json"] = manifest.model_dump_json(indent=2).encode("utf-8")
    project_config = source_root.parent.parent / "pyproject.toml"
    if project_config.is_file():
        entries["pyproject.toml"] = project_config.read_bytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(path, "x", compression=ZIP_DEFLATED) as archive:
        for name, content in sorted(entries.items()):
            info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, content)


def _initial_view(scenario: BranchScenario, selection_enabled: bool) -> BranchView:
    context = scenario.initial_context.model_copy(deep=True)
    context.investigation.request.constraints["enable_experiment_selection"] = selection_enabled
    observation = next(node.observation for node in scenario.nodes
                       if node.node_id == scenario.initial_node)
    return BranchView(
        case_id=scenario.case_id, context=context,
        operations=[item.model_copy(deep=True) for item in scenario.operations],
        observations=[observation.model_copy(deep=True)],
        evidence_ids=sorted({item.evidence_id for item in context.evidence}
                            | set(observation.evidence_ids)),
        history=[], cost=BranchCost(),
        remaining_turns=context.investigation.request.budget.max_rounds,
    )


def _scope_valid(choice: BranchDecision, view: BranchView) -> bool:
    decision = choice.decision
    refs = set(decision.evidence_ids)
    if decision.read_request:
        refs.add(decision.read_request.evidence_id)
    if decision.analysis_request:
        refs.update(decision.analysis_request.input_evidence_ids)
    terminal_shape_valid = decision.kind not in {"report", "request_input"} or (
        decision.read_request is None and decision.analysis_request is None
        and decision.search_query is None and decision.target_action_id is None
        and decision.target_run_ref is None and decision.capability_id is None
        and decision.provider_id is None
        and (decision.kind != "report" or not decision.missing_fields)
    )
    return (
        decision.objective_id == view.context.investigation.request.objective.objective_id
        and refs.issubset(view.evidence_ids)
        and decision.plan_delta is None and decision.hypothesis_delta is None
        and (not choice.parameters or decision.kind == "experiment")
        and (choice.conclusion is None or decision.kind == "report")
        and terminal_shape_valid
    )


def run_branch_scenario(
    scenario: BranchScenario, policy: BranchPolicy, *, selection_enabled: bool,
) -> BranchTrace:
    view = _initial_view(scenario, selection_enabled)
    hash_view = view.model_copy(deep=True)
    hash_view.context.investigation.request.constraints.pop("enable_experiment_selection")
    trace = BranchTrace(
        case_id=scenario.case_id, split=scenario.split,
        variant=VARIANTS[int(selection_enabled)],
        initial_view_sha256=payload_hash(hash_view.model_dump(mode="json")),
        turns=[], stop_reason="turn_budget", cost=BranchCost(),
    )
    nodes = {node.node_id: node for node in scenario.nodes}
    modeled = {item.operation.key(): item for item in scenario.operations}
    node = nodes[scenario.initial_node]
    budget = view.context.investigation.request.budget
    for _ in range(budget.max_rounds):
        try:
            # Deep copies prevent callback mutations from changing labels, budget or future views.
            choice = BranchDecision.model_validate(policy.decide(view.model_copy(deep=True)))
            choice = choice.model_copy(deep=True)
        except Exception as error:
            trace.stop_reason = "policy_error"
            trace.violations.append(f"policy_error:{type(error).__name__}")
            break
        turn = BranchTurn(choice=choice)
        trace.turns.append(turn)
        valid = _scope_valid(choice, view)
        if choice.decision.kind in {"report", "request_input"}:
            trace.stop_reason = choice.decision.kind
            if choice.decision.kind == "report":
                required = node.supported_reports.get(choice.conclusion)
                supported = required is not None and set(required).issubset(
                    choice.decision.evidence_ids
                )
                trace.quality_pass = valid and supported
                trace.false_claim = choice.conclusion not in {None, "unresolved"} and (
                    not valid or not supported
                )
            else:
                fields = choice.decision.missing_fields
                trace.quality_pass = (
                    valid and bool(fields) and bool(node.acceptable_missing_fields)
                    and set(fields) == set(node.acceptable_missing_fields)
                )
            if not trace.quality_pass:
                trace.violations.append("unsupported_terminal_decision")
            break
        if not valid:
            turn.response = BranchResponse(status="invalid")
            trace.violations.append("decision_outside_visible_scope")
        else:
            try:
                operation_key = BranchOperation(
                    decision=choice.decision, parameters=choice.parameters
                ).key()
            except ValueError:
                operation_key = ""
            item = modeled.get(operation_key)
            edge = next((edge for edge in node.transitions
                         if edge.operation_key == operation_key), None)
            if item is None or edge is None:
                turn.response = BranchResponse(status="unsupported")
            elif edge.status != "ok":
                turn.response = BranchResponse(status=edge.status)
            else:
                proposed_cost = view.cost.plus(item.cost)
                if (
                    proposed_cost.simulated_runs > budget.max_real_runs
                    or proposed_cost.device_minutes > budget.max_device_minutes
                    or proposed_cost.tool_reads > budget.max_tool_reads
                ):
                    turn.response = BranchResponse(status="budget_exhausted")
                else:
                    if node.unnecessary_experiment:
                        trace.unnecessary_runs += item.cost.simulated_runs
                    view.cost = proposed_cost
                    node = nodes[edge.next_node]
                    observation = node.observation.model_copy(deep=True)
                    turn.response = BranchResponse(status="ok", observation=observation)
                    view.observations.append(observation)
                    view.evidence_ids = sorted(set(view.evidence_ids) | set(observation.evidence_ids))
        view.history.append(turn.model_copy(deep=True))
        view.remaining_turns -= 1
        view.context.budget.update({
            "real_runs": view.cost.simulated_runs,
            "reserved_device_minutes": view.cost.device_minutes,
            "tool_reads": view.cost.tool_reads,
        })
    trace.cost = view.cost
    if trace.stop_reason == "turn_budget":
        trace.violations.append("turn_budget_exhausted_without_terminal_decision")
    return trace


def run_paired_branches(
    scenarios: list[BranchScenario], manifest: BranchManifest,
    policy_factory: Callable[[BranchRunConfig], BranchPolicy],
) -> PairedBranchResult:
    current = freeze_branch_suite(scenarios, manifest.config)
    if current != manifest:
        raise ValueError("frozen scenarios, source or runtime changed; create a new manifest")
    traces = []
    for scenario in scenarios:
        for enabled in (False, True):
            # Each trajectory gets a fresh policy with identical configuration.
            traces.append(run_branch_scenario(
                scenario, policy_factory(manifest.config.model_copy(deep=True)),
                selection_enabled=enabled,
            ))
    metrics: dict[str, dict[str, dict[str, float]]] = {}
    deltas: dict[str, dict[str, float]] = {}
    for split in sorted({trace.split for trace in traces}):
        metrics[split] = {}
        for variant in VARIANTS:
            group = [trace for trace in traces if trace.split == split and trace.variant == variant]
            count = len(group)
            metrics[split][variant] = {
                "case_count": count,
                "quality_pass_rate": sum(trace.quality_pass for trace in group) / count,
                "false_claim_rate": sum(trace.false_claim for trace in group) / count,
                "mean_simulated_runs": sum(trace.cost.simulated_runs for trace in group) / count,
                "mean_device_minutes": sum(trace.cost.device_minutes for trace in group) / count,
                "mean_tool_reads": sum(trace.cost.tool_reads for trace in group) / count,
                "mean_unnecessary_runs": sum(trace.unnecessary_runs for trace in group) / count,
                "mean_turns": sum(len(trace.turns) for trace in group) / count,
                "mean_unsupported_actions": sum(
                    turn.response is not None and turn.response.status == "unsupported"
                    for trace in group for turn in trace.turns
                ) / count,
                "mean_model_calls": sum(turn.choice.model_calls
                                        for trace in group for turn in trace.turns) / count,
                "mean_tokens": sum(turn.choice.input_tokens + turn.choice.output_tokens
                                   for trace in group for turn in trace.turns) / count,
            }
        without, enabled = (metrics[split][variant] for variant in VARIANTS)
        deltas[split] = {key: enabled[key] - without[key] for key in without if key != "case_count"}
    return PairedBranchResult(
        manifest=manifest, manifest_sha256=payload_hash(manifest.model_dump(mode="json")),
        traces=traces, metrics_by_split=metrics, deltas_by_split=deltas,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["freeze", "run"])
    parser.add_argument("fixtures", type=Path)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--snapshot", type=Path, help="freeze: archive Python source and fixtures")
    args = parser.parse_args(argv)
    scenarios = load_branch_scenarios(args.fixtures)
    if args.command == "freeze":
        if args.manifest.exists():
            parser.error("manifest exists; choose a new path to preserve the frozen baseline")
        if args.snapshot is not None and (
            args.snapshot.exists() or args.snapshot.resolve() == args.manifest.resolve()
        ):
            parser.error("snapshot must use a new path distinct from the manifest")
        manifest = freeze_branch_suite(scenarios, BranchRunConfig(
            policy_id="scripted-selector-demo-v1", model_name="scripted-fixture",
        ))
        if args.snapshot is not None:
            write_branch_snapshot(args.snapshot, scenarios, manifest)
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
        print(f"Frozen {len(scenarios)} scenarios: {args.manifest}")
        return 0
    if args.output is None:
        parser.error("run requires --output")
    if args.output.exists():
        parser.error("output exists; choose a new path to preserve recorded results")
    from game_test_agent.evals.branch_demo import create_demo_policy

    manifest = BranchManifest.model_validate_json(args.manifest.read_text(encoding="utf-8"))
    result = run_paired_branches(scenarios, manifest, create_demo_policy)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    print(json.dumps(result.metrics_by_split, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    # Use canonical module classes when adapters import this module during -m execution.
    from game_test_agent.evals.branching import main as module_main

    raise SystemExit(module_main())
