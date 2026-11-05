from __future__ import annotations

import sqlite3

from game_test_agent.contracts.models import InvestigationRequest, TestObjective as Objective
from game_test_agent.storage import Repository


def test_version_three_database_upgrades_outbox_delivery_columns(tmp_path):
    path = tmp_path / "legacy-v3.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
        INSERT INTO schema_migrations VALUES (1, 'old'), (2, 'old'), (3, 'old');
        CREATE TABLE workflow_start_outbox (
          investigation_id TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'pending',
          attempts INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
        );
        CREATE TABLE workflow_command_outbox (
          command_id TEXT PRIMARY KEY, investigation_id TEXT NOT NULL, kind TEXT NOT NULL,
          payload_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL
        );
        CREATE TABLE event_outbox (
          event_id TEXT PRIMARY KEY, investigation_id TEXT NOT NULL, event_type TEXT NOT NULL,
          payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL
        );
        """
    )
    connection.close()

    repository = Repository(path)
    try:
        assert repository.schema_version() == 24
        investigation, _ = repository.create_investigation(
            InvestigationRequest(
                tenant_id="studio-a",
                project_id="game-a",
                objective=Objective(objective_id="upgrade", description="验证升级"),
            ),
            "upgrade-v3-v4",
        )
        assert repository.enqueue_command(
            investigation.investigation_id, "cmd-upgrade", "input", {"content": "ok"}
        )
        assert repository.pending_commands()[0]["attempts"] == 0
    finally:
        repository.close()


def test_version_five_run_results_are_backfilled_with_tenant_scope(tmp_path):
    path = tmp_path / "legacy-v5.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
        INSERT INTO schema_migrations VALUES
          (1, 'old'), (2, 'old'), (3, 'old'), (4, 'old'), (5, 'old');
        CREATE TABLE investigations (
          investigation_id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL
        );
        INSERT INTO investigations VALUES ('inv-old', 'tenant-old');
        CREATE TABLE run_result_snapshots (
          provider_id TEXT NOT NULL, project_id TEXT NOT NULL, run_id TEXT NOT NULL,
          result_revision INTEGER NOT NULL, action_id TEXT NOT NULL,
          investigation_id TEXT NOT NULL, result_json TEXT NOT NULL,
          result_hash TEXT NOT NULL, ingestion_state TEXT NOT NULL,
          ingestion_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          PRIMARY KEY(provider_id, project_id, run_id, result_revision),
          UNIQUE(action_id, result_revision)
        );
        INSERT INTO run_result_snapshots VALUES (
          'provider', 'project', 'run', 1, 'action', 'inv-old', '{}',
          'hash', 'pending', NULL, 'old', 'old'
        );
        """
    )
    connection.close()

    repository = Repository(path)
    try:
        row = repository._connection.execute(
            "SELECT tenant_id FROM run_result_snapshots WHERE action_id='action'"
        ).fetchone()
        assert repository.schema_version() == 24
        assert row["tenant_id"] == "tenant-old"
    finally:
        repository.close()
