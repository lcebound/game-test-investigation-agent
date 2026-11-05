"""Shared read identities and live cache checks for selection and execution."""
from __future__ import annotations

import hashlib
import time

from game_test_agent.memory.recall import HistorySearch, RecallService
from game_test_agent.storage import canonical_json
from game_test_agent.storage.feedback import FeedbackStore
from game_test_agent.workflows.native_evidence import EvidenceSearch
from game_test_agent.workflows.native_questions import input_fingerprint
from game_test_agent.workflows.native_recall import ContextRead


LOCAL_READS = frozenset({"agent_read_collected_evidence", "agent_search_evidence",
                         "agent_read_context", "agent_find_history"})


def read_identity(name, arguments, bindings, read_schema):
    if name not in LOCAL_READS:
        provider, tool = bindings[name]
        return arguments, canonical_json(["mcp", provider.provider_id, tool.name, arguments])
    schema, kind = {
        "agent_read_collected_evidence": (read_schema, "evidence"),
        "agent_search_evidence": (EvidenceSearch, "search"),
        "agent_read_context": (ContextRead, "context"),
        "agent_find_history": (HistorySearch, "history_search"),
    }[name]
    normalized = schema.model_validate(arguments).model_dump(mode="json")
    if name in {"agent_read_collected_evidence", "agent_read_context"}:
        normalized["query"] = normalized["query"] or None
    return normalized, canonical_json([kind, normalized])


def read_state_fingerprint(repository, view, name, arguments):
    """Bind directories and negative searches to their complete metadata universe."""
    if name == "agent_search_evidence":
        rows = sorted((e.evidence_id, e.sha256, e.source_revision, str(e.retrieval_status))
                      for e in repository.list_current_evidence(view.investigation_id))
        return hashlib.sha256(canonical_json(rows).encode()).hexdigest()
    evidence_id = (arguments["evidence_id"] if name == "agent_read_collected_evidence" else
                   arguments["ref"][len("evidence/"):] if name == "agent_read_context"
                   and arguments["ref"].startswith("evidence/") else None)
    if evidence_id is not None:
        rows = [(e.evidence_id, e.sha256, e.source_revision, str(e.retrieval_status))
                for e in repository.list_current_evidence(view.investigation_id) if e.evidence_id == evidence_id]
        return hashlib.sha256(canonical_json(rows).encode()).hexdigest()
    if name == "agent_read_context" and arguments["ref"] == "inputs":
        return input_fingerprint(repository.list_user_inputs(view.investigation_id))
    return None


def reusable_read(repository, view, memory, name, arguments, key, *, check_ttl=True):
    """Return a still-readable memo, without obtaining any artifact contents."""
    memo = memory.cache.get(key)
    if not memo:
        return None
    payload = memo["payload"]
    valid = True
    try:
        if name == "agent_find_history":
            valid = False  # Discovery must see newly finished reports.
        fingerprint = read_state_fingerprint(repository, view, name, arguments)
        if fingerprint is not None and fingerprint != memo.get("state_fingerprint"):
            valid = False
        records = {e.evidence_id: e for e in repository.list_current_evidence(view.investigation_id)
                   if e.retrieval_status == "ready"}
        ids = {payload["evidence_id"]} if payload.get("evidence_id") else set()
        ids.update(hit["evidence_id"] for hit in payload.get("hits", []))
        if ids - records.keys():
            valid = False
        if payload.get("evidence_id") and payload.get("sha256") and valid:
            record = records[payload["evidence_id"]]
            valid = (record.sha256 == payload["sha256"] and
                     record.source_revision == payload.get("source_revision", record.source_revision))
        kind = payload.get("resource_kind")
        if kind == "user_input":
            row = next((r for r in repository.list_user_inputs(view.investigation_id)
                        if r["command_id"] == payload["input_id"]), None)
            valid &= row is not None and hashlib.sha256(row["payload"]["content"].encode()).hexdigest() == payload["sha256"]
        if kind == "feedback_directory":
            valid &= payload.get("generation") == FeedbackStore(repository).generation(view.investigation_id)
        if kind == "run_feedback":
            digest = FeedbackStore(repository).validate_reference(view.investigation_id, ContextRead.model_validate(arguments))
            valid &= digest == payload["sha256"]
        if kind == "historical_directory":
            valid &= payload.get("generation") == RecallService(repository).generation(view.investigation_id)
        if payload.get("historical"):
            valid &= RecallService(repository).validate_cached(view, payload)
        if check_ttl and name not in LOCAL_READS:
            valid &= time.time() - memo["saved_at"] < 30
    except (ValueError, LookupError, PermissionError, KeyError, TypeError):
        valid = False
    if not valid:
        memory.cache.pop(key, None)
        return None
    return memo


def refresh_search_coverage(repository, view, memory):
    if memory.search_coverage:
        fingerprint = read_state_fingerprint(repository, view, "agent_search_evidence", {})
        memory.search_coverage = {key: value for key, value in memory.search_coverage.items()
                                  if value.get("state_fingerprint") == fingerprint}


def validate_context_reference(repository, view, request):
    """Check ownership, pins and reference shape; no reads, writes or read charge."""
    if request.ref == "history" or request.ref.startswith("history/"):
        RecallService(repository).validate_reference(view, request)
        return
    if request.ref == "feedbacks" or request.ref.startswith("feedback/"):
        FeedbackStore(repository).validate_reference(view.investigation_id, request)
        return
    if request.ref == "inputs":
        if request.byte_offset or request.expected_sha256:
            raise ValueError("input directory uses offset/limit/query")
        return
    if request.ref.startswith("evidence/"):
        if request.offset and request.pointer is None:
            raise ValueError("evidence pages use byte_offset")
        record = next((e for e in repository.list_current_evidence(view.investigation_id)
                       if e.evidence_id == request.ref[len("evidence/"):] and e.retrieval_status == "ready"), None)
        if record is None:
            raise PermissionError("evidence is unavailable")
        if request.expected_sha256 and record.sha256 != request.expected_sha256:
            raise ValueError("evidence reference is stale")
        return
    if request.ref.startswith("input/") and not request.offset:
        row = next((r for r in repository.list_user_inputs(view.investigation_id)
                    if r["command_id"] == request.ref[len("input/"):]), None)
        if row is None:
            raise PermissionError("input is unavailable")
        if request.expected_sha256 and hashlib.sha256(row["payload"]["content"].encode()).hexdigest() != request.expected_sha256:
            raise ValueError("input reference is stale")
        return
    raise ValueError("unknown context reference")
