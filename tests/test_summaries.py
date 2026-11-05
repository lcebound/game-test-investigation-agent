from __future__ import annotations

import pytest

from game_test_agent.capabilities import CapabilityRegistry
from game_test_agent.contracts.models import (
    InvestigationRequest,
    TestObjective as Objective,
)
from game_test_agent.evidence import ContextBuilder
from game_test_agent.memory import InvestigationSummaryService
from game_test_agent.runtime import InvestigationKernel


def test_context_summary_is_source_addressed_replayable_and_invalidated(repository):
    other, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective=Objective(objective_id="other", description="占用较早事件游标"),
        ),
        "context-summary-other",
    )
    repository.record_investigation_event(other.investigation_id, "test.other", {})
    investigation, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective=Objective(objective_id="summary", description="验证可回查摘要"),
        ),
        "context-summary",
    )
    service = InvestigationSummaryService(repository)
    first = service.refresh(investigation.investigation_id)
    assert first.source_event_cursor_start > 1
    assert service.refresh(investigation.investigation_id) == first
    assert service.current(investigation.investigation_id) == first
    assert len(repository.list_context_summaries(investigation.investigation_id)) == 1

    repository.record_investigation_event(
        investigation.investigation_id,
        "test.source.changed",
        {"reason": "new authoritative input"},
    )
    assert service.current(investigation.investigation_id) is None
    second = service.refresh(investigation.investigation_id)
    assert second.summary_id != first.summary_id
    assert second.source_event_cursor_end > first.source_event_cursor_end
    assert repository.list_context_summaries(investigation.investigation_id) == [first, second]

    with pytest.raises(ValueError, match="hash does not match"):
        repository.save_context_summary(
            second.model_copy(update={"summary_text": "试图改写不可变摘要"})
        )


def test_kernel_attaches_current_summary_to_frozen_context(repository):
    investigation, _ = repository.create_investigation(
        InvestigationRequest(
            tenant_id="studio-a",
            project_id="game-a",
            objective=Objective(objective_id="summary-kernel", description="冻结摘要来源"),
        ),
        "summary-kernel",
    )
    registry = CapabilityRegistry(repository)
    context = ContextBuilder(repository, registry).build(
        investigation.investigation_id,
        scopes=set(),
    )
    prepared = InvestigationKernel(repository).prepare_round(
        context,
        plan_revision=1,
        model_name="test-model",
        prompt_version="investigator-v1",
    )
    assert prepared.snapshot is not None
    summary = prepared.snapshot.context.summary
    assert summary is not None
    assert summary.source_state_revision == investigation.revision
    assert (
        summary.summary_id
        == repository.get_latest_context_summary(investigation.investigation_id).summary_id
    )
    assert summary.summary_text.splitlines()[1] == "目标：冻结摘要来源"
