"""Source-aware investigation reads and conservative report checks.

Source fields are operator-configured JSON Pointers, never model annotations.
These checks establish provenance and literal/numeric consistency, not causality.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from game_test_agent.context.access import ContextAccess
from game_test_agent.contracts.models import ContextAccessScope, EvidenceAssertion


class NativeCitation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_id: str = Field(min_length=1, max_length=256)
    quote: str = Field(min_length=1, max_length=3000)


class NativeFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    finding_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=2000)
    citations: list[NativeCitation] = Field(min_length=1, max_length=8)
    scope: Literal["single_run", "cross_run", "unknown"] = "unknown"
    run_id: str | None = Field(default=None, max_length=256)
    provider_id: str | None = Field(default=None, max_length=128)
    build_id: str | None = Field(default=None, max_length=256)
    environment: str | None = Field(default=None, max_length=500)
    phase: str | None = Field(default=None, max_length=128)
    limitations: list[str] = Field(default_factory=list, max_length=10)
    assertions: list[EvidenceAssertion] = Field(default_factory=list, max_length=8)


def extract_source(text: str, fields: dict[str, str]) -> dict:
    """Retain only explicitly mapped scalar values from the exact saved response."""
    values = {}
    try:
        data = json.loads(text)
        json_available = True
    except (ValueError, TypeError):
        data = None
        json_available = False
    for name, pointer in fields.items():
        value = data
        try:
            for part in pointer.split("/")[1:]:
                part = part.replace("~1", "/").replace("~0", "~")
                if isinstance(value, list):
                    if not re.fullmatch(r"0|[1-9][0-9]*", part):
                        raise ValueError("invalid array index")
                    value = value[int(part)]
                else:
                    value = value[part]
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if not isinstance(value, str) or not value.strip() or len(value) > 500:
            continue
        if name == "execution_source" and value not in {"real", "simulated", "unknown"}:
            continue
        if name == "timestamp":
            try:
                if datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is None:
                    continue
            except ValueError:
                continue
        values[name] = value
    return {"values": values, "pointers": fields, "json_available": json_available,
            "trust": "provider_declared", "causality_verified": False}


def source_index(repository, investigation_id) -> dict[str, dict]:
    rows = repository.list_recent_events_by_types(
        investigation_id, ["mcp.native.tool_completed"], limit=5000
    )
    return {row["data"]["evidence_id"]: row["data"].get("source", {}) for row in rows}


_MEASURE = re.compile(
    r"(?<![0-9A-Za-z_.])(-?\d+(?:\.\d+)?)\s*"
    r"(milliseconds?|毫秒|ms|microseconds?|微秒|us|μs|seconds?|秒|s|fps|%|MB|MiB|GB|GiB)"
    r"(?![A-Za-z])", re.IGNORECASE
)
_UNITS = {"milliseconds": ("time", "0.001"), "millisecond": ("time", "0.001"),
          "毫秒": ("time", "0.001"), "ms": ("time", "0.001"),
          "microseconds": ("time", "0.000001"), "microsecond": ("time", "0.000001"),
          "微秒": ("time", "0.000001"), "us": ("time", "0.000001"),
          "μs": ("time", "0.000001"), "seconds": ("time", "1"),
          "second": ("time", "1"), "秒": ("time", "1"), "s": ("time", "1")}
_BROADEN = (r"每[一]?帧|所有帧|every\s+frame", r"总是|始终|always",
            r"每次|every\s+time", r"根因已确认|确认根因|root\s+cause\s+confirmed",
            r"修复已验证|fix\s+verified")


def _measurements(text):
    result = set()
    for number, unit in _MEASURE.findall(text):
        dimension, multiplier = _UNITS.get(unit.lower(), (unit.lower(), "1"))
        result.add((dimension, Decimal(number) * Decimal(multiplier)))
    return result


def _affirmed(pattern, text):
    """Keyword guards are a fallback only; do not treat negation as affirmation."""
    for match in re.finditer(pattern, text, re.I):
        prefix = re.split(r"[。；;.!?\n]|\bbut\b|然而|但是", text[:match.start()], flags=re.I)[-1][-48:]
        suffix = text[match.end():match.end() + 24]
        negated = re.search(r"\b(?:no|not|never|cannot|can't|unable|unconfirmed)\b|未|无法|不能|没有|并非|不足以|不(?:能|可|是|会)?", prefix, re.I)
        false_value = re.match(r"[\s\"':=]*(?:false\b|未|否|不成立)", suffix, re.I)
        if not negated and not false_value:
            return True
    return False


def text_consistency_errors(text: str, quotes: list[str]) -> list[str]:
    errors = []
    source = "\n".join(quotes)
    if _measurements(text) - _measurements(source):
        errors.append("Measured values/units must be supported by the cited original fragments.")
    for pattern in _BROADEN:
        if _affirmed(pattern, text) and not _affirmed(pattern, source):
            errors.append("Frequency, root-cause or fix claims exceed the cited observations.")
            break
    return errors


def finding_errors(finding: NativeFinding, sources: dict[str, dict], view) -> list[str]:
    # Structured assertions are independently rendered from data. Model prose
    # remains an interpretation, not the text certified by those assertions.
    errors = [] if finding.assertions else text_consistency_errors(finding.text, [c.quote for c in finding.citations])
    metadata = [sources.get(c.evidence_id, {}).get("values", {}) for c in finding.citations]
    providers = [sources.get(c.evidence_id, {}).get("provider_id") for c in finding.citations]
    if finding.provider_id is not None and any(p != finding.provider_id for p in providers):
        errors.append("provider_id is missing or inconsistent in cited source metadata.")
    for field in ("run_id", "build_id", "environment", "phase"):
        declared = getattr(finding, field)
        known = {item[field] for item in metadata if item.get(field)}
        if declared is not None and any(item.get(field) != declared for item in metadata):
            errors.append(f"{field} is missing or inconsistent in cited source metadata.")
        if field in {"build_id", "environment"} and len(known) > 1:
            errors.append(f"Cross-source comparison changes {field}; keep observations separate.")
    runs = {(provider, item.get("run_id")) for provider, item in zip(providers, metadata)}
    incomplete = any(not provider or not run for provider, run in runs)
    if len({(provider, run) for provider, run in runs if provider and run}) > 1 and finding.scope != "cross_run":
        errors.append("Multiple source runs require explicit cross_run scope.")
    if finding.scope == "single_run" and (incomplete or len(runs) != 1):
        errors.append("single_run requires one known, identical source run.")
    if finding.scope == "cross_run" and (incomplete or len(runs) < 2):
        errors.append("cross_run requires at least two explicitly identified source runs.")
    if finding.scope == "cross_run" and any(not item.get(key) for item in metadata
                                            for key in ("build_id", "environment")):
        errors.append("Cross-run comparison requires known build and environment for every source.")
    expected_build = view.request.build_ref
    expected_environment = view.request.constraints.get("environment_fingerprint")
    if expected_build and (finding.scope != "unknown" or finding.build_id is not None) and any(
        item.get("build_id") != expected_build.id for item in metadata
    ):
        errors.append("Cited sources do not establish the requested build; report this as a gap.")
    if expected_environment and (finding.scope != "unknown" or finding.environment is not None) and any(
        item.get("environment") != expected_environment for item in metadata
    ):
        errors.append("Cited sources do not establish the requested environment; report the gap.")
    return errors


class EvidenceSearch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=256)
    regex: bool = False
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)
    run_id: str | None = None
    provider_id: str | None = None
    build_id: str | None = None
    environment: str | None = None
    phase: str | None = None
    time_from: datetime | None = None
    time_to: datetime | None = None
    start_evidence_id: str | None = None
    byte_offset: int = Field(default=0, ge=0)
    max_hits: int = Field(default=10, ge=1, le=20)

    @model_validator(mode="after")
    def valid_times(self):
        for value in (self.time_from, self.time_to):
            if value is not None and value.tzinfo is None:
                raise ValueError("search timestamps must include a timezone")
        if self.time_from and self.time_to and self.time_from > self.time_to:
            raise ValueError("search time range is reversed")
        return self


# Regex executes in a killable child; untrusted patterns cannot stall the owner.
_SEARCH_WORKER = """
import json, re, sys
r = json.load(sys.stdin)
p = re.compile(r['query'] if r['regex'] else re.escape(r['query']), re.I)
hits = []
for i, text in enumerate(r['texts']):
    for m in p.finditer(text):
        if m.end() == m.start():
            continue
        hits.append([i, m.start(), m.end()])
        if len(hits) >= r['max_hits']:
            break
    if len(hits) >= r['max_hits']:
        break
json.dump(hits, sys.stdout)
"""


def _select_search_records(repository, view, request):
    records = sorted(repository.list_current_evidence(view.investigation_id), key=lambda e: e.evidence_id)
    known = {e.evidence_id for e in records if e.retrieval_status == "ready"}
    if set(request.evidence_ids) - known:
        raise ValueError("search evidence must be readable in this investigation")
    sources = source_index(repository, view.investigation_id)
    records = [e for e in records if e.evidence_id in known
               and (not request.evidence_ids or e.evidence_id in request.evidence_ids)]
    filtered = []
    for record in records:
        values = sources.get(record.evidence_id, {}).get("values", {})
        if request.provider_id is not None and sources.get(record.evidence_id, {}).get("provider_id") != request.provider_id:
            continue
        if any(getattr(request, key) is not None and values.get(key) != getattr(request, key)
               for key in ("run_id", "build_id", "environment", "phase")):
            continue
        if request.time_from or request.time_to:
            if not values.get("timestamp"):
                continue
            stamp = datetime.fromisoformat(values["timestamp"].replace("Z", "+00:00"))
            if request.time_from and stamp < request.time_from or request.time_to and stamp > request.time_to:
                continue
        filtered.append(record)
    if request.start_evidence_id is not None:
        positions = [i for i, e in enumerate(filtered) if e.evidence_id == request.start_evidence_id]
        if not positions:
            raise ValueError("search cursor is outside the selected evidence")
        filtered = filtered[positions[0]:]
    return filtered


def _read_search_chunk(artifacts, record, offset, allowance):
    # Pure I/O/computation: no database connection crosses into this worker.
    raw = artifacts.read_bytes(record.artifact_ref)
    if hashlib.sha256(raw).hexdigest() != record.sha256.lower():
        raise IOError("artifact content does not match the evidence record")
    if offset > len(raw):
        raise ValueError("search byte offset exceeds artifact size")
    while offset < len(raw) and raw[offset] & 0xC0 == 0x80:
        offset += 1
    end = min(len(raw), offset + min(2 * 1024 * 1024, allowance))
    while end < len(raw) and raw[end] & 0xC0 == 0x80:
        end -= 1
    try:
        body = raw[offset:end].decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("search supports UTF-8 text only; analyze binary evidence separately") from None
    return record, body, offset, end, len(raw)


def _prepare_search(repository, artifacts, view, request):
    filtered = _select_search_records(repository, view, request)
    access = ContextAccess(repository, artifacts)
    scope = ContextAccessScope(tenant_id=view.request.tenant_id, project_id=view.request.project_id)
    chunks, total = [], 0
    for record in filtered[:5]:
        record = access.resolve_artifact(view.investigation_id, record.evidence_id, scope)
        offset = request.byte_offset if not chunks else 0
        chunk = _read_search_chunk(artifacts, record, offset, 4 * 1024 * 1024 - total)
        chunks.append(chunk)
        total += chunk[3] - chunk[2]
        if chunk[3] < chunk[4] or total >= 4 * 1024 * 1024:
            break
    return chunks, filtered


def _match_chunks(request, chunks, timeout_seconds):
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-c", _SEARCH_WORKER],
            input=json.dumps({"query": request.query, "regex": request.regex,
                              "max_hits": request.max_hits, "texts": [c[1] for c in chunks]}),
            capture_output=True, text=True, encoding="utf-8", timeout=timeout_seconds,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        raise ValueError("search pattern exceeded its time limit; simplify it") from None
    if result.returncode:
        raise ValueError("invalid or unsupported search pattern")
    matches = json.loads(result.stdout)
    return matches


def _search_result(request, chunks, filtered, matches, sources):
    total = sum(chunk[3] - chunk[2] for chunk in chunks)
    hits, excerpts = [], []
    buffers = {index: chunk[1].encode("utf-8") for index, chunk in enumerate(chunks)}
    for index, start, end in matches:
        record, body, offset, _, size = chunks[index]
        # A bounded original excerpt; search results also enter the normal read audit trail.
        byte_start = offset + len(body[:max(0, start - 100)].encode())
        excerpt = ContextAccess._excerpt(record, buffers[index], byte_offset=byte_start,
                                        max_bytes=1000, base_offset=offset, total_bytes=size)
        excerpts.append(excerpt)
        hits.append({"evidence_id": record.evidence_id, "result": excerpt.text,
                     "byte_offset": excerpt.byte_offset, "next_byte_offset": excerpt.next_byte_offset,
                     "matched": True, "source": sources.get(record.evidence_id, {})})
    cursor = None
    if chunks:
        record, body, offset, end, size = chunks[-1]
        if len(matches) >= request.max_hits:
            index, _, stop = matches[-1]
            record, body, offset, end, size = chunks[index]
            cursor = {"start_evidence_id": record.evidence_id,
                      "byte_offset": offset + len(body[:stop].encode())}
        elif end < size:
            # Literal matches crossing the chunk boundary remain discoverable.
            overlap = 0 if request.regex else max(0, len(request.query.encode()) - 1)
            cursor = {"start_evidence_id": record.evidence_id, "byte_offset": max(offset + 1, end - overlap)}
        elif len(chunks) < len(filtered):
            cursor = {"start_evidence_id": filtered[len(chunks)].evidence_id, "byte_offset": 0}
    return {"hits": hits, "query": request.query, "next_cursor": cursor,
            "scanned_bytes": total, "complete": cursor is None,
            "match_scope": "within_scan_chunks" if request.regex else "literal_with_boundary_overlap",
            "result": "Found original excerpts." if hits else "No match in the scanned range.",
            "limitation": "Time filters use mapped artifact timestamps, not every log-line timestamp."}, excerpts


def _record_search(repository, view, result, excerpts):
    for excerpt in excerpts:
        repository.record_investigation_event(view.investigation_id, "evidence.excerpt_read",
                                             {"excerpt": excerpt.model_dump(mode="json")})
    return result


def _render_search(repository, artifacts, view, request, chunks, filtered, matches):
    return _record_search(repository, view, *_search_result(
        request, chunks, filtered, matches, source_index(repository, view.investigation_id)))


def search_evidence(repository, artifacts, view, request: EvidenceSearch, *, timeout_seconds=2):
    chunks, filtered = _prepare_search(repository, artifacts, view, request)
    matches = _match_chunks(request, chunks, timeout_seconds)
    return _render_search(repository, artifacts, view, request, chunks, filtered, matches)


async def search_evidence_async(repository, artifacts, view, request: EvidenceSearch, *, timeout_seconds=2):
    # Resolve scope and record audits on the owner thread; file I/O never blocks it.
    filtered = _select_search_records(repository, view, request)
    access = ContextAccess(repository, artifacts)
    scope = ContextAccessScope(tenant_id=view.request.tenant_id, project_id=view.request.project_id)
    chunks, total = [], 0
    for record in filtered[:5]:
        record = access.resolve_artifact(view.investigation_id, record.evidence_id, scope)
        offset = request.byte_offset if not chunks else 0
        chunk = await asyncio.to_thread(_read_search_chunk, artifacts, record, offset,
                                        4 * 1024 * 1024 - total)
        chunks.append(chunk)
        total += chunk[3] - chunk[2]
        if chunk[3] < chunk[4] or total >= 4 * 1024 * 1024:
            break
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-I", "-c", _SEARCH_WORKER,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    payload = json.dumps({"query": request.query, "regex": request.regex,
                          "max_hits": request.max_hits, "texts": [c[1] for c in chunks]}).encode()
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(payload), timeout=timeout_seconds)
    except (TimeoutError, asyncio.CancelledError) as error:
        if process.returncode is None:
            process.kill()
        await process.wait()
        if isinstance(error, asyncio.CancelledError):
            raise
        raise ValueError("search pattern exceeded its time limit; simplify it") from None
    if process.returncode:
        raise ValueError("invalid or unsupported search pattern")
    matches = json.loads(stdout)
    sources = source_index(repository, view.investigation_id)
    result, excerpts = await asyncio.to_thread(_search_result, request, chunks, filtered, matches, sources)
    # A source superseded while background work was running must not become a current hit.
    current = {item.evidence_id for item in repository.list_current_evidence(view.investigation_id)}
    if any(chunk[0].evidence_id not in current for chunk in chunks):
        raise ValueError("search evidence changed during execution; restart the search")
    return _record_search(repository, view, result, excerpts)
