from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from game_test_agent.storage.mysql_repository import (
    MappingRow,
    MySqlConnectionAdapter,
    translate_sqlite_sql,
)


class DriverIntegrityError(Exception):
    pass


class FakeCursor:
    def __init__(self, connection) -> None:
        self.connection = connection
        self.description = None
        self.closed = False
        self.rows = []

    def execute(self, sql, parameters):
        self.connection.calls.append((sql, parameters))
        if self.connection.raise_integrity:
            raise DriverIntegrityError("duplicate")
        if sql.lstrip().upper().startswith("SELECT"):
            self.description = (("value",),)
            self.rows = [{"value": 1}]

    def fetchall(self):
        return self.rows

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self) -> None:
        self.calls = []
        self.raise_integrity = False
        self.last_cursor = None

    def cursor(self):
        self.last_cursor = FakeCursor(self)
        return self.last_cursor


def test_sql_translation_preserves_literals_and_maps_upserts():
    translated = translate_sqlite_sql("SELECT '?' AS literal FROM capabilities WHERE provider_id=?")
    assert translated == ("SELECT '?' AS literal FROM capability_snapshots WHERE provider_id=%s")

    capability_upsert = translate_sqlite_sql(
        """INSERT INTO capabilities VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(provider_id, capability_id, capability_version) DO UPDATE SET
                       descriptor_json=excluded.descriptor_json,
                       descriptor_hash=excluded.descriptor_hash,
                       validated_at=excluded.validated_at"""
    )
    assert "ON DUPLICATE KEY UPDATE" in capability_upsert
    assert "VALUES(descriptor_json)" in capability_upsert
    assert "ON CONFLICT" not in capability_upsert
    cleanup_upsert = translate_sqlite_sql(
        """INSERT INTO cleanup_operations(
                     action_id,investigation_id,state,attempts,last_error,updated_at
                   ) VALUES (?,?,'pending',0,NULL,?)
                   ON CONFLICT(action_id) DO UPDATE SET
                     state=CASE WHEN cleanup_operations.state='resolved' THEN 'resolved' ELSE 'pending' END,
                     updated_at=excluded.updated_at"""
    )
    assert "ON DUPLICATE KEY UPDATE" in cleanup_upsert
    assert "updated_at=VALUES(updated_at)" in cleanup_upsert


def test_cursor_identifier_is_quoted_without_changing_literals_or_comments():
    sql = "SELECT cursor, 'cursor', `cursor`, \"cursor\" FROM investigation_events -- cursor\nWHERE cursor>?"
    result = translate_sqlite_sql(sql)
    assert result == "SELECT `cursor`, 'cursor', `cursor`, \"cursor\" FROM investigation_events -- cursor\nWHERE `cursor`>%s"


def test_mysql_timestamp_rows_follow_utc_repository_protocol():
    from datetime import timezone
    row = MappingRow({"created_at": datetime(2026, 10, 31, 12, 30)})
    assert row["created_at"] == "2026-10-31T12:30:00+00:00"
    assert datetime.fromisoformat(row[0]).timestamp() == datetime(2026, 10, 31, 12, 30, tzinfo=timezone.utc).timestamp()


def test_mysql_adapter_buffers_rows_normalizes_timestamps_and_closes_cursor():
    connection = FakeConnection()
    adapter = MySqlConnectionAdapter(connection, (DriverIntegrityError,))

    row = adapter.execute(
        "SELECT value FROM sample WHERE updated_at=?",
        ("2026-09-18T01:02:03+00:00",),
    ).fetchone()

    assert isinstance(row, MappingRow)
    assert row[0] == row["value"] == 1
    assert isinstance(connection.calls[0][1][0], datetime)
    assert connection.last_cursor.closed


def test_mysql_driver_integrity_error_maps_to_repository_conflict_path():
    connection = FakeConnection()
    connection.raise_integrity = True
    adapter = MySqlConnectionAdapter(connection, (DriverIntegrityError,))

    with pytest.raises(sqlite3.IntegrityError):
        adapter.execute("INSERT INTO sample VALUES (?)", ("duplicate",))
    assert connection.last_cursor.closed
