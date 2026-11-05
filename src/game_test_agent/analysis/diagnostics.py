"""Registered, deterministic crash and aligned frame-time analysis recipes."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from game_test_agent.analysis.recipes import AnalysisRecipeExecution, AnalysisRecipeRunner, AnalysisRecipeService
from game_test_agent.contracts.models import AnalysisRecipe, AnalysisRecipeStep
from game_test_agent.evidence.parsers import CrashDocument
from game_test_agent.storage import canonical_json


class DiagnosticRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipe: Literal["crash", "frame_time"]
    evidence_id: str = Field(min_length=1, max_length=256)
    data_pointer: str = Field(default="", max_length=500)
    frame_column: str = Field(default="frame_ms", min_length=1, max_length=128)
    component_columns: list[str] = Field(default_factory=list, max_length=8)
    timestamp_column: str | None = Field(default=None, max_length=128)
    time_from: datetime | None = None
    time_to: datetime | None = None

    @model_validator(mode="after")
    def valid_window(self):
        if self.data_pointer and not self.data_pointer.startswith("/"):
            raise ValueError("data_pointer must be a JSON Pointer")
        if len(set(self.component_columns)) != len(self.component_columns):
            raise ValueError("component columns must be unique")
        if self.recipe == "crash" and (self.component_columns or self.timestamp_column or self.time_from or self.time_to):
            raise ValueError("crash recipes do not accept frame windows")
        if self.time_from or self.time_to:
            if not self.timestamp_column:
                raise ValueError("time filtering requires an explicit timestamp column")
            if any(t is not None and t.tzinfo is None for t in (self.time_from, self.time_to)):
                raise ValueError("time windows require timezone-aware timestamps")
            if self.time_from and self.time_to and self.time_from > self.time_to:
                raise ValueError("time window is reversed")
        return self


def _payload(material, parameters):
    request = DiagnosticRequest.model_validate(parameters)
    value = next(iter(material["inputs"]["evidence"].values()))
    # The recipe service decodes application/json; text evidence is still a string.
    if isinstance(value, (str, bytes, bytearray)):
        value = json.loads(value)
    for part in request.data_pointer.split("/")[1:] if request.data_pointer else []:
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if not part.isdecimal() or (len(part) > 1 and part.startswith("0")):
                raise ValueError("invalid array index")
            value = value[int(part)]
        else:
            value = value[part]
    return request, value


def crash_diagnostic(material, parameters):
    _, value = _payload(material, parameters)
    crash = CrashDocument.model_validate(value)
    frames = sorted(crash.frames, key=lambda frame: frame.index)
    if len({f.index for f in frames}) != len(frames):
        raise ValueError("crash frame indices must be unique")
    located = [f for f in frames if f.symbol]
    return {"recipe": "crash-v1", "signal": crash.signal, "exception_code": crash.exception_code,
            "dump_kind": crash.dump_kind, "symbols_matched": crash.symbols_matched,
            "frame_count": len(frames), "symbolized_frame_count": len(located),
            "top_frames": [f.model_dump(mode="json") for f in frames[:20]],
            "top_locatable_frame": located[0].model_dump(mode="json") if located and crash.symbols_matched else None,
            "summary": f"Crash signal {crash.signal}; symbols_matched={crash.symbols_matched}; {len(frames)} frames.",
            "limitations": ["Stack location is an observation, not a confirmed root cause."]
                + ([] if crash.symbols_matched else ["Matching build symbols are required before source attribution."])}


def _number(row, column):
    value = row[column]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("frame measurements must be finite, nonnegative JSON numbers in milliseconds")
    return value


def frame_diagnostic(material, parameters):
    request, rows = _payload(material, parameters)
    if not isinstance(rows, list) or not 3 <= len(rows) <= 100_000:
        raise ValueError("frame analysis requires 3 to 100000 aligned sample rows")
    selected = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError("frame rows must be objects")
        if request.timestamp_column:
            stamp = datetime.fromisoformat(row[request.timestamp_column].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError("sample timestamps must have timezones")
            if request.time_from and stamp < request.time_from or request.time_to and stamp > request.time_to:
                continue
        values = {column: _number(row, column) for column in [request.frame_column, *request.component_columns]}
        selected.append((index, values))
    if len(selected) < 3:
        raise ValueError("the selected time window has fewer than three aligned samples")
    columns = {}
    for column in [request.frame_column, *request.component_columns]:
        values = sorted(row[column] for _, row in selected)
        columns[column] = {"samples": len(values), "unit": "ms", "min": values[0], "max": values[-1],
                           "p50": values[math.ceil(.5 * len(values)) - 1],
                           "p95": values[math.ceil(.95 * len(values)) - 1],
                           "p99": values[math.ceil(.99 * len(values)) - 1]}
    index, worst = max(selected, key=lambda item: item[1][request.frame_column])
    return {"recipe": "frame-time-v1", "columns": columns, "selected_samples": len(selected),
            "worst_frame": {"source_row": index, "aligned_measurements_ms": worst},
            "summary": f"{request.frame_column}: P95={columns[request.frame_column]['p95']} ms; max={columns[request.frame_column]['max']} ms.",
            "limitations": ["All declared columns must already use milliseconds.",
                            "Components are aligned to the same row; duration alone does not establish a CPU/GPU bottleneck.",
                            "One evidence artifact does not establish independent reproduction or recovery."]}


class NativeDiagnosticService:
    def __init__(self, repository, artifacts):
        self.repository = repository
        self.recipes = AnalysisRecipeService(repository, artifacts, AnalysisRecipeRunner(
            {("crash_diagnostic", "1"): crash_diagnostic, ("frame_diagnostic", "1"): frame_diagnostic},
            {"diagnostic": lambda result: isinstance(result, dict) and "limitations" in result}),
            max_input_bytes=4 * 1024 * 1024)

    def recipe(self, request):
        return AnalysisRecipe(recipe_id=f"native_{request.recipe}_v1", version="1",
            input_types=["text/plain", "application/json"], steps=[AnalysisRecipeStep(
                step_id="diagnose", operation="aggregate", component="crash_diagnostic" if request.recipe == "crash" else "frame_diagnostic",
                component_version="1", parameters=request.model_dump(mode="json"), output_type="json")],
            output_schema={"type": "object", "required": ["recipe", "summary", "limitations"]}, verifier="diagnostic")

    def execution_key(self, investigation_id, request):
        _, selected = self.recipes._select_inputs(investigation_id, [request.evidence_id])
        item = selected[0]
        identity = {"investigation_id": investigation_id, "recipe": self.recipe(request),
                    "sha256": item.sha256, "source_revision": item.source_revision,
                    "artifact_ref": item.artifact_ref}
        return "native-diagnostic-" + hashlib.sha256(canonical_json(identity).encode()).hexdigest()

    def cached(self, investigation_id, key):
        # The evidence row is the completion record: recovery also works if the process
        # stopped after ingestion but before diagnostic_completed or the conversation checkpoint.
        for item in self.repository.list_current_evidence(investigation_id):
            if item.analysis_job_id == key and item.producer == "analysis-recipe" and item.retrieval_status == "ready":
                return item
        return None

    def _completed(self, investigation_id, request, execution):
        self.repository.record_investigation_event(investigation_id, "analysis.diagnostic_completed", {
            "recipe": request.recipe, "input_evidence_id": request.evidence_id,
            "output_evidence_id": execution.evidence.evidence_id, "output_sha256": execution.evidence.sha256})
        return execution

    def execute(self, investigation_id, request: DiagnosticRequest):
        key = self.execution_key(investigation_id, request)
        item = self.cached(investigation_id, key)
        if item is not None:
            output = json.loads(self.recipes.artifacts.read_bytes(item.artifact_ref))
            return AnalysisRecipeExecution(key, output, item)
        execution = self.recipes.execute(investigation_id, self.recipe(request),
            [request.evidence_id], execution_key=key)
        return self._completed(investigation_id, request, execution)

    async def execute_async(self, investigation_id, request: DiagnosticRequest, *, execution_key):
        execution = await self.recipes.execute_async(investigation_id, self.recipe(request),
            [request.evidence_id], execution_key=execution_key)
        return self._completed(investigation_id, request, execution)
