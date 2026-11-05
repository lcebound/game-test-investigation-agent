"""Run harness ownership/recovery checks in the existing disposable CI stack."""

import asyncio
import importlib.util
import json
from pathlib import Path


def validation_module():
    path = Path(__file__).resolve().parents[2] / "scripts/validate_harness_services.py"
    spec = importlib.util.spec_from_file_location("harness_services_validation", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_mysql_harness_ownership_and_observation_recovery(
    live_repository, dependency_env, tmp_path
):
    config = tmp_path / "mysql.json"
    config.write_text(
        json.dumps(
            {
                "mysql_database": "gta_dependency_checks",
                "mysql_port": int(dependency_env["GTA_TEST_MYSQL_PORT"]),
                "mysql_password": dependency_env["GTA_TEST_MYSQL_PASSWORD"],
            }
        ),
        encoding="utf-8",
    )
    try:
        result = asyncio.run(validation_module().mysql_check(config))
        assert result["passed"] and "feedback_retry_idempotence" in result["scenarios"]
    finally:
        config.unlink(missing_ok=True)


def test_temporal_harness_restart_cancel_and_replay(dependency_env, tmp_path):
    result = asyncio.run(
        validation_module().temporal_check(
            dependency_env["GTA_TEST_TEMPORAL_ADDRESS"], tmp_path / "temporal"
        )
    )
    assert result["worker_restart_recovered"] and result["cancel_replayed"]
