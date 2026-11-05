from __future__ import annotations

import pytest

from game_test_agent.contracts.models import InvestigationRequest, TestObjective as Objective
from game_test_agent.storage import ConflictError


def request(description: str = "验证帧耗时") -> InvestigationRequest:
    return InvestigationRequest(
        tenant_id="studio-a",
        project_id="game-a",
        objective=Objective(objective_id="perf", description=description),
    )


def test_intake_is_idempotent(repository):
    first, created = repository.create_investigation(request(), "request-1")
    second, repeated = repository.create_investigation(request(), "request-1")
    assert created is True
    assert repeated is False
    assert first.investigation_id == second.investigation_id


def test_idempotency_payload_conflict_is_rejected(repository):
    repository.create_investigation(request(), "request-1")
    with pytest.raises(ConflictError):
        repository.create_investigation(request("不同目标"), "request-1")
