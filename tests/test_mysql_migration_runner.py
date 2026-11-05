from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from game_test_agent.storage.mysql_migrate import (
    Migration,
    MySqlMigrationRunner,
    load_migrations,
    split_mysql_statements,
)


class FakeCursor:
    def __init__(self, connection) -> None:
        self.connection = connection
        self.last_sql = ""
        self.closed = False

    def execute(self, sql, parameters=None):
        self.last_sql = sql
        self.connection.executed.append((sql, parameters))
        if sql.startswith("INSERT INTO schema_migrations"):
            version, checksum = parameters
            self.connection.applied[int(version)] = str(checksum)

    def fetchone(self):
        if "GET_LOCK" in self.last_sql:
            return (1,)
        return None

    def fetchall(self):
        if self.last_sql.startswith("SELECT version, checksum"):
            return sorted(self.connection.applied.items())
        return []

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, applied=None) -> None:
        self.applied = dict(applied or {})
        self.executed = []
        self.commits = 0
        self.rollbacks = 0
        self.latest_cursor = None

    def cursor(self):
        self.latest_cursor = FakeCursor(self)
        return self.latest_cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_mysql_statement_splitter_respects_strings_and_comments():
    sql = """
    -- a comment with ;
    CREATE TABLE sample(value VARCHAR(64));
    INSERT INTO sample VALUES ('a;b'), ('it''s safe');
    /* block ; comment */ SELECT `semi;column` FROM sample;
    """

    statements = split_mysql_statements(sql)

    assert len(statements) == 3
    assert "'a;b'" in statements[1]
    assert "`semi;column`" in statements[2]


def test_loads_versioned_migration_with_content_checksum(tmp_path):
    path = tmp_path / "001_initial.sql"
    path.write_text("CREATE TABLE example(id INT);\n", encoding="utf-8")

    migrations = load_migrations(tmp_path)

    assert migrations[0].version == 1
    assert migrations[0].checksum == hashlib.sha256(path.read_bytes()).hexdigest()


def test_runner_applies_once_and_checks_checksum():
    migration = Migration(1, "001_test.sql", "CREATE TABLE test(id INT);", "a" * 64)
    connection = FakeConnection()

    assert MySqlMigrationRunner(connection).apply([migration]) == [1]
    assert connection.commits == 1
    assert connection.applied == {1: "a" * 64}
    assert MySqlMigrationRunner(connection).apply([migration]) == []
    assert connection.latest_cursor.closed

    changed = migration.__class__(1, migration.name, migration.sql, "b" * 64)
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        MySqlMigrationRunner(connection).apply([changed])
    assert connection.rollbacks == 1


def test_repository_mysql_migration_is_managed_by_runner():
    root = Path(__file__).parents[1] / "deploy" / "mysql"
    migrations = load_migrations(root)

    assert [item.version for item in migrations] == list(range(1, 21))
    latest = split_mysql_statements(migrations[-3].sql)
    assert any("CREATE TABLE IF NOT EXISTS conversation_route_evaluations" in item for item in latest)
    assert len(split_mysql_statements(migrations[-2].sql)) == 2
    statements = split_mysql_statements(migrations[0].sql)
    assert len(statements) >= 20
    assert not any("INSERT IGNORE INTO schema_migrations" in item for item in statements)
    second = split_mysql_statements(migrations[1].sql)
    assert any("CREATE TABLE IF NOT EXISTS run_result_snapshots" in item for item in second)
    assert any("CREATE TABLE IF NOT EXISTS experiment_runs" in item for item in second)
    assert any("CREATE TABLE IF NOT EXISTS criterion_assessments" in item for item in second)
    third = split_mysql_statements(migrations[2].sql)
    assert any("CREATE TABLE IF NOT EXISTS context_snapshots" in item for item in third)
    assert any("CREATE TABLE IF NOT EXISTS decision_records" in item for item in third)
    fourth = split_mysql_statements(migrations[3].sql)
    assert any("CREATE TABLE IF NOT EXISTS planned_actions" in item for item in fourth)
    sixth = split_mysql_statements(migrations[5].sql)
    assert any("CREATE TABLE IF NOT EXISTS case_memory_records" in item for item in sixth)
    seventh = split_mysql_statements(migrations[6].sql)
    assert any("ALTER TABLE cleanup_operations" in item for item in seventh)
    assert any("CREATE TABLE IF NOT EXISTS analysis_jobs" in item for item in seventh)
    assert any("CREATE TABLE IF NOT EXISTS analysis_result_snapshots" in item for item in seventh)
    assert any("CREATE TABLE IF NOT EXISTS derived_fact_candidates" in item for item in seventh)


@pytest.mark.parametrize("mapping", [False, True])
def test_recovery_index_migration_resumes_after_partial_ddl(mapping):
    class RecoveryCursor(FakeCursor):
        def fetchall(self):
            if "information_schema.statistics" in self.last_sql:
                index = self.connection.executed[-1][1][0]
                if index == "ix_route_evaluation_deadline":
                    rows = [("state", None, 1, "BTREE"), ("lease_deadline", None, 1, "BTREE")]
                    if mapping:
                        return [dict(zip(("COLUMN_NAME", "SUB_PART", "NON_UNIQUE", "INDEX_TYPE"), row)) for row in rows]
                    return rows
            return super().fetchall()

    connection = FakeConnection()
    connection.cursor = lambda: RecoveryCursor(connection)
    migration = load_migrations(Path(__file__).parents[1] / "deploy/mysql")[-2]
    assert MySqlMigrationRunner(connection).apply([migration]) == [19]
    creates = [sql for sql, _ in connection.executed if sql.startswith("CREATE INDEX")]
    assert len(creates) == 1 and "ix_route_evaluation_retry" in creates[0]


def test_recovery_index_migration_rejects_wrong_existing_definition():
    from unittest.mock import Mock

    cursor = Mock()
    cursor.fetchall.return_value = [("state", 4, 1, "BTREE"), ("lease_deadline", None, 1, "BTREE")]
    with pytest.raises(RuntimeError, match="incompatible"):
        MySqlMigrationRunner._recovery_index_exists(cursor,
            "CREATE INDEX ix_route_evaluation_deadline ON conversation_route_evaluations(state,lease_deadline)")


def test_historical_mysql_compatibility_preserves_checksums_and_full_source_identity():
    from game_test_agent.storage.mysql_sql import migration_statement
    root = Path(__file__).parents[1] / "deploy/mysql"
    migrations = load_migrations(root)
    source = migrations[1]
    transformed = [migration_statement(source.name, sql) for sql in split_mysql_statements(source.sql)]
    statement = next(sql for sql in transformed if "uq_evidence_source" in sql)
    assert "source_identity_sha256" in statement and "JSON_ARRAY(" in statement
    assert "run_id,source_evidence_id,source_revision" in statement
    assert source.checksum == hashlib.sha256((root / source.name).read_bytes()).hexdigest()
    connection = FakeConnection({1: migrations[0].checksum})
    MySqlMigrationRunner(connection).apply([source])
    sqls = [sql for sql, _ in connection.executed]
    supporting = next(i for i, sql in enumerate(sqls) if sql.startswith("CREATE INDEX ix_evidence_fk"))
    dropping = next(i for i, sql in enumerate(sqls) if "DROP INDEX uq_evidence_content" in sql)
    assert supporting < dropping
