from __future__ import annotations

from game_test_agent.config import Settings
from game_test_agent.storage.mysql_repository import connect_mysql_repository
from game_test_agent.storage.repository import Repository


def create_repository(settings: Settings) -> Repository:
    if settings.database_backend == "sqlite":
        return Repository(settings.database_path)
    if settings.database_backend == "mysql":
        return connect_mysql_repository(
            host=settings.mysql_host,
            port=settings.mysql_port,
            database=settings.mysql_database,
            user=settings.mysql_user,
            password=settings.mysql_password,
            ssl_ca=settings.mysql_ssl_ca,
        )
    raise ValueError(f"unsupported database backend: {settings.database_backend}")
