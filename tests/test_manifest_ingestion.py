from __future__ import annotations

import asyncio
import hashlib

import pytest

from game_test_agent.contracts.models import (
    EvidenceManifestItem,
    InvestigationRequest,
    RunRef,
    RunResult,
    TestObjective as Objective,
)
from game_test_agent.evidence import EvidenceManifestIngestor
from game_test_agent.storage import LocalArtifactStore
from game_test_agent.storage import ConflictError


class FakeFetcher:
    provider_id = "lab-a"

    def __init__(self, content: bytes) -> None:
        self.content = content

    async def fetch_artifact(self, artifact_ref: str):
        return self.content, "text/csv"


def test_manifest_download_is_hashed_stored_and_scoped(repository, settings):
    async def scenario() -> None:
        investigation, _ = repository.create_investigation(
            InvestigationRequest(
                tenant_id="studio-a",
                project_id="game-a",
                objective=Objective(objective_id="perf", description="验证性能"),
            ),
            "manifest-1",
        )
        content = b"frame_ms\n16\n17\n"
        result = RunResult(
            run_ref=RunRef(provider_id="lab-a", project_id="game-a", run_id="run-1"),
            revision=1,
            execution_status="succeeded",
            test_verdict="passed",
            execution_source="real",
            provider_status="DONE",
            evidence_manifest=[
                EvidenceManifestItem(
                    evidence_id="ev-frame",
                    artifact_ref="provider://artifact/frame",
                    kind="frame_metrics",
                    mime_type="text/csv",
                    sha256=hashlib.sha256(content).hexdigest(),
                    locator={"kind": "sample_window", "value": "all rows"},
                    quality="verified",
                    producer="lab-a",
                    producer_version="1",
                )
            ],
        )
        records = await EvidenceManifestIngestor(
            repository, LocalArtifactStore(settings.artifact_root)
        ).ingest(investigation.investigation_id, result, FakeFetcher(content))
        assert records[0].evidence_id.startswith("obs-")
        assert records[0].source_evidence_id == "ev-frame"
        assert records[0].artifact_ref.startswith("artifact://studio-a/game-a/")
        assert (
            repository.list_evidence(investigation.investigation_id)[0].sha256
            == hashlib.sha256(content).hexdigest()
        )
        facts = repository.list_facts(investigation.investigation_id)
        assert len(facts) == 1
        assert facts[0].extractor == "MetricCsvAnalyzer"
        assert any(
            event["event_type"] == "fact.extracted"
            for event in repository.list_events(investigation.investigation_id)
        )

    asyncio.run(scenario())


def test_simulated_result_forces_simulated_evidence(repository, settings):
    async def scenario() -> None:
        investigation, _ = repository.create_investigation(
            InvestigationRequest(
                tenant_id="studio-a",
                project_id="game-a",
                objective=Objective(objective_id="perf", description="验证性能"),
            ),
            "manifest-sim",
        )
        content = b"metric\n1\n"
        result = RunResult(
            run_ref=RunRef(provider_id="lab-a", project_id="game-a", run_id="run-sim"),
            revision=1,
            execution_status="succeeded",
            test_verdict="passed",
            execution_source="simulated",
            provider_status="DONE",
            evidence_manifest=[
                EvidenceManifestItem(
                    evidence_id="ev-sim-manifest",
                    artifact_ref="provider://artifact/sim",
                    kind="metrics",
                    mime_type="text/csv",
                    sha256=hashlib.sha256(content).hexdigest(),
                    locator={"kind": "sample_window", "value": "all"},
                    quality="verified",
                    producer="sim",
                    producer_version="1",
                )
            ],
        )
        records = await EvidenceManifestIngestor(
            repository, LocalArtifactStore(settings.artifact_root)
        ).ingest(investigation.investigation_id, result, FakeFetcher(content))
        assert records[0].quality == "simulated"
        assert "simulated execution" in records[0].limitations

    asyncio.run(scenario())


def test_parser_failure_keeps_source_evidence_and_emits_visible_event(repository, settings):
    async def scenario() -> None:
        investigation, _ = repository.create_investigation(
            InvestigationRequest(
                tenant_id="studio-a",
                project_id="game-a",
                objective=Objective(objective_id="perf-invalid", description="分析损坏指标"),
            ),
            "manifest-invalid-parser",
        )
        content = b"wrong_column\nnot-a-number\n"
        result = RunResult(
            run_ref=RunRef(provider_id="lab-a", project_id="game-a", run_id="run-invalid"),
            revision=1,
            execution_status="succeeded",
            test_verdict="unknown",
            execution_source="real",
            provider_status="DONE",
            evidence_manifest=[
                EvidenceManifestItem(
                    evidence_id="ev-invalid-frame",
                    artifact_ref="provider://artifact/invalid-frame",
                    kind="frame_metrics",
                    mime_type="text/csv",
                    sha256=hashlib.sha256(content).hexdigest(),
                    locator={"kind": "sample_window", "value": "all"},
                    quality="partial",
                    producer="lab-a",
                    producer_version="1",
                )
            ],
        )

        records = await EvidenceManifestIngestor(
            repository, LocalArtifactStore(settings.artifact_root)
        ).ingest(investigation.investigation_id, result, FakeFetcher(content))

        assert len(records) == 1
        assert records[0].evidence_id.startswith("obs-")
        assert records[0].source_evidence_id == "ev-invalid-frame"
        assert repository.list_facts(investigation.investigation_id) == []
        failures = [
            item
            for item in repository.list_events(investigation.investigation_id)
            if item["event_type"] == "evidence.analysis_failed"
        ]
        assert failures[0]["data"] == {
            "evidence_id": records[0].evidence_id,
            "kind": "frame_metrics",
            "error_type": "ValueError",
        }

    asyncio.run(scenario())


def test_identical_bytes_from_distinct_runs_remain_distinct_observations(repository, settings):
    async def scenario() -> None:
        investigation, _ = repository.create_investigation(
            InvestigationRequest(
                tenant_id="studio-a",
                project_id="game-a",
                objective=Objective(objective_id="repeat", description="验证重复运行"),
            ),
            "manifest-distinct-runs",
        )
        content = b"frame_ms\n16\n17\n"
        ingestor = EvidenceManifestIngestor(repository, LocalArtifactStore(settings.artifact_root))

        async def ingest(run_id: str) -> str:
            result = RunResult(
                run_ref=RunRef(provider_id="lab-a", project_id="game-a", run_id=run_id),
                revision=1,
                execution_status="succeeded",
                test_verdict="passed",
                execution_source="real",
                provider_status="DONE",
                evidence_manifest=[
                    EvidenceManifestItem(
                        evidence_id="frame-metrics",
                        artifact_ref=f"provider://artifact/{run_id}",
                        kind="frame_metrics",
                        mime_type="text/csv",
                        sha256=hashlib.sha256(content).hexdigest(),
                        locator={"kind": "sample_window", "value": "all"},
                        quality="verified",
                        producer="lab-a",
                        producer_version="1",
                    )
                ],
            )
            return (
                await ingestor.ingest(investigation.investigation_id, result, FakeFetcher(content))
            )[0].evidence_id

        first = await ingest("run-a")
        second = await ingest("run-b")
        assert first != second
        assert len(repository.list_evidence(investigation.investigation_id)) == 2
        assert len(repository.list_facts(investigation.investigation_id)) == 2

    asyncio.run(scenario())


def test_same_source_revision_replays_or_conflicts(repository, settings):
    async def scenario() -> None:
        investigation, _ = repository.create_investigation(
            InvestigationRequest(
                tenant_id="studio-a",
                project_id="game-a",
                objective=Objective(objective_id="identity", description="验证源身份"),
            ),
            "manifest-source-identity",
        )
        ingestor = EvidenceManifestIngestor(repository, LocalArtifactStore(settings.artifact_root))

        def result_for(content: bytes) -> RunResult:
            return RunResult(
                run_ref=RunRef(provider_id="lab-a", project_id="game-a", run_id="run-1"),
                revision=1,
                execution_status="succeeded",
                test_verdict="passed",
                execution_source="real",
                provider_status="DONE",
                evidence_manifest=[
                    EvidenceManifestItem(
                        evidence_id="metrics",
                        artifact_ref="provider://artifact/metrics",
                        kind="frame_metrics",
                        mime_type="text/csv",
                        sha256=hashlib.sha256(content).hexdigest(),
                        locator={"kind": "sample_window", "value": "all"},
                        quality="verified",
                        producer="lab-a",
                        producer_version="1",
                    )
                ],
            )

        original = b"frame_ms\n10\n"
        first = await ingestor.ingest(
            investigation.investigation_id, result_for(original), FakeFetcher(original)
        )
        replay = await ingestor.ingest(
            investigation.investigation_id, result_for(original), FakeFetcher(original)
        )
        assert replay[0].evidence_id == first[0].evidence_id
        assert len(repository.list_evidence(investigation.investigation_id)) == 1

        changed = b"frame_ms\n20\n"
        with pytest.raises(ConflictError):
            await ingestor.ingest(
                investigation.investigation_id,
                result_for(changed),
                FakeFetcher(changed),
            )

    asyncio.run(scenario())


def test_source_observation_is_not_replayed_into_another_investigation(repository, settings):
    async def scenario() -> None:
        first, _ = repository.create_investigation(
            InvestigationRequest(
                tenant_id="studio-a",
                project_id="game-a",
                objective=Objective(objective_id="first", description="首个调查"),
            ),
            "manifest-first-investigation",
        )
        second, _ = repository.create_investigation(
            InvestigationRequest(
                tenant_id="studio-a",
                project_id="game-a",
                objective=Objective(objective_id="second", description="第二个调查"),
            ),
            "manifest-second-investigation",
        )
        content = b"frame_ms\n10\n"
        result = RunResult(
            run_ref=RunRef(provider_id="lab-a", project_id="game-a", run_id="shared-run"),
            revision=1,
            execution_status="succeeded",
            test_verdict="passed",
            execution_source="real",
            provider_status="DONE",
            evidence_manifest=[
                EvidenceManifestItem(
                    evidence_id="metrics",
                    artifact_ref="provider://artifact/shared",
                    kind="frame_metrics",
                    mime_type="text/csv",
                    sha256=hashlib.sha256(content).hexdigest(),
                    locator={"kind": "sample_window", "value": "all"},
                    quality="verified",
                    producer="lab-a",
                    producer_version="1",
                )
            ],
        )
        ingestor = EvidenceManifestIngestor(repository, LocalArtifactStore(settings.artifact_root))
        await ingestor.ingest(first.investigation_id, result, FakeFetcher(content))
        with pytest.raises(ValueError, match="another investigation"):
            await ingestor.ingest(second.investigation_id, result, FakeFetcher(content))
        assert repository.list_evidence(second.investigation_id) == []

    asyncio.run(scenario())
