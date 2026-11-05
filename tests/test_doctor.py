from __future__ import annotations

import asyncio

from game_test_agent.doctor import run_checks


def test_doctor_validates_local_dependencies_without_secrets(settings):
    checks = asyncio.run(run_checks(settings))
    assert all(item.ok for item in checks)
    assert {item.name for item in checks} == {
        "database",
        "artifact_store",
        "mcp_configuration",
        "event_source_policies",
        "analysis_sandbox_configuration",
    }
