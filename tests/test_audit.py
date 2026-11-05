from __future__ import annotations


def test_audit_redacts_credentials(repository):
    repository.record_audit(
        principal_id="user-1",
        tenant_id="studio-a",
        project_id="game-a",
        action="test",
        target_id="inv-1",
        metadata={"token": "secret-value", "nested": {"password": "hidden", "safe": "ok"}},
    )
    event = repository.list_audit_events("inv-1")[0]
    assert event["metadata"]["token"] == "[REDACTED]"
    assert event["metadata"]["nested"]["password"] == "[REDACTED]"
    assert event["metadata"]["nested"]["safe"] == "ok"
