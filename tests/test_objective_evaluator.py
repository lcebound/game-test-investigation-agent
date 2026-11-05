from __future__ import annotations

import asyncio
import hashlib

from game_test_agent.contracts.models import (
    EvidenceManifestItem,
    InvestigationRequest,
    Resolution,
    RunRef,
    RunResult,
    TestObjective as Objective,
)
from game_test_agent.evidence import EvidenceManifestIngestor
from game_test_agent.objectives import ObjectiveEvaluator
from game_test_agent.reports import ReportBuilder
from game_test_agent.storage import LocalArtifactStore


class MetricFetcher:
    provider_id = "portable-lab"

    def __init__(self, content: bytes) -> None:
        self.content = content

    async def fetch_artifact(self, _artifact_ref: str):
        return self.content, "text/csv"


def test_objective_requires_two_distinct_runs_before_passing(repository, settings):
    async def scenario() -> None:
        investigation, _ = repository.create_investigation(
            InvestigationRequest(
                tenant_id="studio-a",
                project_id="game-a",
                objective=Objective(objective_id="frame-budget", description="P95 不高于 13ms"),
                constraints={
                    "objective_spec": {
                        "criterion_id": "frame-p95",
                        "objective_id": "frame-budget",
                        "metric": "frame_ms",
                        "statistic": "p95",
                        "comparison": "le",
                        "threshold": 13,
                        "unit": "ms",
                        "min_independent_runs": 2,
                        "max_spread": 3,
                        "allowed_execution_sources": ["real"],
                    }
                },
            ),
            "objective-two-runs",
        )
        ingestor = EvidenceManifestIngestor(repository, LocalArtifactStore(settings.artifact_root))
        evaluator = ObjectiveEvaluator(repository)

        async def add_run(run_id: str, content: bytes) -> None:
            result = RunResult(
                run_ref=RunRef(provider_id="portable-lab", project_id="game-a", run_id=run_id),
                revision=1,
                execution_status="succeeded",
                test_verdict="passed",
                execution_source="real",
                provider_status="COMPLETE",
                evidence_manifest=[
                    EvidenceManifestItem(
                        evidence_id="frame-csv",
                        artifact_ref=f"opaque://{run_id}",
                        kind="frame_metrics",
                        mime_type="text/csv",
                        sha256=hashlib.sha256(content).hexdigest(),
                        locator={"kind": "sample_window", "value": "all"},
                        quality="verified",
                        producer="portable-lab",
                        producer_version="1",
                    )
                ],
            )
            await ingestor.ingest(investigation.investigation_id, result, MetricFetcher(content))

        await add_run("run-1", b"frame_ms\n10\n12\n")
        first = evaluator.evaluate(investigation.investigation_id)
        assert first is not None
        assert first.verdict == "insufficient"
        assert "requires 2 independent runs; found 1" in first.missing_requirements

        await add_run("run-2", b"frame_ms\n11\n13\n")
        final = evaluator.evaluate(investigation.investigation_id)
        assert final is not None
        assert final.verdict == "passed"
        assert len(final.supporting_fact_ids) == 2
        assert final.scope == "real_target"
        assert evaluator.evaluate(investigation.investigation_id) == final

        report = ReportBuilder(repository).build(investigation.investigation_id, "objective_passed")
        assert report.resolution == Resolution.GOAL_MET
        assert report.coverage.completed_objectives == ["frame-budget"]
        assert report.coverage.uncovered_objectives == []

        await add_run("run-3", b"frame_ms\n14\n14\n")
        failed = evaluator.evaluate(investigation.investigation_id)
        assert failed is not None
        assert failed.verdict == "failed"
        assert len(failed.contradicting_fact_ids) == 1
        failed_report = ReportBuilder(repository).build(
            investigation.investigation_id, "objective_failed"
        )
        assert failed_report.resolution == Resolution.GOAL_FAILED
        assert failed_report.coverage.completed_objectives == ["frame-budget"]

    asyncio.run(scenario())


def test_objective_reassessment_uses_corrected_source_revision(repository, settings):
    async def scenario() -> None:
        investigation, _ = repository.create_investigation(
            InvestigationRequest(
                tenant_id="studio-a",
                project_id="game-a",
                objective=Objective(objective_id="corrected", description="use corrected metric"),
                constraints={
                    "objective_spec": {
                        "criterion_id": "corrected-p95",
                        "objective_id": "corrected",
                        "metric": "frame_ms",
                        "statistic": "p95",
                        "comparison": "le",
                        "threshold": 13,
                        "unit": "ms",
                    }
                },
            ),
            "objective-corrected-revision",
        )
        ingestor = EvidenceManifestIngestor(repository, LocalArtifactStore(settings.artifact_root))
        evaluator = ObjectiveEvaluator(repository)

        async def ingest_revision(revision: int, content: bytes) -> None:
            await ingestor.ingest(
                investigation.investigation_id,
                RunResult(
                    run_ref=RunRef(
                        provider_id="portable-lab",
                        project_id="game-a",
                        run_id="stable-run",
                    ),
                    revision=revision,
                    execution_status="succeeded",
                    test_verdict="passed",
                    execution_source="real",
                    provider_status="COMPLETE",
                    evidence_manifest=[
                        EvidenceManifestItem(
                            evidence_id="frame-csv",
                            artifact_ref=f"opaque://stable-run/{revision}",
                            kind="frame_metrics",
                            mime_type="text/csv",
                            sha256=hashlib.sha256(content).hexdigest(),
                            locator={"kind": "sample_window", "value": "all"},
                            quality="verified",
                            producer="portable-lab",
                            producer_version="1",
                        )
                    ],
                ),
                MetricFetcher(content),
            )

        await ingest_revision(1, b"frame_ms\n10\n12\n")
        first = evaluator.evaluate(investigation.investigation_id)
        assert first is not None and first.verdict == "passed"

        await ingest_revision(2, b"frame_ms\n20\n22\n")
        corrected = evaluator.evaluate(investigation.investigation_id)
        assert corrected is not None and corrected.verdict == "failed"
        assert corrected.evaluated_state_revision > first.evaluated_state_revision
        assert len(corrected.contradicting_fact_ids) == 1
        assert repository.latest_assessment(investigation.investigation_id) == corrected
        assert len(repository.list_current_evidence(investigation.investigation_id)) == 1
        assert len(repository.list_current_facts(investigation.investigation_id)) == 1

    asyncio.run(scenario())
