from __future__ import annotations

from pathlib import Path


def test_mysql_migration_contains_domain_and_reliability_tables():
    sql = (Path(__file__).parents[1] / "deploy" / "mysql" / "001_initial.sql").read_text(
        encoding="utf-8"
    )
    required = {
        "investigations",
        "investigation_authorizations",
        "investigation_actions",
        "budget_reservations",
        "reasoning_steps",
        "evidence_records",
        "hypotheses",
        "reports",
        "event_inbox",
        "event_outbox",
        "workflow_start_outbox",
        "workflow_command_outbox",
        "cleanup_operations",
        "audit_events",
    }
    for table in required:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    assert "ENGINE=InnoDB" in sql
    assert "utf8mb4_0900_bin" in sql
