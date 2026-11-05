from __future__ import annotations

from datetime import datetime, timedelta, timezone

from game_test_agent.contracts.models import Budget
from game_test_agent.policies import StopContext, StopPolicy


def test_hard_budget_stop_precedes_soft_goal_state():
    now = datetime.now(timezone.utc)
    decision = StopPolicy().evaluate(
        StopContext(
            started_at=now - timedelta(seconds=61),
            now=now,
            goal_met=True,
        ),
        Budget(max_wall_seconds=60),
    )
    assert decision.should_stop
    assert decision.reason == "wall_clock_budget_exhausted"


def test_no_progress_stops_bounded_loop():
    now = datetime.now(timezone.utc)
    decision = StopPolicy(no_progress_rounds=2).evaluate(
        StopContext(
            started_at=now,
            now=now,
            consecutive_rounds_without_evidence=2,
        ),
        Budget(),
    )
    assert decision.should_stop
    assert decision.reason == "no_new_evidence"


def test_completed_goal_precedes_non_wall_clock_budgets():
    now = datetime.now(timezone.utc)
    decision = StopPolicy().evaluate(
        StopContext(
            started_at=now,
            now=now,
            goal_met=True,
            reasoning_rounds=4,
            real_runs=3,
        ),
        Budget(max_rounds=4, max_real_runs=3),
    )
    assert decision.should_stop
    assert decision.reason == "goal_met"


def test_experiment_budget_does_not_block_read_or_report_loop():
    now = datetime.now(timezone.utc)
    decision = StopPolicy().evaluate(
        StopContext(
            started_at=now,
            now=now,
            real_runs=3,
            tool_reads=40,
            device_minutes=20,
        ),
        Budget(max_real_runs=3, max_tool_reads=40, max_device_minutes=20),
    )
    assert not decision.should_stop
