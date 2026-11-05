from __future__ import annotations

import argparse
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from game_test_agent.storage.mysql_sql import migration_statement


_MIGRATION_NAME = re.compile(r"^(?P<version>[0-9]+)_[a-z0-9][a-z0-9_-]*\.sql$")


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str


def split_mysql_statements(sql: str) -> list[str]:
    """Split trusted migration SQL while respecting strings and SQL comments."""

    statements: list[str] = []
    current: list[str] = []
    state = "normal"
    index = 0
    while index < len(sql):
        character = sql[index]
        following = sql[index + 1] if index + 1 < len(sql) else ""
        if state == "normal":
            if character == "'":
                state = "single"
                current.append(character)
            elif character == '"':
                state = "double"
                current.append(character)
            elif character == "`":
                state = "backtick"
                current.append(character)
            elif character == "-" and following == "-":
                state = "line_comment"
                index += 1
            elif character == "#":
                state = "line_comment"
            elif character == "/" and following == "*":
                state = "block_comment"
                index += 1
            elif character == ";":
                statement = "".join(current).strip()
                if statement:
                    statements.append(statement)
                current = []
            else:
                current.append(character)
        elif state == "line_comment":
            if character in "\r\n":
                state = "normal"
                current.append("\n")
        elif state == "block_comment":
            if character == "*" and following == "/":
                state = "normal"
                index += 1
        else:
            current.append(character)
            if character == "\\" and following:
                current.append(following)
                index += 1
            elif state == "single" and character == "'":
                if following == "'":
                    current.append(following)
                    index += 1
                else:
                    state = "normal"
            elif state == "double" and character == '"':
                state = "normal"
            elif state == "backtick" and character == "`":
                state = "normal"
        index += 1
    if state not in {"normal", "line_comment"}:
        raise ValueError(f"unterminated SQL token in state {state}")
    trailing = "".join(current).strip()
    if trailing:
        statements.append(trailing)
    return statements


def load_migrations(directory: Path) -> list[Migration]:
    migrations: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = _MIGRATION_NAME.fullmatch(path.name)
        if not match:
            raise ValueError(f"invalid migration filename: {path.name}")
        raw = path.read_bytes()
        try:
            sql = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError(f"migration {path.name} must be UTF-8") from error
        version = int(match.group("version"))
        if version < 1:
            raise ValueError(f"migration version must be positive: {path.name}")
        migrations.append(
            Migration(
                version=version,
                name=path.name,
                sql=sql,
                checksum=hashlib.sha256(raw).hexdigest(),
            )
        )
    versions = [item.version for item in migrations]
    if not migrations:
        raise ValueError(f"no migrations found in {directory}")
    if len(versions) != len(set(versions)):
        raise ValueError("migration versions must be unique")
    if versions != sorted(versions):
        raise ValueError("migration versions must be ordered")
    return migrations


class MySqlMigrationRunner:
    LOCK_NAME = "game-test-agent-schema-migrations"

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def apply(self, migrations: Iterable[Migration]) -> list[int]:
        applied_now: list[int] = []
        cursor = self.connection.cursor()
        try:
            cursor.execute("SELECT GET_LOCK(%s, %s)", (self.LOCK_NAME, 30))
            lock_row = cursor.fetchone()
            lock_value = (
                next(iter(lock_row.values())) if isinstance(lock_row, dict) else lock_row[0]
            )
            if lock_value != 1:
                raise TimeoutError("could not acquire the MySQL migration lock")
            cursor.execute(
                """CREATE TABLE IF NOT EXISTS schema_migrations (
                     version INT PRIMARY KEY,
                     checksum CHAR(64) NOT NULL,
                     applied_at TIMESTAMP(6) NOT NULL
                   ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin"""
            )
            cursor.execute("SELECT version, checksum FROM schema_migrations ORDER BY version")
            rows = cursor.fetchall()
            applied = {
                int(row["version"] if isinstance(row, dict) else row[0]): str(
                    row["checksum"] if isinstance(row, dict) else row[1]
                )
                for row in rows
            }
            for migration in migrations:
                existing = applied.get(migration.version)
                if existing is not None:
                    if existing != migration.checksum:
                        raise RuntimeError(
                            f"checksum mismatch for applied migration {migration.version}"
                        )
                    continue
                if migration.name == "002_trusted_execution_and_observations.sql":
                    # Migration 001's content index also backs the investigation FK.
                    # Keep a supporting index before 002 drops that unique index.
                    # Do not rewrite a historical file or disable FK enforcement.
                    cursor.execute("""SELECT INDEX_NAME FROM information_schema.statistics
                        WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='evidence_records'
                        AND INDEX_NAME='ix_evidence_fk_investigation'""")
                    if cursor.fetchone() is None:
                        cursor.execute("CREATE INDEX ix_evidence_fk_investigation ON evidence_records(investigation_id)")
                for statement in split_mysql_statements(migration.sql):
                    if migration.name == "019_route_recovery_indexes.sql" and self._recovery_index_exists(cursor, statement):
                        continue
                    cursor.execute(migration_statement(migration.name, statement))
                cursor.execute(
                    "INSERT INTO schema_migrations(version, checksum, applied_at) "
                    "VALUES (%s, %s, UTC_TIMESTAMP(6))",
                    (migration.version, migration.checksum),
                )
                self.connection.commit()
                applied_now.append(migration.version)
            return applied_now
        except Exception:
            self.connection.rollback()
            raise
        finally:
            try:
                cursor.execute("SELECT RELEASE_LOCK(%s)", (self.LOCK_NAME,))
            finally:
                cursor.close()

    @staticmethod
    def _recovery_index_exists(cursor, statement):
        # MySQL DDL commits independently. A restart after the first CREATE INDEX
        # must validate and reuse that index before finishing migration 019.
        match = re.fullmatch(r"CREATE INDEX (ix_route_evaluation_\w+) ON conversation_route_evaluations\(([^)]+)\)", statement)
        if match is None:
            raise ValueError("unexpected recovery index migration statement")
        expected = [column.strip() for column in match[2].split(",")]
        cursor.execute("""SELECT COLUMN_NAME,SUB_PART,NON_UNIQUE,INDEX_TYPE FROM information_schema.statistics
            WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='conversation_route_evaluations'
            AND INDEX_NAME=%s ORDER BY SEQ_IN_INDEX""", (match[1],))
        rows = cursor.fetchall()
        if not rows:
            return False
        normalized = [tuple(row[key] for key in ("COLUMN_NAME", "SUB_PART", "NON_UNIQUE", "INDEX_TYPE"))
                      if isinstance(row, dict) else tuple(row) for row in rows]
        if [row[0] for row in normalized] != expected or any(
            row[1] is not None or row[2] != 1 or row[3] != "BTREE" for row in normalized):
            raise RuntimeError("existing recovery index has an incompatible definition")
        return True


def connect_from_env() -> Any:
    try:
        import pymysql
    except ImportError as error:  # pragma: no cover - optional dependency
        raise RuntimeError("install the 'mysql' optional dependency to run migrations") from error
    host = os.getenv("GTA_MYSQL_HOST")
    database = os.getenv("GTA_MYSQL_DATABASE")
    user = os.getenv("GTA_MYSQL_USER")
    password = os.getenv("GTA_MYSQL_PASSWORD")
    missing = [
        name
        for name, value in {
            "GTA_MYSQL_HOST": host,
            "GTA_MYSQL_DATABASE": database,
            "GTA_MYSQL_USER": user,
            "GTA_MYSQL_PASSWORD": password,
        }.items()
        if not value
    ]
    if missing:
        raise ValueError("missing MySQL configuration: " + ", ".join(missing))
    ssl_ca = os.getenv("GTA_MYSQL_SSL_CA")
    if host not in {"127.0.0.1", "localhost", "::1"} and not ssl_ca:
        raise ValueError("GTA_MYSQL_SSL_CA is required for non-local MySQL")
    return pymysql.connect(
        host=host,
        port=int(os.getenv("GTA_MYSQL_PORT", "3306")),
        user=user,
        password=password,
        init_command="SET time_zone = '+00:00'",
        database=database,
        charset="utf8mb4",
        autocommit=False,
        connect_timeout=10,
        read_timeout=30,
        write_timeout=30,
        ssl={"ca": ssl_ca} if ssl_ca else None,
    )


def default_migration_directory() -> Path:
    packaged = Path(__file__).with_name("mysql_migrations")
    if packaged.is_dir():
        return packaged
    source_checkout = Path.cwd() / "deploy" / "mysql"
    if source_checkout.is_dir():
        return source_checkout
    raise FileNotFoundError(
        "no packaged migrations found; pass --directory with the migration location"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply Game Test Agent MySQL migrations")
    parser.add_argument(
        "--directory",
        type=Path,
        help="Migration directory (defaults to packaged migrations or deploy/mysql)",
    )
    args = parser.parse_args()
    directory = args.directory.resolve() if args.directory else default_migration_directory()
    migrations = load_migrations(directory)
    connection = connect_from_env()
    try:
        applied = MySqlMigrationRunner(connection).apply(migrations)
    finally:
        connection.close()
    print("Applied migrations: " + (", ".join(map(str, applied)) if applied else "none"))


if __name__ == "__main__":
    main()
