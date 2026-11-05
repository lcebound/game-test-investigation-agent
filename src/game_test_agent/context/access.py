from __future__ import annotations

import hashlib
from urllib.parse import unquote, urlparse

from game_test_agent.contracts.models import (
    ContextAccessScope,
    ContextSummary,
    EventPage,
    EvidenceExcerpt,
    EvidenceRecord,
    EvidenceSearchFilters,
    EvidenceSearchHit,
    RunRef,
    RunResultSnapshot,
)
from game_test_agent.storage import ArtifactStore, Repository
from game_test_agent.memory import InvestigationSummaryService


class ContextAccess:
    """Scoped read-only access to indexed metadata and immutable evidence artifacts."""

    _TEXT_MIME_TYPES = {
        "application/json",
        "application/ld+json",
        "application/x-ndjson",
        "application/xml",
        "application/yaml",
    }

    def __init__(self, repository: Repository, artifacts: ArtifactStore) -> None:
        self.repository = repository
        self.artifacts = artifacts

    def search_evidence(
        self,
        investigation_id: str,
        scope: ContextAccessScope,
        *,
        filters: EvidenceSearchFilters | None = None,
        query: str | None = None,
        limit: int = 20,
    ) -> list[EvidenceSearchHit]:
        if not 1 <= limit <= 100:
            raise ValueError("evidence search limit must be between 1 and 100")
        if query is not None and len(query) > 1000:
            raise ValueError("evidence search query cannot exceed 1000 characters")
        self._require_investigation_scope(investigation_id, scope)
        selected_filters = filters or EvidenceSearchFilters()
        facts = self.repository.list_facts(investigation_id)
        facts_by_evidence: dict[str, list[tuple[str, str]]] = {}
        for fact in facts:
            for evidence_id in fact.evidence_ids:
                facts_by_evidence.setdefault(evidence_id, []).append((fact.fact_id, fact.statement))

        terms = [item for item in (query or "").casefold().split() if item]
        hits: list[EvidenceSearchHit] = []
        for evidence in self.repository.list_current_evidence(investigation_id):
            self._require_evidence_scope(evidence, scope)
            if not self._matches(evidence, selected_filters):
                continue
            related_facts = facts_by_evidence.get(evidence.evidence_id, [])
            searchable = " ".join(
                [
                    evidence.evidence_id,
                    evidence.kind,
                    evidence.mime_type,
                    evidence.producer,
                    evidence.producer_version,
                    evidence.source_evidence_id or "",
                    evidence.action_id or "",
                    evidence.build_ref.id if evidence.build_ref else "",
                    evidence.environment_fingerprint or "",
                    " ".join(evidence.limitations),
                    " ".join(statement for _, statement in related_facts),
                ]
            ).casefold()
            if terms and not all(term in searchable for term in terms):
                continue
            score = sum(searchable.count(term) for term in terms)
            hits.append(
                EvidenceSearchHit(
                    evidence=evidence,
                    matching_fact_ids=[fact_id for fact_id, _ in related_facts],
                    score=score,
                )
            )
        hits.sort(
            key=lambda item: (
                item.score,
                item.evidence.collected_at,
                item.evidence.evidence_id,
            ),
            reverse=True,
        )
        return hits[:limit]

    def read_evidence(
        self,
        investigation_id: str,
        evidence_id: str,
        scope: ContextAccessScope,
        *,
        byte_offset: int = 0,
        max_bytes: int = 64 * 1024,
    ) -> EvidenceExcerpt:
        if byte_offset < 0:
            raise ValueError("byte offset must be non-negative")
        if not 1 <= max_bytes <= 1024 * 1024:
            raise ValueError("evidence reads must be between 1 byte and 1 MiB")
        self._require_investigation_scope(investigation_id, scope)
        evidence = next(
            (
                item
                for item in self.repository.list_current_evidence(investigation_id)
                if item.evidence_id == evidence_id
            ),
            None,
        )
        if evidence is None:
            raise LookupError(f"evidence {evidence_id!r} is not part of this investigation")
        self._require_evidence_scope(evidence, scope)
        if evidence.retrieval_status != "ready":
            raise ValueError(f"evidence is not readable: {evidence.retrieval_status}")
        if not self._is_text(evidence.mime_type):
            raise ValueError("binary evidence requires a media-capable artifact reader")
        _, content = self.read_artifact(investigation_id, evidence_id, scope)
        if byte_offset > len(content):
            raise ValueError("byte offset is beyond the end of the artifact")
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("text evidence must be UTF-8 and offsets must align") from error
        return self._excerpt(evidence, content, byte_offset=byte_offset, max_bytes=max_bytes)

    @staticmethod
    def _excerpt(evidence, content, *, byte_offset, max_bytes, base_offset=0, total_bytes=None):
        """Slice already verified UTF-8 bytes; a search buffer may cover only part of an artifact."""
        local_offset = byte_offset - base_offset
        if not 0 <= local_offset <= len(content):
            raise ValueError("byte offset is outside the verified buffer")
        if local_offset < len(content) and content[local_offset] & 0xC0 == 0x80:
            raise ValueError("text evidence offsets must align to UTF-8")
        chunk_end = min(len(content), local_offset + max_bytes)
        while chunk_end > local_offset:
            try:
                text = content[local_offset:chunk_end].decode("utf-8")
                break
            except UnicodeDecodeError as error:
                if error.reason != "unexpected end of data":
                    raise ValueError("text evidence must be valid UTF-8") from error
                chunk_end -= 1
        else:
            if local_offset < len(content):
                raise ValueError("max_bytes is too small for the next UTF-8 character")
            text = ""
        chunk = content[local_offset:chunk_end]
        next_offset = base_offset + chunk_end
        size = len(content) if total_bytes is None else total_bytes
        truncated = next_offset < size
        return EvidenceExcerpt(
            evidence_id=evidence.evidence_id,
            artifact_ref=evidence.artifact_ref,
            mime_type=evidence.mime_type,
            locator=evidence.locator,
            text=text,
            byte_offset=byte_offset,
            returned_bytes=len(chunk),
            total_bytes=size,
            next_byte_offset=next_offset if truncated else None,
            truncated=truncated,
            source_revision=evidence.source_revision,
            source_sha256=evidence.sha256,
            excerpt_sha256=hashlib.sha256(chunk).hexdigest(),
        )

    def resolve_artifact(self, investigation_id: str, evidence_id: str, scope: ContextAccessScope):
        """Resolve authorization on the repository owner thread, before background I/O."""
        self._require_investigation_scope(investigation_id, scope)
        evidence = next(
            (
                item
                for item in self.repository.list_current_evidence(investigation_id)
                if item.evidence_id == evidence_id
            ),
            None,
        )
        if evidence is None:
            raise LookupError("evidence is not part of this investigation")
        self._require_evidence_scope(evidence, scope)
        self._require_artifact_scope(evidence.artifact_ref, scope)
        if evidence.retrieval_status != "ready":
            raise ValueError("evidence is not ready")
        return evidence

    def read_artifact(self, investigation_id: str, evidence_id: str, scope: ContextAccessScope):
        """Read a current immutable artifact, including binary media, with the same scope checks."""
        evidence = self.resolve_artifact(investigation_id, evidence_id, scope)
        content = self.artifacts.read_bytes(evidence.artifact_ref)
        if hashlib.sha256(content).hexdigest() != evidence.sha256.lower():
            raise IOError("artifact content does not match the evidence record")
        return evidence, content

    def get_run(
        self,
        scope: ContextAccessScope,
        run_ref: RunRef,
        *,
        revision: int | None = None,
    ) -> RunResultSnapshot | None:
        if run_ref.project_id != scope.project_id:
            raise PermissionError("cross-project run access is forbidden")
        return self.repository.get_run_result_by_ref(scope.tenant_id, run_ref, revision)

    def get_history(
        self,
        investigation_id: str,
        scope: ContextAccessScope,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> EventPage:
        if after < 0:
            raise ValueError("history cursor must be non-negative")
        self._require_investigation_scope(investigation_id, scope)
        events = self.repository.list_events(investigation_id, after=after, limit=limit)
        return EventPage(
            events=events,
            next_cursor=int(events[-1]["cursor"]) if events else after,
        )

    def get_summary(
        self, investigation_id: str, scope: ContextAccessScope
    ) -> ContextSummary | None:
        self._require_investigation_scope(investigation_id, scope)
        return InvestigationSummaryService(self.repository).current(investigation_id)

    def _require_investigation_scope(
        self, investigation_id: str, scope: ContextAccessScope
    ) -> None:
        investigation = self.repository.get_investigation(investigation_id)
        request = investigation.request
        if (request.tenant_id, request.project_id) != (scope.tenant_id, scope.project_id):
            raise PermissionError("cross-project context access is forbidden")

    @staticmethod
    def _require_evidence_scope(evidence: EvidenceRecord, scope: ContextAccessScope) -> None:
        if (evidence.tenant_id, evidence.project_id) != (scope.tenant_id, scope.project_id):
            raise PermissionError("cross-project evidence access is forbidden")

    @classmethod
    def _matches(cls, evidence: EvidenceRecord, filters: EvidenceSearchFilters) -> bool:
        equality_checks = {
            "kind": evidence.kind,
            "mime_type": evidence.mime_type,
            "quality": evidence.quality,
            "retrieval_status": evidence.retrieval_status,
            "source_type": evidence.source_type,
            "action_id": evidence.action_id,
            "environment_fingerprint": evidence.environment_fingerprint,
        }
        for field, actual in equality_checks.items():
            expected = getattr(filters, field)
            if expected is not None and actual != expected:
                return False
        if filters.provider_id is not None and (
            evidence.run_ref is None or evidence.run_ref.provider_id != filters.provider_id
        ):
            return False
        if filters.run_id is not None and (
            evidence.run_ref is None or evidence.run_ref.run_id != filters.run_id
        ):
            return False
        if filters.build_id is not None and (
            evidence.build_ref is None or evidence.build_ref.id != filters.build_id
        ):
            return False
        if filters.collected_after and evidence.collected_at < filters.collected_after:
            return False
        if filters.collected_before and evidence.collected_at > filters.collected_before:
            return False
        return True

    @classmethod
    def _is_text(cls, mime_type: str) -> bool:
        normalized = mime_type.split(";", 1)[0].strip().lower()
        return normalized.startswith("text/") or normalized in cls._TEXT_MIME_TYPES

    @staticmethod
    def _require_artifact_scope(ref: str, scope: ContextAccessScope) -> None:
        parsed = urlparse(ref)
        if parsed.scheme == "artifact":
            parts = parsed.path.strip("/").split("/")
            tenant = parsed.netloc
            if len(parts) != 2 or (tenant, parts[0]) != (
                scope.tenant_id,
                scope.project_id,
            ):
                raise PermissionError("artifact reference is outside the requested scope")
            return
        if parsed.scheme == "s3+sha256":
            parts = unquote(parsed.path).strip("/").split("/")
            if len(parts) < 4 or tuple(parts[-4:-2]) != (
                scope.tenant_id,
                scope.project_id,
            ):
                raise PermissionError("artifact reference is outside the requested scope")
            return
        raise ValueError("unsupported artifact reference")
