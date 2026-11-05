from __future__ import annotations

from game_test_agent.contracts.models import ProjectionCheckpoint
from game_test_agent.memory import InvestigationSummaryService
from game_test_agent.objectives import ObjectiveEvaluator
from game_test_agent.provenance.graph import ProvenanceIndex
from game_test_agent.storage import ConflictError, Repository, payload_hash


class ProjectionLagError(ConflictError):
    pass


class ProjectionCoordinator:
    """Synchronously rebuilds critical read projections and records their ledger cursor."""

    REQUIRED = ("objective", "summary", "provenance")

    def __init__(self, repository: Repository) -> None:
        self.repository = repository
        self.objectives = ObjectiveEvaluator(repository)
        self.summaries = InvestigationSummaryService(repository)
        self.provenance = ProvenanceIndex(repository)

    def refresh(self, investigation_id: str) -> list[ProjectionCheckpoint]:
        # Derived writes may themselves advance the cursor on the first pass.
        # Only certify a pass during which the entire input ledger stayed still.
        for _ in range(3):
            cursor = self.repository.latest_event_cursor(investigation_id)
            assessment = self.objectives.evaluate(investigation_id)
            summary = self.summaries.refresh(investigation_id)
            graph = self.provenance.build(investigation_id)
            if self.repository.latest_event_cursor(investigation_id) == cursor:
                break
        else:
            raise ProjectionLagError("ledger changed while rebuilding critical projections")
        materials = {
            "objective": assessment.model_dump(mode="json") if assessment else {"empty": True},
            "summary": summary.model_dump(mode="json"),
            "provenance": {
                "nodes": {
                    key: value.model_dump(mode="json") for key, value in sorted(graph.nodes.items())
                },
                "edges": [item.model_dump(mode="json") for item in graph.edges],
            },
        }
        checkpoints = [
            self.repository.save_projection_checkpoint(
                ProjectionCheckpoint(
                    investigation_id=investigation_id,
                    projection_name=name,
                    processed_cursor=cursor,
                    projection_hash=payload_hash(materials[name]),
                )
            )
            for name in self.REQUIRED
        ]
        return checkpoints

    def assert_fresh(self, investigation_id: str, *, required_cursor: int | None = None) -> None:
        target = (
            self.repository.latest_event_cursor(investigation_id)
            if required_cursor is None
            else required_cursor
        )
        checkpoints = {
            item.projection_name: item
            for item in self.repository.list_projection_checkpoints(investigation_id)
        }
        stale = [
            name
            for name in self.REQUIRED
            if name not in checkpoints or checkpoints[name].processed_cursor < target
        ]
        if stale:
            raise ProjectionLagError(
                f"critical projections lag cursor {target}: {', '.join(stale)}"
            )
