from __future__ import annotations

import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from game_test_agent.storage.repository import Repository
from game_test_agent.storage.mysql_sql import quote_mysql_identifiers


_ISO_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")


class MappingRow(dict[str, Any]):
    def __init__(self, values: dict[str, Any]) -> None:
        # MySQL TIMESTAMP values arrive as naive UTC datetimes. The repository
        # protocol uses ISO strings like SQLite; never interpret them as host time.
        super().__init__({key: (value.replace(tzinfo=value.tzinfo or timezone.utc).isoformat()
                               if isinstance(value, datetime) else value)
                          for key, value in values.items()})
        self._ordered_values = tuple(self.values())

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            return self._ordered_values[key]
        return super().__getitem__(key)


def translate_sqlite_sql(sql: str) -> str:
    translated = sql.replace("INSERT OR IGNORE", "INSERT IGNORE")
    translated = re.sub(r"\bcapabilities\b", "capability_snapshots", translated)
    translated = translated.replace(
        """ON CONFLICT(provider_id, capability_id, capability_version) DO UPDATE SET
                       descriptor_json=excluded.descriptor_json,
                       descriptor_hash=excluded.descriptor_hash,
                       validated_at=excluded.validated_at""",
        """ON DUPLICATE KEY UPDATE
                       descriptor_json=VALUES(descriptor_json),
                       descriptor_hash=VALUES(descriptor_hash),
                       validated_at=VALUES(validated_at)""",
    )
    translated = translated.replace(
        """ON CONFLICT(action_id) DO UPDATE SET
                     state=CASE WHEN cleanup_operations.state='resolved' THEN 'resolved' ELSE 'pending' END,
                     updated_at=excluded.updated_at""",
        """ON DUPLICATE KEY UPDATE
                     state=CASE WHEN cleanup_operations.state='resolved' THEN 'resolved' ELSE 'pending' END,
                     updated_at=VALUES(updated_at)""",
    )
    output: list[str] = []
    state = "normal"
    index = 0
    while index < len(translated):
        character = translated[index]
        following = translated[index + 1] if index + 1 < len(translated) else ""
        if state == "normal":
            if character == "'":
                state = "single"
            elif character == '"':
                state = "double"
            elif character == "`":
                state = "backtick"
            if character == "?":
                output.append("%s")
            else:
                output.append(character)
        else:
            output.append(character)
            if character == "\\" and following:
                output.append(following)
                index += 1
            elif state == "single" and character == "'":
                if following == "'":
                    output.append(following)
                    index += 1
                else:
                    state = "normal"
            elif state == "double" and character == '"':
                state = "normal"
            elif state == "backtick" and character == "`":
                state = "normal"
        index += 1
    return quote_mysql_identifiers("".join(output))


def _mysql_parameter(value: Any) -> Any:
    if isinstance(value, str) and _ISO_DATETIME.fullmatch(value):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    return value


class MySqlCursorAdapter:
    def __init__(self, cursor: Any, integrity_errors: tuple[type[BaseException], ...]) -> None:
        self.cursor = cursor
        self.integrity_errors = integrity_errors
        self._rows: list[dict[str, Any]] = []
        self._offset = 0

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> "MySqlCursorAdapter":
        try:
            self.cursor.execute(
                translate_sqlite_sql(sql), tuple(_mysql_parameter(item) for item in parameters)
            )
            if self.cursor.description:
                self._rows = list(self.cursor.fetchall())
        except self.integrity_errors as error:
            raise sqlite3.IntegrityError(str(error)) from error
        finally:
            self.cursor.close()
        return self

    def fetchone(self) -> MappingRow | None:
        if self._offset >= len(self._rows):
            return None
        row = self._rows[self._offset]
        self._offset += 1
        return MappingRow(row if isinstance(row, dict) else dict(row))

    @property
    def rowcount(self) -> int:
        return self.cursor.rowcount

    def fetchall(self) -> list[MappingRow]:
        rows = self._rows[self._offset :]
        self._offset = len(self._rows)
        return [MappingRow(row if isinstance(row, dict) else dict(row)) for row in rows]


class MySqlConnectionAdapter:
    def __init__(self, connection: Any, integrity_errors: tuple[type[BaseException], ...]) -> None:
        self.connection = connection
        self.integrity_errors = integrity_errors

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> MySqlCursorAdapter:
        return MySqlCursorAdapter(self.connection.cursor(), self.integrity_errors).execute(
            sql, parameters
        )

    def begin(self) -> None:
        self.connection.begin()

    def commit(self) -> None:
        self.connection.commit()

    def rollback(self) -> None:
        self.connection.rollback()

    def close(self) -> None:
        self.connection.close()


class MySqlRepository(Repository):
    """Repository implementation reusing domain transactions over a MySQL DB-API adapter."""

    def __init__(
        self,
        connection: Any,
        *,
        integrity_errors: tuple[type[BaseException], ...] = (),
    ) -> None:
        self.path = Path("mysql://game-test-agent")
        self._lock = threading.RLock()
        self._connection = MySqlConnectionAdapter(connection, integrity_errors)
        if not self.ping():
            raise ConnectionError("MySQL repository ping failed")
        if self.schema_version() < 20:
            raise RuntimeError("MySQL schema requires migration 020 or newer")

    def _lock_investigation_row(self, connection, investigation_id: str) -> None:
        from game_test_agent.storage.repository import NotFoundError

        if not connection.execute("SELECT 1 FROM investigations WHERE investigation_id=? FOR UPDATE",
                                  (investigation_id,)).fetchone():
            raise NotFoundError(investigation_id)

    @contextmanager
    def transaction(self) -> Iterator[MySqlConnectionAdapter]:
        with self._lock:
            from game_test_agent.runtime.worker_ownership import validate_owned_write
            validate_owned_write(self)
            self._connection.begin()
            try:
                yield self._connection
            except Exception:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()


def connect_mysql_repository(
    *,
    host: str,
    port: int,
    database: str,
    user: str,
    password: str,
    ssl_ca: str | None,
) -> MySqlRepository:
    try:
        import pymysql
    except ImportError as error:  # pragma: no cover - optional dependency
        raise RuntimeError("install the 'mysql' optional dependency for MySQL runtime") from error
    if host not in {"127.0.0.1", "localhost", "::1"} and not ssl_ca:
        raise ValueError("a CA file is required for non-local MySQL")
    connection = pymysql.connect(
        host=host,
        port=port,
        database=database,
        user=user,
        password=password,
        charset="utf8mb4",
        # All writes use Repository.transaction(). Outside it, a SELECT must not
        # retain SERIALIZABLE read locks or a stale snapshot across activities.
        autocommit=True,
        connect_timeout=10,
        read_timeout=30,
        write_timeout=30,
        ssl={"ca": ssl_ca} if ssl_ca else None,
        cursorclass=pymysql.cursors.DictCursor,
        init_command="SET SESSION TRANSACTION ISOLATION LEVEL SERIALIZABLE",
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET time_zone = '+00:00'")
    except Exception:
        connection.close()
        raise
    return MySqlRepository(connection, integrity_errors=(pymysql.err.IntegrityError,))
