from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from game_test_agent.events import EventIngress
from game_test_agent.events.policy import EventSourcePolicy
from game_test_agent.storage import ConflictError


def event(event_id: str = "evt-1", result_revision: int = 1) -> bytes:
    now = datetime.now(timezone.utc).isoformat()
    return json.dumps(
        {
            "event_id": event_id,
            "event_type": "game_test.anomaly.detected",
            "source": "lab-a",
            "tenant_id": "studio-a",
            "project_id": "game-a",
            "occurred_at": now,
            "published_at": now,
            "root_event_id": "evt-1",
            "origin": "platform",
            "data": {
                "finding_id": "finding-1",
                "incident_key": "game-a/build-42/perf",
                "kind": "performance_regression",
                "severity": "high",
                "run_ref": {"provider_id": "lab-a", "project_id": "game-a", "run_id": "run-1"},
                "result_revision": result_revision,
                "execution_source": "real",
                "build": {"id": "build-42"},
                "environment_fingerprint": "env-1",
                "observation": {
                    "metric": "frame_time_p95",
                    "value": 28,
                    "unit": "ms",
                    "comparison_status": "comparable",
                },
            },
        }
    ).encode()


def test_event_inbox_is_idempotent(repository):
    ingress = EventIngress(repository)
    message = event()
    first, created = ingress.accept(message)
    second, repeated = ingress.accept(message)
    assert created is True
    assert repeated is False
    assert first.investigation_id == second.investigation_id


def test_same_event_id_with_changed_payload_is_rejected(repository):
    ingress = EventIngress(repository)
    ingress.accept(event())
    with pytest.raises(ConflictError):
        ingress.accept(event(result_revision=2))


def test_platform_event_with_missing_metadata_remains_unknown_and_is_idempotent(repository):
    payload = json.loads(event())
    payload["data"].update(
        {
            "kind": "infrastructure_failure",
            "build": {"id": None, "revision": None, "sha256": None, "configuration": None},
            "environment_fingerprint": None,
            "rule": {"id": "execution-infrastructure-failure", "version": "1"},
        }
    )
    body = json.dumps(payload).encode()
    ingress = EventIngress(repository)
    first, created = ingress.accept(body)
    second, repeated = ingress.accept(body)
    assert created and not repeated
    assert first.investigation_id == second.investigation_id
    assert first.request.build_ref is None
    assert first.request.constraints["environment_fingerprint"] is None
    assert first.request.constraints["anomaly_kind"] == "infrastructure"


def test_evidence_update_is_durable_command_and_idempotent(repository):
    ingress = EventIngress(repository)
    investigation, _ = ingress.accept(event())
    now = datetime.now(timezone.utc).isoformat()
    message = json.dumps(
        {
            "event_id": "evt-evidence-1",
            "event_type": "game_test.evidence.updated",
            "source": "lab-a",
            "tenant_id": "studio-a",
            "project_id": "game-a",
            "occurred_at": now,
            "published_at": now,
            "root_event_id": "evt-1",
            "origin": "platform",
            "investigation_id": investigation.investigation_id,
            "data": {
                "run_ref": {
                    "provider_id": "lab-a",
                    "project_id": "game-a",
                    "run_id": "run-1",
                },
                "result_revision": 2,
                "evidence_refs": ["artifact-1"],
                "evidence_pending": False,
            },
        }
    ).encode()
    _, created = ingress.accept(message)
    _, repeated = ingress.accept(message)
    assert created is True
    assert repeated is False
    commands = repository.pending_commands()
    assert commands[0]["kind"] == "evidence"
    assert commands[0]["payload"]["result_revision"] == 2


def test_new_failure_after_terminal_incident_starts_a_new_investigation(repository):
    from game_test_agent.contracts.models import InvestigationStatus

    ingress = EventIngress(repository)
    first, _ = ingress.accept(event())
    for status in [
        InvestigationStatus.DISCOVERING,
        InvestigationStatus.PLANNING,
        InvestigationStatus.REPORTING,
        InvestigationStatus.COMPLETED,
    ]:
        repository.transition(first.investigation_id, status)
    payload = json.loads(event(event_id="evt-next"))
    payload["data"]["finding_id"] = "finding-next"
    payload["data"]["run_ref"]["run_id"] = "run-next"
    payload["data"]["test_ref"] = {"test_id": "case:registered-case"}
    payload["data"]["target_selector"] = {"target_kind": "windows_client"}
    second, created = ingress.accept(json.dumps(payload).encode())
    assert created
    assert second.investigation_id != first.investigation_id
    assert second.request.constraints["goal_type"] == "diagnosis"
    assert (
        second.request.constraints["action_parameters"]["test_ref"]["test_id"]
        == "case:registered-case"
    )
    assert second.request.constraints["action_target_selector"]["target_kind"] == "windows_client"


def test_event_scopes_come_only_from_operator_policy(repository):
    policies = {
        ("lab-a", "studio-a", "game-a"): EventSourcePolicy(
            source="lab-a",
            tenant_id="studio-a",
            project_id="game-a",
            scopes=["runs:submit", "evidence:read"],
            policy_profile="bounded-diagnosis",
        )
    }
    investigation, _ = EventIngress(repository, policies).accept(event())
    authorization = repository.get_authorization(investigation.investigation_id)
    assert authorization["principal_id"] == "event:lab-a"
    assert set(authorization["scopes"]) == {"runs:submit", "evidence:read"}
    assert authorization["policy_profile"] == "bounded-diagnosis"
