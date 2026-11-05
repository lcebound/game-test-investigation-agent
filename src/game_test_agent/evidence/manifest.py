from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Protocol

from game_test_agent.contracts.models import EvidenceLocator, EvidenceRecord, RunResult
from game_test_agent.evidence.service import EvidenceService
from game_test_agent.evidence.analysis import EvidenceAnalyzerRouter
from game_test_agent.storage import ArtifactStore, ConflictError, Repository


class ArtifactFetcher(Protocol):
    provider_id: str

    async def fetch_artifact(self, artifact_ref: str) -> tuple[bytes, str]: ...


class EvidenceManifestIngestor:
    def __init__(
        self,
        repository: Repository,
        artifacts: ArtifactStore,
    ) -> None:
        self.repository = repository
        self.artifacts = artifacts
        self.evidence = EvidenceService(repository)
        self.analyzers = EvidenceAnalyzerRouter(repository, artifacts)

    async def ingest(
        self,
        investigation_id: str,
        result: RunResult,
        fetcher: ArtifactFetcher,
    ) -> list[EvidenceRecord]:
        investigation = self.repository.get_investigation(investigation_id)
        if result.run_ref.provider_id != fetcher.provider_id:
            raise PermissionError("result provider does not match artifact fetcher")
        if result.run_ref.project_id != investigation.request.project_id:
            raise PermissionError("result belongs to another project")
        expected_build = investigation.request.build_ref
        if expected_build and result.build_ref and result.build_ref.id != expected_build.id:
            raise ValueError("run result build does not match the investigation")
        expected_environment = investigation.request.constraints.get("environment_fingerprint")
        if (
            expected_environment
            and result.environment_fingerprint
            and result.environment_fingerprint != expected_environment
        ):
            raise ValueError("run result environment does not match the investigation")
        records: list[EvidenceRecord] = []
        created_records: list[EvidenceRecord] = []
        snapshot = self.repository.get_run_result_by_ref(
            investigation.request.tenant_id, result.run_ref, result.revision
        )
        action_id = snapshot.action_id if snapshot else None
        experiment_id = None
        run_sequence = None
        if action_id:
            action, _, _ = self.repository.get_action(action_id)
            experiment_id = action.experiment_id
            run_sequence = action.run_sequence
        for item in result.evidence_manifest:
            content, returned_mime = await fetcher.fetch_artifact(item.artifact_ref)
            if returned_mime != item.mime_type:
                raise ValueError(f"artifact MIME mismatch for {item.evidence_id}")
            digest = hashlib.sha256(content).hexdigest()
            if digest != item.sha256.lower():
                raise ValueError(f"artifact hash mismatch for {item.evidence_id}")
            stored = self.artifacts.put_bytes(
                investigation.request.tenant_id,
                investigation.request.project_id,
                content,
            )
            quality = "simulated" if result.execution_source == "simulated" else item.quality
            limitations = list(item.limitations)
            if result.execution_source == "simulated" and "simulated execution" not in limitations:
                limitations.append("simulated execution")
            if expected_build and result.build_ref is None:
                limitations.append("provider did not return build identity")
            if expected_environment and result.environment_fingerprint is None:
                limitations.append("provider did not return environment fingerprint")
            identity = json.dumps(
                {
                    "tenant_id": investigation.request.tenant_id,
                    "project_id": investigation.request.project_id,
                    "provider_id": result.run_ref.provider_id,
                    "run_id": result.run_ref.run_id,
                    "source_evidence_id": item.evidence_id,
                    "source_revision": result.revision,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            canonical_id = f"obs-{hashlib.sha256(identity.encode()).hexdigest()[:32]}"
            record = EvidenceRecord(
                evidence_id=canonical_id,
                tenant_id=investigation.request.tenant_id,
                project_id=investigation.request.project_id,
                run_ref=result.run_ref,
                build_ref=result.build_ref,
                environment_fingerprint=result.environment_fingerprint,
                execution_source=result.execution_source,
                artifact_ref=stored.ref,
                kind=item.kind,
                mime_type=item.mime_type,
                sha256=digest,
                producer=item.producer,
                producer_version=item.producer_version,
                collected_at=(
                    item.collected_at
                    or result.finished_at
                    or result.started_at
                    or datetime.now(timezone.utc)
                ),
                locator=EvidenceLocator.model_validate(item.locator),
                quality=quality,
                parser_version=None,
                limitations=limitations,
                source_type="run",
                source_evidence_id=item.evidence_id,
                source_revision=result.revision,
                action_id=action_id,
                experiment_id=experiment_id,
                run_sequence=run_sequence,
            )
            outcome = self.evidence.ingest(investigation_id, record)
            if outcome.status == "conflict":
                raise ConflictError(outcome.reason or "evidence source conflict")
            if not outcome.canonical_evidence_id:
                raise ValueError(outcome.reason or "evidence was rejected")
            persisted = self.repository.get_evidence(outcome.canonical_evidence_id)
            records.append(persisted)
            if outcome.status == "created":
                created_records.append(persisted)
        self.analyzers.analyze_records(investigation_id, created_records)
        return records
