from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from game_test_agent.contracts.models import Budget


@dataclass(frozen=True, slots=True)
class StopContext:
    started_at: datetime
    now: datetime
    canceled: bool = False
    goal_met: bool = False
    reasoning_rounds: int = 0
    real_runs: int = 0
    tool_reads: int = 0
    device_minutes: float = 0
    consecutive_rounds_without_evidence: int = 0
    valuable_actions_available: bool = True
    required_capability_available: bool = True


@dataclass(frozen=True, slots=True)
class StopDecision:
    should_stop: bool
    reason: str | None = None


class StopPolicy:
    def __init__(self, no_progress_rounds: int = 2) -> None:
        self.no_progress_rounds = no_progress_rounds

    def evaluate(self, context: StopContext, budget: Budget) -> StopDecision:
        if context.canceled:
            return StopDecision(True, "user_canceled")
        elapsed = (context.now - context.started_at).total_seconds()
        if elapsed >= budget.max_wall_seconds:
            return StopDecision(True, "wall_clock_budget_exhausted")
        if context.goal_met:
            return StopDecision(True, "goal_met")
        if context.reasoning_rounds >= budget.max_rounds:
            return StopDecision(True, "reasoning_round_budget_exhausted")
        if not context.required_capability_available:
            return StopDecision(True, "required_capability_unavailable")
        if not context.valuable_actions_available:
            return StopDecision(True, "no_valuable_action")
        if context.consecutive_rounds_without_evidence >= self.no_progress_rounds:
            return StopDecision(True, "no_new_evidence")
        return StopDecision(False)


def utc_stop_context(started_at: datetime, **kwargs) -> StopContext:
    return StopContext(started_at=started_at, now=datetime.now(timezone.utc), **kwargs)
