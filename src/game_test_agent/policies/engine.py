from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from jsonschema import Draft202012Validator

from game_test_agent.capabilities import CapabilityRegistry
from game_test_agent.contracts.models import ActionPermit, ActionSpec, InvestigationStatus
from game_test_agent.storage import Repository, canonical_json


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    action: ActionSpec
    permit: ActionPermit
    created: bool


class PolicyEngine:
    def __init__(self, repository: Repository, registry: CapabilityRegistry) -> None:
        self.repository = repository
        self.registry = registry

    def reserve(self, action: ActionSpec, *, principal_id: str, scopes: set[str]) -> PolicyDecision:
        investigation = self.repository.get_investigation(action.investigation_id)
        if investigation.status in {
            InvestigationStatus.COMPLETED,
            InvestigationStatus.CANCELED,
            InvestigationStatus.FAILED,
            InvestigationStatus.CANCELING,
        }:
            raise ValueError("terminal or canceling investigations cannot create actions")
        if (action.tenant_id, action.project_id) != (
            investigation.request.tenant_id,
            investigation.request.project_id,
        ):
            raise PermissionError("action scope does not match its investigation")
        allowed_providers = investigation.request.constraints.get("provider_allowlist")
        if allowed_providers is not None and action.provider_id not in allowed_providers:
            raise PermissionError("action provider is outside the investigation allowlist")
        capability = self.registry.resolve(
            action.project_id,
            action.provider_id,
            action.capability_id,
            action.capability_version,
            scopes=scopes,
        )
        if (
            action.provider_revision is not None
            and action.provider_revision != capability.provider_revision
        ):
            raise PermissionError("action provider revision does not match the capability")
        action = action.model_copy(update={"provider_revision": capability.provider_revision})
        if capability.operation_kind != action.operation_kind:
            raise PermissionError("action operation kind does not match the capability")
        if not capability.creates_run:
            raise PermissionError("non-experiment capability cannot create a run")
        if capability.risk_level == "high_impact":
            raise PermissionError("high-impact capabilities are not available to the first release")
        Draft202012Validator(capability.input_schema).validate(action.parameters)
        requested_minutes = float(action.constraints.get("max_device_minutes", 0))
        estimated = float(capability.estimated_cost.get("device_minutes", 0))
        reservation = max(requested_minutes, estimated)
        request_hash = hashlib.sha256(canonical_json(action).encode()).hexdigest()
        permit = ActionPermit(
            permit_id=f"permit-{uuid.uuid4().hex}",
            action_id=action.action_id,
            principal_id=principal_id,
            request_hash=request_hash,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            reserved_device_minutes=reservation,
        )
        semantic_material = {
            "operation_kind": action.operation_kind,
            "capability_id": action.capability_id,
            "capability_version": action.capability_version,
            "provider_id": action.provider_id,
            "provider_revision": action.provider_revision,
            "build_ref": (action.build_ref.model_dump(mode="json") if action.build_ref else None),
            "test_ref": (action.test_ref.model_dump(mode="json") if action.test_ref else None),
            "parameters": action.parameters,
            "target_selector": action.target_selector.model_dump(mode="json"),
            "execution_constraints": {
                key: value
                for key, value in action.constraints.items()
                if key != "max_device_minutes"
            },
            "experiment_id": action.experiment_id,
        }
        persisted_action, persisted_permit, created = self.repository.reserve_action(
            action,
            permit,
            semantic_material,
            investigation.request.budget.max_device_minutes,
            counter_name=("tool_reads" if capability.risk_level == "read_only" else "real_runs"),
            counter_limit=(
                investigation.request.budget.max_tool_reads
                if capability.risk_level == "read_only"
                else investigation.request.budget.max_real_runs
            ),
        )
        return PolicyDecision(action=persisted_action, permit=persisted_permit, created=created)
