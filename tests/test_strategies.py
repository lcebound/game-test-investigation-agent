from __future__ import annotations

from game_test_agent.planning import strategy_for


def test_memory_strategy_preserves_evidence_boundary():
    strategy = strategy_for("memory_growth")
    assert strategy is not None
    assert strategy.version == "1.0.0"
    assert any("RSS" in item for item in strategy.forbidden_conclusions)
    assert "引用链" in strategy.valuable_investigations
    assert strategy.experiment_templates[0].capability_operation == "experiment"
    assert strategy.experiment_templates[0].required_evidence_types == [
        "memory_series",
        "lifecycle_events",
    ]
