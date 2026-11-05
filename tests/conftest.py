from __future__ import annotations

from pathlib import Path

import pytest

from game_test_agent.config import Settings
from game_test_agent.storage import Repository


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_path=tmp_path / "agent.db",
        artifact_root=tmp_path / "artifacts",
        enable_model=False,
    )


@pytest.fixture
def repository(settings: Settings):
    repo = Repository(settings.database_path)
    yield repo
    repo.close()
