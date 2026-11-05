from __future__ import annotations

import asyncio
import hashlib
import json
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from jsonschema import Draft202012Validator

from game_test_agent.contracts.models import (
    AnalysisRecipe,
    AnalysisRecipeStep,
    EvidenceLocator,
    EvidenceRecord,
)
from game_test_agent.evidence.service import EvidenceService
from game_test_agent.observability.tracing import trace_operation
from game_test_agent.storage import ArtifactStore, Repository, canonical_json


RecipeComponent = Callable[[dict[str, Any], dict[str, Any]], Any]
RecipeVerifier = Callable[[Any], bool]


@dataclass(frozen=True, slots=True)
class AnalysisRecipeExecution:
    execution_id: str
    output: Any
    evidence: EvidenceRecord


class AnalysisRecipeRunner:
    """Execute only registered deterministic components in a validated DAG."""

    def __init__(
        self,
        components: dict[tuple[str, str], RecipeComponent],
        verifiers: dict[str, RecipeVerifier],
    ) -> None:
        self.components = dict(components)
        self.verifiers = dict(verifiers)

    def validate(self, recipe: AnalysisRecipe) -> list[str]:
        errors: list[str] = []
        identities = [item.step_id for item in recipe.steps]
        if len(identities) != len(set(identities)):
            errors.append("recipe step IDs must be unique")
        known = set(identities)
        for step in recipe.steps:
            missing = set(step.dependencies) - known
            if missing:
                errors.append(f"step {step.step_id} has missing dependencies: {sorted(missing)}")
            if (step.component, step.component_version) not in self.components:
                errors.append(f"step {step.step_id} uses an unregistered component/version")
        _order, cyclic = self._order(recipe.steps)
        if cyclic:
            errors.append("recipe dependency graph contains a cycle")
        if recipe.verifier not in self.verifiers:
            errors.append("recipe verifier is not registered")
        try:
            Draft202012Validator.check_schema(recipe.output_schema)
        except Exception as error:
            errors.append(f"recipe output schema is invalid: {error}")
        return errors

    def run(self, recipe: AnalysisRecipe, inputs: dict[str, Any]) -> Any:
        errors = self.validate(recipe)
        if errors:
            raise ValueError("; ".join(errors))
        order, _ = self._order(recipe.steps)
        by_id = {item.step_id: item for item in recipe.steps}
        outputs: dict[str, Any] = {}
        for step_id in order:
            step = by_id[step_id]
            material = {
                "inputs": inputs,
                "dependencies": {key: outputs[key] for key in step.dependencies},
            }
            outputs[step_id] = self.components[(step.component, step.component_version)](
                material, dict(step.parameters)
            )
        result = outputs[order[-1]]
        Draft202012Validator(recipe.output_schema).validate(result)
        if not self.verifiers[recipe.verifier](result):
            raise ValueError("recipe verifier rejected the output")
        return result

    @staticmethod
    def _order(steps: list[AnalysisRecipeStep]) -> tuple[list[str], bool]:
        dependents: dict[str, list[str]] = {item.step_id: [] for item in steps}
        indegree = {item.step_id: len(item.dependencies) for item in steps}
        for item in steps:
            for dependency in item.dependencies:
                if dependency in dependents:
                    dependents[dependency].append(item.step_id)
        ready = deque(sorted(key for key, value in indegree.items() if value == 0))
        order: list[str] = []
        while ready:
            node = ready.popleft()
            order.append(node)
            for child in sorted(dependents[node]):
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
        return order, len(order) != len(steps)


class AnalysisRecipeService:
    """Connect trusted recipe components to immutable artifacts and evidence provenance."""

    def __init__(
        self,
        repository: Repository,
        artifacts: ArtifactStore,
        runner: AnalysisRecipeRunner,
        *,
        max_input_bytes: int = 1024 * 1024,
    ) -> None:
        if max_input_bytes < 1:
            raise ValueError("recipe input limit must be positive")
        self.repository = repository
        self.artifacts = artifacts
        self.runner = runner
        self.max_input_bytes = max_input_bytes
        self.evidence = EvidenceService(repository)

    @trace_operation("analysis.recipe")
    def execute(
        self,
        investigation_id: str,
        recipe: AnalysisRecipe,
        input_evidence_ids: list[str],
        *,
        execution_key: str | None = None,
    ) -> AnalysisRecipeExecution:
        investigation, selected = self._select_inputs(investigation_id, input_evidence_ids)
        output = self._compute(recipe, selected)
        stored = self.artifacts.put_bytes(investigation.request.tenant_id,
            investigation.request.project_id, canonical_json(output).encode("utf-8"))
        return self._persist(investigation_id, recipe, selected, output, stored, execution_key)

    @trace_operation("analysis.recipe")
    async def execute_async(self, investigation_id, recipe, input_evidence_ids, *, execution_key=None):
        # Never send a repository/SQLite connection into a background thread.
        investigation, selected = self._select_inputs(investigation_id, input_evidence_ids)
        output = await asyncio.to_thread(self._compute, recipe, selected)
        stored = await asyncio.to_thread(self.artifacts.put_bytes, investigation.request.tenant_id,
            investigation.request.project_id, canonical_json(output).encode("utf-8"))
        return self._persist(investigation_id, recipe, selected, output, stored, execution_key)

    def _select_inputs(self, investigation_id, input_evidence_ids):
        if not input_evidence_ids or len(input_evidence_ids) != len(set(input_evidence_ids)):
            raise ValueError("recipe inputs must be a non-empty unique evidence list")
        investigation = self.repository.get_investigation(investigation_id)
        current = {
            item.evidence_id: item
            for item in self.repository.list_current_evidence(investigation_id)
        }
        selected: list[EvidenceRecord] = []
        for evidence_id in input_evidence_ids:
            item = current.get(evidence_id)
            if item is None:
                raise LookupError(f"current evidence {evidence_id!r} is unavailable")
            if item.retrieval_status != "ready":
                raise ValueError(f"evidence {evidence_id!r} is not ready")
            if not self.artifacts.validate_scope(item.artifact_ref,
                    investigation.request.tenant_id, investigation.request.project_id):
                raise PermissionError("recipe artifact belongs to another project")
            selected.append(item)
        return investigation, selected

    def _compute(self, recipe, selected):
        decoded: dict[str, Any] = {}
        total_bytes = 0
        for item in selected:
            evidence_id = item.evidence_id
            content = self.artifacts.read_bytes(item.artifact_ref)
            if hashlib.sha256(content).hexdigest() != item.sha256.lower():
                raise IOError(f"evidence {evidence_id!r} failed its artifact hash")
            total_bytes += len(content)
            if total_bytes > self.max_input_bytes:
                raise ValueError("recipe inputs exceed the bounded read limit")
            mime_type = item.mime_type.split(";", 1)[0].strip().lower()
            if mime_type == "application/json":
                decoded[evidence_id] = json.loads(content)
            elif mime_type.startswith("text/"):
                decoded[evidence_id] = content.decode("utf-8")
            else:
                raise ValueError(f"recipe input {evidence_id!r} is not JSON or text")
        return self.runner.run(
            recipe,
            {
                "evidence": decoded,
                "metadata": {item.evidence_id: item.model_dump(mode="json") for item in selected},
            },
        )

    def _persist(self, investigation_id, recipe, selected, output, stored, execution_key):
        investigation, current = self._select_inputs(investigation_id, [e.evidence_id for e in selected])
        if [(e.sha256, e.source_revision) for e in current] != [(e.sha256, e.source_revision) for e in selected]:
            raise ValueError("recipe inputs changed during execution")
        identity = canonical_json(
            {
                "recipe_id": recipe.recipe_id,
                "recipe_version": recipe.version,
                "inputs": [
                    {
                        "evidence_id": item.evidence_id,
                        "source_revision": item.source_revision,
                        "sha256": item.sha256,
                    }
                    for item in selected
                ],
                "output_sha256": stored.sha256,
                **({"execution_key": execution_key} if execution_key else {}),
            }
        )
        digest = hashlib.sha256(identity.encode()).hexdigest()
        execution_id = execution_key or f"recipe-run-{digest[:24]}"
        simulated = any(item.execution_source == "simulated" for item in selected)
        verified = all(item.quality == "verified" for item in selected)
        record = EvidenceRecord(
            evidence_id=f"derived-{digest[:32]}",
            tenant_id=investigation.request.tenant_id,
            project_id=investigation.request.project_id,
            execution_source="simulated" if simulated else "unknown",
            artifact_ref=stored.ref,
            kind=f"analysis_recipe/{recipe.recipe_id}",
            mime_type="application/json",
            sha256=stored.sha256,
            producer="analysis-recipe",
            producer_version=recipe.version,
            collected_at=datetime.now(timezone.utc),
            locator=EvidenceLocator(kind="sample_window", value="complete output"),
            quality="simulated" if simulated else ("verified" if verified else "partial"),
            parser_version=recipe.version,
            derived_from=[item.evidence_id for item in selected],
            limitations=["deterministic registered recipe output"],
            source_type="analysis",
            source_evidence_id=f"{recipe.recipe_id}:{recipe.version}:output",
            analysis_job_id=execution_id,
        )
        outcome = self.evidence.ingest(investigation_id, record)
        if not outcome.canonical_evidence_id:
            raise ValueError(outcome.reason or "recipe output evidence was rejected")
        persisted = self.repository.get_evidence(outcome.canonical_evidence_id)
        return AnalysisRecipeExecution(execution_id, output, persisted)
