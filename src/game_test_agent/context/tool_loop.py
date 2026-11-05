from __future__ import annotations

import csv
import hashlib
import io
import math
import re
from statistics import median

from game_test_agent.analysis import AnalysisRecipeRunner, AnalysisRecipeService
from game_test_agent.contracts.models import (
    AnalysisRecipe,
    AnalysisRecipeStep,
    AnalysisRequest,
    ContextAccessScope,
    EvidenceExcerpt,
    EvidenceSearchFilters,
    ReadRequest,
)
from game_test_agent.context.access import ContextAccess
from game_test_agent.storage import ArtifactStore, Repository


def _csv_summary(material: dict, parameters: dict) -> dict:
    if parameters:
        raise ValueError("csv_summary_v1 does not accept parameters")
    evidence_id, content = next(iter(material["inputs"]["evidence"].items()))
    if not isinstance(content, str):
        raise ValueError("CSV input must be text")
    rows = list(csv.DictReader(io.StringIO(content)))
    if not rows or not rows[0]:
        raise ValueError("CSV has no data rows")
    columns: dict[str, dict] = {}
    for name in rows[0]:
        values = []
        missing = 0
        for row in rows:
            raw = row.get(name)
            if raw is None or not raw.strip():
                missing += 1
                continue
            try:
                value = float(raw)
            except ValueError:
                continue
            if math.isfinite(value):
                values.append(value)
        if values:
            ordered = sorted(values)
            columns[name] = {
                "samples": len(values),
                "missing": missing,
                "unit": "ms" if name.endswith("_ms") else None,
                "median": median(ordered),
                "p95": ordered[math.ceil(0.95 * len(ordered)) - 1],
                "min": ordered[0],
                "max": ordered[-1],
            }
    if not columns:
        raise ValueError("CSV has no finite numeric columns")
    return {"input_evidence_id": evidence_id, "rows": len(rows), "columns": columns}


def _log_error_count(material: dict, parameters: dict) -> dict:
    if parameters:
        raise ValueError("log_error_count_v1 does not accept parameters")
    evidence_id, content = next(iter(material["inputs"]["evidence"].items()))
    if not isinstance(content, str):
        raise ValueError("log input must be text")
    locations = [
        {"line": number, "text": line[:300]}
        for number, line in enumerate(content.splitlines(), 1)
        if re.search(r"\b(error|fatal|exception)\b", line, re.IGNORECASE)
    ]
    return {
        "input_evidence_id": evidence_id,
        "count": len(locations),
        "locations": locations[:100],
        "locations_truncated": len(locations) > 100,
    }


_COMPONENTS = {
    "csv_summary_v1": ("csv_summary", _csv_summary),
    "log_error_count_v1": ("log_error_count", _log_error_count),
}


class InvestigationToolLoop:
    """Bounded, auditable read and deterministic analysis operations."""

    def __init__(self, repository: Repository, artifacts: ArtifactStore) -> None:
        self.repository = repository
        self.access = ContextAccess(repository, artifacts)
        self.recipes = AnalysisRecipeService(
            repository,
            artifacts,
            AnalysisRecipeRunner(
                {(name, "1"): component for name, component in _COMPONENTS.values()},
                {"nonempty": lambda result: isinstance(result, dict) and bool(result)},
            ),
        )

    def read(self, investigation_id: str, decision_id: str, request: ReadRequest) -> EvidenceExcerpt:
        investigation = self.repository.get_investigation(investigation_id)
        self.repository.reserve_counter(
            investigation_id,
            "tool_reads",
            f"read:{decision_id}",
            investigation.request.budget.max_tool_reads,
        )
        scope = ContextAccessScope(
            tenant_id=investigation.request.tenant_id,
            project_id=investigation.request.project_id,
        )
        current = next(
            (
                item for item in self.repository.list_current_evidence(investigation_id)
                if item.evidence_id == request.evidence_id
            ),
            None,
        )
        if current is None:
            raise LookupError("read target is no longer current")
        events = self.repository.list_recent_events(investigation_id)
        cached: EvidenceExcerpt | None = None
        for event in reversed(events):
            if event["event_type"] != "evidence.excerpt_read":
                continue
            data = event["data"]
            if (
                all(
                    (data.get("request") or {}).get(key) == getattr(request, key)
                    for key in ("evidence_id", "byte_offset", "max_bytes")
                )
                and data["excerpt"].get("source_sha256") == current.sha256
            ):
                cached = EvidenceExcerpt.model_validate(data["excerpt"])
                if hashlib.sha256(cached.text.encode("utf-8")).hexdigest() == cached.excerpt_sha256:
                    break
                cached = None
        excerpt = cached or self.access.read_evidence(
            investigation_id,
            request.evidence_id,
            scope,
            byte_offset=request.byte_offset,
            max_bytes=request.max_bytes,
        )
        self.repository.record_investigation_event(
            investigation_id,
            "evidence.excerpt_read",
            {
                "decision_id": decision_id,
                "purpose": request.purpose,
                "request": request.model_dump(mode="json"),
                "cache_hit": cached is not None,
                "excerpt": excerpt.model_dump(mode="json"),
            },
        )
        return excerpt

    def search(
        self,
        investigation_id: str,
        decision_id: str,
        query: str,
        filters: EvidenceSearchFilters | None = None,
    ) -> list[str]:
        investigation = self.repository.get_investigation(investigation_id)
        self.repository.reserve_counter(
            investigation_id,
            "tool_reads",
            f"search:{decision_id}",
            investigation.request.budget.max_tool_reads,
        )
        hits = self.access.search_evidence(
            investigation_id,
            ContextAccessScope(
                tenant_id=investigation.request.tenant_id,
                project_id=investigation.request.project_id,
            ),
            filters=filters,
            query=query,
            limit=20,
        )
        ids = [hit.evidence.evidence_id for hit in hits]
        self.repository.record_investigation_event(
            investigation_id,
            "evidence.search_completed",
            {"decision_id": decision_id, "query": query, "evidence_ids": ids},
        )
        return ids

    def analyze(
        self, investigation_id: str, decision_id: str, request: AnalysisRequest
    ) -> str:
        if request.parameters:
            raise ValueError("registered analysis recipes do not accept parameters")
        if len(request.input_evidence_ids) != 1:
            raise ValueError("registered analysis recipes require one input")
        investigation = self.repository.get_investigation(investigation_id)
        source = next(
            (
                item for item in self.repository.list_current_evidence(investigation_id)
                if item.evidence_id == request.input_evidence_ids[0]
            ),
            None,
        )
        expected_mime = "text/csv" if request.recipe_id == "csv_summary_v1" else "text/plain"
        if source is None or source.mime_type.split(";", 1)[0].strip().lower() != expected_mime:
            raise ValueError("analysis input MIME type does not match recipe")
        self.repository.reserve_counter(
            investigation_id,
            "tool_reads",
            f"analyze:{decision_id}",
            investigation.request.budget.max_tool_reads,
        )
        component, _ = _COMPONENTS[request.recipe_id]
        recipe = AnalysisRecipe(
            recipe_id=request.recipe_id,
            version=request.recipe_version,
            input_types=["text/plain", "text/csv"],
            steps=[AnalysisRecipeStep(
                step_id="calculate",
                operation="aggregate",
                component=component,
                component_version="1",
                output_type="json",
            )],
            output_schema={"type": "object"},
            verifier="nonempty",
        )
        execution = self.recipes.execute(
            investigation_id, recipe, request.input_evidence_ids
        )
        self.repository.record_investigation_event(
            investigation_id,
            "analysis.recipe_completed",
            {
                "decision_id": decision_id,
                "recipe_id": request.recipe_id,
                "recipe_version": request.recipe_version,
                "input_evidence_ids": request.input_evidence_ids,
                "input_sha256": [
                    item.sha256 for item in self.repository.list_current_evidence(investigation_id)
                    if item.evidence_id in request.input_evidence_ids
                ],
                "output_evidence_id": execution.evidence.evidence_id,
                "output_sha256": execution.evidence.sha256,
            },
        )
        return execution.evidence.evidence_id
