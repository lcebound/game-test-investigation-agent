from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from game_test_agent.contracts.models import (
    BuildRef,
    CaseMemoryDraft,
    CaseMemoryRecord,
    CaseMemoryStatus,
    CaseMemoryTransition,
    HistoricalCaseReference,
    InvestigationStatus,
    VersionInterval,
)
from game_test_agent.storage import ConflictError, NotFoundError, Repository, canonical_json


_ALLOWED_TRANSITIONS: dict[CaseMemoryStatus, set[CaseMemoryStatus]] = {
    CaseMemoryStatus.CANDIDATE: {
        CaseMemoryStatus.REVIEWED,
        CaseMemoryStatus.RETRACTED,
    },
    CaseMemoryStatus.REVIEWED: {
        CaseMemoryStatus.ACTIVE,
        CaseMemoryStatus.RETRACTED,
    },
    CaseMemoryStatus.ACTIVE: {
        CaseMemoryStatus.DEPRECATED,
        CaseMemoryStatus.RETRACTED,
    },
    CaseMemoryStatus.DEPRECATED: {CaseMemoryStatus.RETRACTED},
    CaseMemoryStatus.RETRACTED: set(),
}


def _natural_key(value: str) -> tuple[tuple[int, int | str], ...]:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.findall(r"\d+|\D+", value)
    )


def _within(value: str, interval: VersionInterval) -> bool:
    key = _natural_key(value)
    if interval.min_inclusive is not None and key < _natural_key(interval.min_inclusive):
        return False
    if interval.max_exclusive is not None and key >= _natural_key(interval.max_exclusive):
        return False
    return True


class CaseMemoryService:
    """Reviewed cross-investigation hints, isolated from current evidence ledgers."""

    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def create_candidate(
        self,
        source_investigation_id: str,
        draft: CaseMemoryDraft,
        *,
        created_by: str,
        valid_until: datetime | None = None,
    ) -> tuple[CaseMemoryRecord, bool]:
        view = self.repository.get_investigation(source_investigation_id)
        if view.status != InvestigationStatus.COMPLETED:
            raise ConflictError("only completed investigations can produce case-memory candidates")
        report, _markdown, report_hash = self.repository.get_latest_report(source_investigation_id)
        evidence_ids = sorted(
            {evidence_id for claim in report.claims for evidence_id in claim.evidence_ids}
        )
        known_evidence = {
            item.evidence_id
            for item in self.repository.list_current_evidence(source_investigation_id)
        }
        if not set(evidence_ids).issubset(known_evidence):
            raise ConflictError("source report references evidence outside its investigation")
        identity = {
            "source_investigation_id": source_investigation_id,
            "source_report_revision": report.revision,
            "source_report_hash": report_hash,
            "content": draft.content,
            "applicability": draft.applicability,
        }
        case_id = "case-" + hashlib.sha256(canonical_json(identity).encode()).hexdigest()[:32]
        try:
            existing = self.repository.get_case_memory(case_id)
        except NotFoundError:
            pass
        else:
            return existing, False
        now = datetime.now(timezone.utc)
        record = CaseMemoryRecord(
            case_id=case_id,
            revision=1,
            tenant_id=view.request.tenant_id,
            project_id=view.request.project_id,
            status=CaseMemoryStatus.CANDIDATE,
            confidence=draft.confidence,
            content=draft.content,
            applicability=draft.applicability,
            source_investigation_id=source_investigation_id,
            source_report_revision=report.revision,
            source_report_hash=report_hash,
            source_evidence_ids=evidence_ids,
            created_by=created_by,
            state_reason="candidate_created_from_completed_investigation",
            state_changed_by=created_by,
            valid_until=valid_until,
            created_at=now,
            updated_at=now,
        )
        return self.repository.save_case_memory(record, expected_previous_revision=0)

    def transition(
        self,
        case_id: str,
        transition: CaseMemoryTransition,
        *,
        actor: str,
    ) -> CaseMemoryRecord:
        current = self.repository.get_case_memory(case_id)
        if current.revision != transition.expected_revision:
            raise ConflictError(f"case memory expected revision {current.revision}")
        if transition.target_status not in _ALLOWED_TRANSITIONS[current.status]:
            raise ConflictError(
                f"case memory cannot transition from {current.status} "
                f"to {transition.target_status}"
            )
        reviewed_by = current.reviewed_by
        review_notes = current.review_notes
        if transition.target_status == CaseMemoryStatus.REVIEWED:
            reviewed_by = actor
            review_notes = transition.review_notes or transition.reason
        elif transition.review_notes is not None:
            review_notes = transition.review_notes
        if transition.target_status == CaseMemoryStatus.ACTIVE and not reviewed_by:
            raise ConflictError("case memory must be reviewed before activation")
        updated = current.model_copy(
            update={
                "revision": current.revision + 1,
                "status": transition.target_status,
                "confidence": transition.confidence or current.confidence,
                "reviewed_by": reviewed_by,
                "review_notes": review_notes,
                "state_reason": transition.reason,
                "state_changed_by": actor,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        persisted, _ = self.repository.save_case_memory(
            updated,
            expected_previous_revision=current.revision,
        )
        return persisted

    def retract_for_source(
        self,
        source_investigation_id: str,
        *,
        actor: str,
        reason: str,
    ) -> list[CaseMemoryRecord]:
        retracted: list[CaseMemoryRecord] = []
        for current in self.repository.list_current_case_memories_for_source(
            source_investigation_id
        ):
            if current.status == CaseMemoryStatus.RETRACTED:
                continue
            retracted.append(
                self.transition(
                    current.case_id,
                    CaseMemoryTransition(
                        expected_revision=current.revision,
                        target_status=CaseMemoryStatus.RETRACTED,
                        reason=reason,
                    ),
                    actor=actor,
                )
            )
        return retracted

    def search(
        self,
        tenant_id: str,
        project_id: str,
        *,
        query: str | None = None,
        engine_family: str | None = None,
        engine_version: str | None = None,
        build_ref: BuildRef | None = None,
        environment_fingerprint: str | None = None,
        limit: int = 5,
        now: datetime | None = None,
    ) -> list[HistoricalCaseReference]:
        if not 1 <= limit <= 50:
            raise ValueError("case-memory search limit must be between 1 and 50")
        if query is not None and len(query) > 2_000:
            raise ValueError("case-memory search query cannot exceed 2000 characters")
        current_time = now or datetime.now(timezone.utc)
        terms = {item for item in (query or "").casefold().split() if item}
        results: list[HistoricalCaseReference] = []
        records = self.repository.list_current_case_memories(
            tenant_id,
            project_id,
            statuses={CaseMemoryStatus.ACTIVE},
        )
        for record in records:
            if record.valid_until is not None and record.valid_until <= current_time:
                continue
            if not self._is_applicable(
                record,
                engine_family=engine_family,
                engine_version=engine_version,
                build_ref=build_ref,
                environment_fingerprint=environment_fingerprint,
            ):
                continue
            searchable = " ".join(
                [
                    record.content.title,
                    *record.content.symptoms,
                    record.content.confirmed_outcome,
                    *record.content.tags,
                ]
            ).casefold()
            if terms and not all(term in searchable for term in terms):
                continue
            score = sum(searchable.count(term) for term in terms) if terms else 1
            score += {"low": 0, "medium": 2, "high": 4}[record.confidence]
            results.append(
                HistoricalCaseReference(
                    case_id=record.case_id,
                    revision=record.revision,
                    title=record.content.title,
                    symptoms=record.content.symptoms,
                    confirmed_outcome=record.content.confirmed_outcome,
                    effective_experiments=record.content.effective_experiments,
                    ineffective_experiments=record.content.ineffective_experiments,
                    limitations=record.content.limitations,
                    applicability=record.applicability,
                    confidence=record.confidence,
                    source_investigation_id=record.source_investigation_id,
                    score=score,
                )
            )
        results.sort(key=lambda item: (-item.score, item.case_id))
        return results[:limit]

    @staticmethod
    def _is_applicable(
        record: CaseMemoryRecord,
        *,
        engine_family: str | None,
        engine_version: str | None,
        build_ref: BuildRef | None,
        environment_fingerprint: str | None,
    ) -> bool:
        applicability = record.applicability
        if applicability.engine_family is not None:
            if engine_family is None or (
                applicability.engine_family.casefold() != engine_family.casefold()
            ):
                return False
        if applicability.engine_version is not None:
            if engine_version is None or not _within(engine_version, applicability.engine_version):
                return False
        if applicability.build_ids:
            if build_ref is None or build_ref.id not in applicability.build_ids:
                return False
        if applicability.build_revision is not None:
            if (
                build_ref is None
                or build_ref.revision is None
                or not _within(build_ref.revision, applicability.build_revision)
            ):
                return False
        if applicability.environment_fingerprints:
            if environment_fingerprint not in applicability.environment_fingerprints:
                return False
        return True
