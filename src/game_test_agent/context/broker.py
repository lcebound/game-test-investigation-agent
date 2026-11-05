from __future__ import annotations

from game_test_agent.capabilities import CapabilityRegistry
from game_test_agent.contracts.models import (
    CapabilityDescriptor,
    CapabilitySummary,
    ContextAccessScope,
    ContextBundle,
    ContextManifestItem,
    EvidenceExcerpt,
    EvidenceSearchFilters,
    EvidenceSearchHit,
)
from game_test_agent.context.access import ContextAccess
from game_test_agent.context.budget import fit_final_context
from game_test_agent.evidence.context import ContextBuilder
from game_test_agent.memory import ProjectPackRegistry


class ContextBroker:
    """Goal-aware bounded context and progressive capability loading facade."""

    def __init__(
        self,
        builder: ContextBuilder,
        registry: CapabilityRegistry,
        access: ContextAccess | None = None,
        project_packs: ProjectPackRegistry | None = None,
    ) -> None:
        self.builder = builder
        self.registry = registry
        self.access = access
        self.project_packs = project_packs

    def assemble(
        self,
        investigation_id: str,
        *,
        scopes: set[str],
        step_id: str | None = None,
        intent: str | None = None,
        capability_ids: set[str] | None = None,
        window_limit_tokens: int | None = None,
    ) -> ContextBundle:
        context = self.builder.build(
            investigation_id,
            scopes=scopes,
            step_id=step_id,
            capability_intent=intent,
            capability_ids=capability_ids,
            window_limit_tokens=window_limit_tokens,
        )
        version = context.investigation.request.constraints.get("project_pack_version")
        if self.project_packs is None or not isinstance(version, str):
            return fit_final_context(context)
        request = context.investigation.request
        pack = self.project_packs.resolve(request.tenant_id, request.project_id, version)
        manifest = context.context_manifest
        if manifest is not None:
            manifest = manifest.model_copy(
                update={
                    "included": [
                        *manifest.included,
                        ContextManifestItem(
                            object_type="project_pack",
                            object_id=pack.pack_id,
                            revision=pack.version,
                            reason="requested reviewed project knowledge",
                            freshness="current",
                            estimated_tokens=self.builder._estimate_tokens(pack),
                        ),
                    ]
                }
            )
        return fit_final_context(
            context.model_copy(update={"project_pack": pack, "context_manifest": manifest})
        )

    def discover_capabilities(
        self,
        project_id: str,
        *,
        scopes: set[str],
        intent: str | None = None,
    ) -> list[CapabilitySummary]:
        return self.registry.discover_summaries(project_id, scopes=scopes, intent=intent)

    def load_capability(
        self,
        project_id: str,
        capability_id: str,
        version: str,
        *,
        scopes: set[str],
    ) -> CapabilityDescriptor:
        matches = [
            item
            for item in self.registry.load(project_id, {capability_id}, scopes=scopes)
            if item.capability_version == version
        ]
        if len(matches) != 1:
            raise LookupError("exact capability version is not available in scope")
        return matches[0]

    def retrieve(
        self,
        investigation_id: str,
        query: str,
        scope: ContextAccessScope,
        *,
        filters: EvidenceSearchFilters | None = None,
        limit: int = 20,
    ) -> list[EvidenceSearchHit]:
        if self.access is None:
            raise RuntimeError("context evidence access is not configured")
        return self.access.search_evidence(
            investigation_id,
            scope,
            filters=filters,
            query=query,
            limit=limit,
        )

    def read_excerpt(
        self,
        investigation_id: str,
        evidence_id: str,
        scope: ContextAccessScope,
        *,
        byte_offset: int = 0,
        max_bytes: int = 64 * 1024,
    ) -> EvidenceExcerpt:
        if self.access is None:
            raise RuntimeError("context evidence access is not configured")
        return self.access.read_evidence(
            investigation_id,
            evidence_id,
            scope,
            byte_offset=byte_offset,
            max_bytes=max_bytes,
        )
