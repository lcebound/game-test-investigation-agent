from __future__ import annotations

from game_test_agent.contracts.models import CapabilityDescriptor, CapabilitySummary
from game_test_agent.storage import NotFoundError, Repository


class CapabilityRegistry:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def register(self, descriptor: CapabilityDescriptor) -> None:
        if descriptor.risk_level != "read_only" and descriptor.idempotency == "none":
            raise ValueError(
                "write capabilities without idempotency cannot be registered for automatic use"
            )
        if descriptor.risk_level != "read_only" and not descriptor.supports_action_lookup:
            raise ValueError("write capabilities must support action lookup")
        self.repository.register_capability(descriptor)

    def discover(
        self, project_id: str, *, scopes: set[str] | None = None
    ) -> list[CapabilityDescriptor]:
        scopes = scopes or set()
        return [
            item
            for item in self.repository.list_capabilities(project_id)
            if set(item.required_scopes).issubset(scopes)
        ]

    def resolve(
        self,
        project_id: str,
        provider_id: str,
        capability_id: str,
        capability_version: str,
        *,
        scopes: set[str],
    ) -> CapabilityDescriptor:
        for item in self.discover(project_id, scopes=scopes):
            if (
                item.provider_id == provider_id
                and item.capability_id == capability_id
                and item.capability_version == capability_version
            ):
                return item
        raise NotFoundError(
            "requested capability/version is not available in the authorized project"
        )

    def discover_summaries(
        self,
        project_id: str,
        *,
        scopes: set[str] | None = None,
        intent: str | None = None,
    ) -> list[CapabilitySummary]:
        items = self.discover(project_id, scopes=scopes)
        if intent:
            normalized = intent.casefold()
            matching = [
                item
                for item in items
                if normalized in item.capability_id.casefold()
                or normalized in item.operation_kind.casefold()
                or any(normalized in kind.casefold() for kind in item.evidence_types)
            ]
            if matching:
                items = matching
        return [
            CapabilitySummary(
                capability_id=item.capability_id,
                capability_version=item.capability_version,
                provider_id=item.provider_id,
                operation_kind=item.operation_kind,
                risk_level=item.risk_level,
                availability=item.availability,
                evidence_types=item.evidence_types,
                required_scopes=item.required_scopes,
                estimated_cost=item.estimated_cost,
            )
            for item in items
        ]

    def load(
        self,
        project_id: str,
        capability_ids: set[str],
        *,
        scopes: set[str] | None = None,
    ) -> list[CapabilityDescriptor]:
        return [
            item
            for item in self.discover(project_id, scopes=scopes)
            if item.capability_id in capability_ids
        ]
