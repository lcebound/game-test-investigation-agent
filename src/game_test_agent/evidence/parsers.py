from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import Field, model_validator

from game_test_agent.contracts.models import Fact, StrictModel
from game_test_agent.evidence.ledger import HypothesisLedger
from game_test_agent.storage import ArtifactStore, Repository, canonical_json


@dataclass(frozen=True, slots=True)
class MetricSummary:
    count: int
    minimum: float
    maximum: float
    mean: float
    p50: float
    p95: float
    p99: float
    unit: str

    def as_dict(self) -> dict[str, int | float | str]:
        return {
            "count": self.count,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "mean": self.mean,
            "p50": self.p50,
            "p95": self.p95,
            "p99": self.p99,
            "unit": self.unit,
        }


class MetricCsvAnalyzer:
    PARSER_VERSION = "metric-csv-v1"
    MAX_ROWS = 1_000_000

    def __init__(self, repository: Repository, artifacts: ArtifactStore) -> None:
        self.repository = repository
        self.artifacts = artifacts
        self.ledger = HypothesisLedger(repository)

    def analyze(
        self,
        investigation_id: str,
        evidence_id: str,
        *,
        column: str,
        unit: str,
    ) -> Fact:
        evidence_by_id = {
            item.evidence_id: item
            for item in self.repository.list_current_evidence(investigation_id)
        }
        evidence = evidence_by_id.get(evidence_id)
        if evidence is None:
            raise ValueError(f"evidence {evidence_id!r} was not found")
        if evidence.retrieval_status != "ready":
            raise ValueError("only ready evidence can be parsed")
        if evidence.mime_type not in {"text/csv", "application/csv"}:
            raise ValueError("metric CSV analyzer only accepts CSV evidence")
        content = self.artifacts.read_bytes(evidence.artifact_ref)
        if hashlib.sha256(content).hexdigest() != evidence.sha256.lower():
            raise ValueError("evidence hash does not match artifact content")
        values = self._read_values(content, column)
        summary = self._summarize(values, unit)
        identity = {
            "evidence_id": evidence_id,
            "column": column,
            "unit": unit,
            "parser_version": self.PARSER_VERSION,
            "summary": summary.as_dict(),
        }
        fact_id = "fact-" + hashlib.sha256(canonical_json(identity).encode()).hexdigest()[:24]
        fact = Fact(
            fact_id=fact_id,
            investigation_id=investigation_id,
            statement=(
                f"证据 {evidence_id} 的 {column} 共 {summary.count} 个样本："
                f"P50={summary.p50:.3f}{unit}，P95={summary.p95:.3f}{unit}，"
                f"P99={summary.p99:.3f}{unit}。"
            ),
            evidence_ids=[evidence_id],
            extractor="MetricCsvAnalyzer",
            extractor_version=self.PARSER_VERSION,
            data={"column": column, **summary.as_dict()},
        )
        self.ledger.add_fact(fact)
        return fact

    @classmethod
    def _read_values(cls, content: bytes, column: str) -> list[float]:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise ValueError("CSV evidence must be UTF-8 encoded") from error
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames or column not in reader.fieldnames:
            raise ValueError(f"CSV column {column!r} does not exist")
        values: list[float] = []
        for row_number, row in enumerate(reader, start=2):
            if len(values) >= cls.MAX_ROWS:
                raise ValueError("CSV exceeds the parser row limit")
            raw = row.get(column)
            if raw is None or not raw.strip():
                continue
            try:
                value = float(raw)
            except ValueError as error:
                raise ValueError(
                    f"non-numeric value in row {row_number}, column {column}"
                ) from error
            if not math.isfinite(value):
                raise ValueError(f"non-finite value in row {row_number}, column {column}")
            values.append(value)
        if not values:
            raise ValueError("CSV contains no numeric samples in the requested column")
        return values

    @staticmethod
    def _summarize(values: list[float], unit: str) -> MetricSummary:
        ordered = sorted(values)

        def percentile(percent: float) -> float:
            if len(ordered) == 1:
                return ordered[0]
            position = (len(ordered) - 1) * percent
            lower = math.floor(position)
            upper = math.ceil(position)
            if lower == upper:
                return ordered[lower]
            fraction = position - lower
            return ordered[lower] * (1 - fraction) + ordered[upper] * fraction

        return MetricSummary(
            count=len(ordered),
            minimum=ordered[0],
            maximum=ordered[-1],
            mean=sum(ordered) / len(ordered),
            p50=percentile(0.50),
            p95=percentile(0.95),
            p99=percentile(0.99),
            unit=unit,
        )


class AssertionObservation(StrictModel):
    expected: str = Field(max_length=4000)
    actual: str = Field(max_length=4000)
    message: str | None = Field(default=None, max_length=4000)


class TestFailureDocument(StrictModel):
    test_id: str = Field(min_length=1, max_length=512)
    test_version: str | None = Field(default=None, max_length=256)
    outcome: Literal["failed", "error", "timed_out"]
    assertion: AssertionObservation | None = None
    failed_step: str | None = Field(default=None, max_length=2000)
    preconditions: list[str] = Field(default_factory=list, max_length=100)
    seed: str | None = Field(default=None, max_length=256)


class CrashFrame(StrictModel):
    index: int = Field(ge=0)
    module: str = Field(min_length=1, max_length=512)
    symbol: str | None = Field(default=None, max_length=2000)
    file: str | None = Field(default=None, max_length=2000)
    line: int | None = Field(default=None, ge=1)


class CrashDocument(StrictModel):
    signal: str = Field(min_length=1, max_length=256)
    exception_code: str | None = Field(default=None, max_length=256)
    dump_kind: Literal["none", "minidump", "full"]
    symbols_matched: bool
    frames: list[CrashFrame] = Field(default_factory=list, max_length=10000)


class MemorySample(StrictModel):
    iteration: int = Field(ge=0)
    rss_bytes: int = Field(ge=0)
    heap_bytes: int | None = Field(default=None, ge=0)
    native_bytes: int | None = Field(default=None, ge=0)
    after_gc: bool = False
    after_unload: bool = False


class MemoryTrendDocument(StrictModel):
    memory_kind: Literal["rss", "managed_heap", "native", "gpu", "unknown"]
    warmup_iterations: int = Field(default=0, ge=0)
    samples: list[MemorySample] = Field(min_length=2, max_length=100000)

    @model_validator(mode="after")
    def unique_iterations(self) -> "MemoryTrendDocument":
        iterations = [item.iteration for item in self.samples]
        if len(iterations) != len(set(iterations)):
            raise ValueError("memory sample iterations must be unique")
        return self


class InfrastructureDocument(StrictModel):
    failure_domain: Literal["scheduler", "device", "network", "storage", "provider", "unknown"]
    error_code: str = Field(min_length=1, max_length=256)
    provider_status: str = Field(min_length=1, max_length=1000)
    affected_targets: list[str] = Field(default_factory=list, max_length=10000)
    retryable: bool | None = None


class StructuredJsonAnalyzer(ABC):
    PARSER_VERSION = "structured-json-v1"
    MAX_BYTES = 8 * 1024 * 1024
    DOCUMENT_MODEL: type[StrictModel]
    ALLOWED_KINDS: frozenset[str]
    EXTRACTOR: str

    def __init__(self, repository: Repository, artifacts: ArtifactStore) -> None:
        self.repository = repository
        self.artifacts = artifacts
        self.ledger = HypothesisLedger(repository)

    def analyze(self, investigation_id: str, evidence_id: str) -> Fact:
        evidence_by_id = {
            item.evidence_id: item
            for item in self.repository.list_current_evidence(investigation_id)
        }
        evidence = evidence_by_id.get(evidence_id)
        if evidence is None:
            raise ValueError(f"evidence {evidence_id!r} was not found")
        if evidence.retrieval_status != "ready":
            raise ValueError("only ready evidence can be parsed")
        if evidence.kind not in self.ALLOWED_KINDS:
            raise ValueError(f"{self.EXTRACTOR} does not accept evidence kind {evidence.kind!r}")
        if evidence.mime_type not in {"application/json", "application/problem+json"}:
            raise ValueError("structured analyzer only accepts JSON evidence")
        content = self.artifacts.read_bytes(evidence.artifact_ref)
        if len(content) > self.MAX_BYTES:
            raise ValueError("JSON evidence exceeds the parser size limit")
        if hashlib.sha256(content).hexdigest() != evidence.sha256.lower():
            raise ValueError("evidence hash does not match artifact content")
        try:
            payload = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("evidence is not valid UTF-8 JSON") from error
        if not isinstance(payload, dict):
            raise ValueError("structured evidence must be a JSON object")
        document = self.DOCUMENT_MODEL.model_validate(payload)
        statement, data = self._extract(document)
        identity = {
            "evidence_id": evidence_id,
            "parser_version": self.PARSER_VERSION,
            "extractor": self.EXTRACTOR,
            "data": data,
        }
        fact = Fact(
            fact_id="fact-" + hashlib.sha256(canonical_json(identity).encode()).hexdigest()[:24],
            investigation_id=investigation_id,
            statement=statement,
            evidence_ids=[evidence_id],
            extractor=self.EXTRACTOR,
            extractor_version=self.PARSER_VERSION,
            data=data,
        )
        self.ledger.add_fact(fact)
        return fact

    @abstractmethod
    def _extract(self, document: StrictModel) -> tuple[str, dict[str, Any]]:
        """Convert one validated provider document into a bounded factual statement."""


class FunctionalFailureJsonAnalyzer(StructuredJsonAnalyzer):
    PARSER_VERSION = "test-failure-json-v1"
    DOCUMENT_MODEL = TestFailureDocument
    ALLOWED_KINDS = frozenset({"test_failure", "test_report"})
    EXTRACTOR = "FunctionalFailureJsonAnalyzer"

    def _extract(self, document: StrictModel) -> tuple[str, dict[str, Any]]:
        parsed = TestFailureDocument.model_validate(document)
        location = f"，失败步骤为 {parsed.failed_step}" if parsed.failed_step else ""
        statement = (
            f"提供方结果记录用例 {parsed.test_id} 的执行结果为 {parsed.outcome}{location}；"
            "该事实只描述本次执行，不证明缺陷根因或其他运行结果。"
        )
        return statement, parsed.model_dump(mode="json")


class CrashJsonAnalyzer(StructuredJsonAnalyzer):
    PARSER_VERSION = "crash-json-v1"
    DOCUMENT_MODEL = CrashDocument
    ALLOWED_KINDS = frozenset({"crash", "crash_dump", "symbolized_stack"})
    EXTRACTOR = "CrashJsonAnalyzer"

    def _extract(self, document: StrictModel) -> tuple[str, dict[str, Any]]:
        parsed = CrashDocument.model_validate(document)
        ordered = sorted(parsed.frames, key=lambda item: item.index)
        top = next((item for item in ordered if item.symbol), ordered[0] if ordered else None)
        top_text = f"，首个可定位栈帧为 {top.module}!{top.symbol or '<unknown>'}" if top else ""
        statement = (
            f"崩溃证据记录信号 {parsed.signal}、dump={parsed.dump_kind}、"
            f"symbols_matched={str(parsed.symbols_matched).lower()}{top_text}；"
            "栈位置是观测，不单独等同于根因。"
        )
        data = parsed.model_dump(mode="json")
        data["top_locatable_frame"] = top.model_dump(mode="json") if top else None
        return statement, data


class MemoryTrendJsonAnalyzer(StructuredJsonAnalyzer):
    PARSER_VERSION = "memory-trend-json-v1"
    DOCUMENT_MODEL = MemoryTrendDocument
    ALLOWED_KINDS = frozenset({"memory_samples", "memory_trend"})
    EXTRACTOR = "MemoryTrendJsonAnalyzer"

    def _extract(self, document: StrictModel) -> tuple[str, dict[str, Any]]:
        parsed = MemoryTrendDocument.model_validate(document)
        samples = sorted(parsed.samples, key=lambda item: item.iteration)
        analysis_samples = [item for item in samples if item.iteration >= parsed.warmup_iterations]
        if len(analysis_samples) < 2:
            raise ValueError("at least two post-warmup memory samples are required")
        xs = [float(item.iteration) for item in analysis_samples]
        ys = [float(item.rss_bytes) for item in analysis_samples]
        x_mean = sum(xs) / len(xs)
        y_mean = sum(ys) / len(ys)
        denominator = sum((value - x_mean) ** 2 for value in xs)
        slope = (
            sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys, strict=True)) / denominator
            if denominator
            else 0.0
        )
        statement = (
            f"热身后 {len(analysis_samples)} 个 {parsed.memory_kind} 样本中，"
            f"RSS 从 {int(ys[0])} 变为 {int(ys[-1])} 字节，"
            f"线性斜率为 {slope:.3f} 字节/迭代；该趋势本身不能确认内存泄漏。"
        )
        return statement, {
            "memory_kind": parsed.memory_kind,
            "warmup_iterations": parsed.warmup_iterations,
            "sample_count": len(analysis_samples),
            "rss_start_bytes": int(ys[0]),
            "rss_end_bytes": int(ys[-1]),
            "rss_max_bytes": int(max(ys)),
            "rss_slope_bytes_per_iteration": slope,
            "post_gc_sample_count": sum(item.after_gc for item in analysis_samples),
            "post_unload_sample_count": sum(item.after_unload for item in analysis_samples),
        }


class InfrastructureJsonAnalyzer(StructuredJsonAnalyzer):
    PARSER_VERSION = "infrastructure-json-v1"
    DOCUMENT_MODEL = InfrastructureDocument
    ALLOWED_KINDS = frozenset({"infrastructure", "provider_error"})
    EXTRACTOR = "InfrastructureJsonAnalyzer"

    def _extract(self, document: StrictModel) -> tuple[str, dict[str, Any]]:
        parsed = InfrastructureDocument.model_validate(document)
        statement = (
            f"基础设施证据记录 {parsed.failure_domain} 域错误 {parsed.error_code}，"
            f"影响 {len(parsed.affected_targets)} 个目标；该记录不能归因为游戏代码缺陷。"
        )
        return statement, parsed.model_dump(mode="json")
