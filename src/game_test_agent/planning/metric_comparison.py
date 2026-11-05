from __future__ import annotations

from game_test_agent.planning.controls import execution_controls

import hashlib
import json
import math
from dataclasses import dataclass, field

from game_test_agent.contracts.models import EvidenceRecord, ExperimentCandidate
from game_test_agent.storage import ArtifactStore, Repository, payload_hash


@dataclass
class MetricComparison:
    samples: list[tuple[str, dict]] = field(default_factory=list)
    comparison_key: str | None = None
    action_id: str | None = None
    limitations: list[str] = field(default_factory=list)

    @property
    def compared_evidence_ids(self) -> list[str]:
        return sorted(item[0] for item in self.samples)


class MetricComparisonService:
    """Collect comparable CSV summaries backed by distinct, ingested real Runs."""

    def __init__(self, repository: Repository, artifacts: ArtifactStore) -> None:
        self.repository = repository
        self.artifacts = artifacts

    def collect(
        self, investigation_id: str, derived: EvidenceRecord,
        output: dict, candidate: ExperimentCandidate,
    ) -> MetricComparison | None:
        investigation = self.repository.get_investigation(investigation_id)
        minimum_runs = investigation.request.constraints.get("min_comparable_runs", 1)
        if (
            derived.kind != "analysis_recipe/csv_summary_v1"
            or not any(
                prediction.observation_key.startswith("columns.")
                for predictions in candidate.predictions.values()
                for prediction in predictions
            )
        ):
            return None
        current = {
            item.evidence_id: item
            for item in self.repository.list_current_evidence(investigation_id)
        }
        source = current.get(derived.derived_from[0]) if len(derived.derived_from) == 1 else None
        if source is None or source.source_type != "run":
            return None
        batch = MetricComparison(samples=[(derived.evidence_id, output)])
        if (
            not isinstance(minimum_runs, int) or isinstance(minimum_runs, bool)
            or not 1 <= minimum_runs <= 3
            or minimum_runs > investigation.request.budget.max_real_runs
        ):
            batch.limitations.append("invalid_repeat_requirement")
            return batch
        signature = self._signature(investigation_id, source)
        if (
            signature is None or derived.quality not in {"verified", "partial"}
            or not self._recipe_completed(investigation_id, derived, source)
        ):
            batch.limitations.append("run_metric_source_not_comparable")
            return batch
        batch.action_id, batch.comparison_key = signature
        if minimum_runs == 1:
            return batch
        by_action: dict[str, tuple[str, dict]] = {batch.action_id: (derived.evidence_id, output)}
        ambiguous_actions: set[str] = set()
        for item in current.values():
            if (
                item.evidence_id == derived.evidence_id
                or item.kind != derived.kind
                or item.producer_version != derived.producer_version
                or item.source_type != "analysis"
                or item.quality not in {"verified", "partial"}
                or len(item.derived_from) != 1
            ):
                continue
            other_source = current.get(item.derived_from[0])
            if other_source is None or other_source.source_type != "run":
                continue
            other_signature = self._signature(investigation_id, other_source)
            if other_signature is None or other_signature[1] != batch.comparison_key:
                continue
            if not self._recipe_completed(investigation_id, item, other_source):
                batch.limitations.append("comparable_metric_recipe_unverified")
                continue
            action_id = other_signature[0]
            if action_id in by_action:
                ambiguous_actions.add(action_id)
                continue
            other_output = self._read_summary(item)
            if other_output is None:
                batch.limitations.append("comparable_metric_output_invalid")
                continue
            by_action[action_id] = (item.evidence_id, other_output)
        if ambiguous_actions:
            batch.limitations.append("multiple_metric_sources_per_run")
        batch.samples = sorted(by_action.values(), key=lambda item: item[0])
        if len(batch.samples) < minimum_runs:
            batch.limitations.append("insufficient_comparable_runs")
        return batch

    def _recipe_completed(
        self, investigation_id: str, derived: EvidenceRecord, source: EvidenceRecord
    ) -> bool:
        if (
            derived.source_type != "analysis"
            or derived.producer != "analysis-recipe"
            or derived.producer_version != "1"
            or derived.parser_version != "1"
            or derived.derived_from != [source.evidence_id]
        ):
            return False
        return any(
            event["data"].get("recipe_id") == "csv_summary_v1"
            and event["data"].get("recipe_version") == "1"
            and event["data"].get("input_evidence_ids") == [source.evidence_id]
            and event["data"].get("input_sha256") == [source.sha256]
            and event["data"].get("output_evidence_id") == derived.evidence_id
            and event["data"].get("output_sha256") == derived.sha256
            for event in self.repository.list_recent_events_by_type(
                investigation_id, "analysis.recipe_completed", limit=500
            )
        )

    def _signature(
        self, investigation_id: str, source: EvidenceRecord
    ) -> tuple[str, str] | None:
        investigation = self.repository.get_investigation(investigation_id)
        request = investigation.request
        selector = request.constraints.get("metric_source_selector")
        source_identity: dict[str, str | None]
        if selector is None:
            source_identity = {"source_evidence_id": source.source_evidence_id}
        else:
            required = {"kind", "mime_type", "producer", "producer_version"}
            if (
                not isinstance(selector, dict) or set(selector) != required
                or any(not isinstance(selector[key], str) or not selector[key]
                       for key in required)
                or any(getattr(source, key) != selector[key] for key in required)
            ):
                return None
            matching_sources = [
                item for item in self.repository.list_current_evidence(investigation_id)
                if item.source_type == "run" and item.action_id == source.action_id
                and all(getattr(item, key) == selector[key] for key in required)
            ]
            if len(matching_sources) != 1:
                return None
            source_identity = {key: selector[key] for key in sorted(required)}
        if (
            source.action_id is None or source.run_ref is None
            or source.source_type != "run" or source.execution_source != "real"
            or source.retrieval_status != "ready"
            or source.quality not in {"verified", "partial"}
            or source.tenant_id != request.tenant_id
            or source.project_id != request.project_id
            or source.run_ref.project_id != request.project_id
            or source.mime_type.split(";", 1)[0].strip().lower() != "text/csv"
        ):
            return None
        try:
            source_content = self.artifacts.read_bytes(source.artifact_ref)
        except OSError:
            return None
        if hashlib.sha256(source_content).hexdigest() != source.sha256:
            return None
        try:
            action, _, _ = self.repository.get_action(source.action_id)
        except LookupError:
            return None
        planned = self.repository.get_planned_action(investigation_id, action.plan_revision)
        snapshot = self.repository.get_run_result_by_ref(
            request.tenant_id, source.run_ref, source.source_revision
        )
        expected_environment = request.constraints.get("environment_fingerprint")
        if (
            action.investigation_id != investigation_id
            or planned is None or planned.action.action_id != action.action_id
            or planned.action.capability_id != action.capability_id
            or planned.action.capability_version != action.capability_version
            or planned.action.parameters != action.parameters
            or planned.action.target_selector != action.target_selector
            or snapshot is None or snapshot.action_id != action.action_id
            or snapshot.ingestion_state != "ingested"
            or snapshot.result.execution_source != "real"
            or snapshot.result.execution_status not in {"succeeded", "failed"}
            or action.provider_id != source.run_ref.provider_id
            or request.build_ref is None
            or action.build_ref != request.build_ref
            or source.build_ref != request.build_ref
            or snapshot.result.build_ref != request.build_ref
            or not expected_environment
            or action.target_selector.environment_fingerprint != expected_environment
            or source.environment_fingerprint != expected_environment
            or snapshot.result.environment_fingerprint != expected_environment
        ):
            return None
        identity = {
            "capability_id": action.capability_id,
            "capability_version": action.capability_version,
            "provider_id": action.provider_id,
            "provider_revision": action.provider_revision,
            "test_ref": action.test_ref,
            "parameters": action.parameters,
            "target_selector": action.target_selector,
            "constraints": execution_controls(action.constraints),
            "build_ref": action.build_ref,
            "environment_fingerprint": expected_environment,
            "source_identity": source_identity,
            "source_kind": source.kind,
            "source_mime_type": source.mime_type,
            "source_producer": source.producer,
            "source_producer_version": source.producer_version,
            "recipe_id": "csv_summary_v1",
            "recipe_version": "1",
        }
        return action.action_id, payload_hash(identity)

    def _read_summary(self, evidence: EvidenceRecord) -> dict | None:
        try:
            content = self.artifacts.read_bytes(evidence.artifact_ref)
            if hashlib.sha256(content).hexdigest() != evidence.sha256:
                return None
            output = json.loads(content)
        except (OSError, ValueError, UnicodeDecodeError):
            return None
        if not isinstance(output, dict):
            return None
        columns = output.get("columns")
        if not isinstance(columns, dict):
            return None
        for column in columns.values():
            if not isinstance(column, dict):
                return None
            if any(
                not isinstance(column.get(key), (int, float))
                or isinstance(column.get(key), bool)
                or not math.isfinite(column[key])
                for key in ("min", "max", "median", "p95")
            ):
                return None
        return output
