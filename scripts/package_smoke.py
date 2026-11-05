"""Inspect and import the built wheel without editable paths or external services."""
from __future__ import annotations

import os
import argparse
import subprocess
import sys
import tempfile
from pathlib import Path
from zipfile import ZipFile


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", type=Path, default=root / "dist")
    args = parser.parse_args()
    wheels = sorted(args.dist.glob("game_test_agent-*.whl"), key=lambda p: p.stat().st_mtime)
    if not wheels:
        raise SystemExit("Build a wheel before running the package smoke check.")
    with tempfile.TemporaryDirectory(prefix="gta-wheel-") as folder:
        with ZipFile(wheels[-1]) as wheel:
            names = set(wheel.namelist())
            required = {"game_test_agent/api/static/workbench-supervisor.js",
                        "game_test_agent/api/static/workbench-supervisor.css",
                        "game_test_agent/api/static/workbench-harness.js", "game_test_agent/storage/mysql_migrations/020_harness_runtime.sql",
                        "game_test_agent/workflows/native_operations.py",
                        "game_test_agent/events/observation_router.py",
                        "game_test_agent/memory/synthesis.py",
                        "game_test_agent/evals/harness_live.py",
                        "game_test_agent/api/static/index.html",
                        "game_test_agent/contracts/v2/NextStep.json",
                        "game_test_agent/storage/mysql_migrations/013_code_permissions.sql",
                        "game_test_agent/storage/mysql_migrations/014_conversation_task_links.sql",
                        "game_test_agent/storage/mysql_migrations/015_task_feedback.sql",
                        "game_test_agent/storage/mysql_migrations/016_investigation_recall_links.sql",
                        "game_test_agent/storage/mysql_migrations/017_task_input_returns.sql",
                        "game_test_agent/api/static/workbench-recall.js",
                        "game_test_agent/api/static/workbench-overview.js",
                        "game_test_agent/api/static/workbench-routing.js",
                        "game_test_agent/api/static/workbench-returns.js",
                        "game_test_agent/events/input_returns.py",
                        "game_test_agent/events/message_router.py",
                        "game_test_agent/events/coordinator.py"}
            if not required <= names:
                raise SystemExit("Wheel is missing required runtime assets.")
            wheel.extractall(folder)
        env = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}}
        code = "import sys; sys.path.insert(0, sys.argv[1]); from game_test_agent.api import create_app; from game_test_agent.workflows.native_mcp import NativeMcpController; print('Wheel imports and assets passed.')"
        subprocess.run([sys.executable, "-I", "-c", code, folder], cwd=folder, env=env, check=True)


if __name__ == "__main__":
    main()
