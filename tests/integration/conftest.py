"""Fixtures only connect to the explicitly enabled, disposable dependency stack."""
import os
from pathlib import Path

import pytest


@pytest.fixture
def dependency_env():
    if os.getenv("GTA_DEPENDENCY_INTEGRATION") != "1":
        pytest.skip("real dependencies: run scripts/run_dependency_checks.py")
    required = ["GTA_TEST_MYSQL_PORT", "GTA_TEST_MYSQL_PASSWORD", "GTA_TEST_S3_ENDPOINT",
                "GTA_TEST_S3_PASSWORD", "GTA_TEST_TEMPORAL_ADDRESS"]
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        pytest.fail("missing isolated dependency settings: " + ", ".join(missing))
    return {key: os.environ[key] for key in required}


@pytest.fixture
def live_repository(dependency_env):
    import pymysql
    from game_test_agent.storage.mysql_migrate import MySqlMigrationRunner, load_migrations
    from game_test_agent.storage.mysql_repository import connect_mysql_repository
    arguments = dict(host="127.0.0.1", port=int(dependency_env["GTA_TEST_MYSQL_PORT"]),
        database="gta_dependency_checks", user="root", password=dependency_env["GTA_TEST_MYSQL_PASSWORD"])
    connection = pymysql.connect(**arguments, cursorclass=pymysql.cursors.DictCursor)
    try:
        migrations = load_migrations(Path(__file__).resolve().parents[2] / "deploy/mysql")
        runner = MySqlMigrationRunner(connection)
        runner.apply(migrations)
        assert runner.apply(migrations) == []
    finally:
        connection.close()
    def connect():
        return connect_mysql_repository(**arguments, ssl_ca=None)
    repo = connect()
    try:
        yield repo, connect
    finally:
        repo.close()


@pytest.fixture
def live_artifacts(dependency_env):
    import boto3
    from botocore.config import Config
    from game_test_agent.storage import S3ArtifactStore
    client = boto3.client("s3", endpoint_url=dependency_env["GTA_TEST_S3_ENDPOINT"],
        region_name="us-east-1", aws_access_key_id="gta-integration",
        aws_secret_access_key=dependency_env["GTA_TEST_S3_PASSWORD"],
        config=Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 2},
                      proxies={}))
    bucket = "gta-dependency-checks"
    if not any(b["Name"] == bucket for b in client.list_buckets()["Buckets"]):
        client.create_bucket(Bucket=bucket)
    yield S3ArtifactStore(bucket, client=client), client
    client.close()
