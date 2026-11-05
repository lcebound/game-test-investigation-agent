from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from game_test_agent.capabilities import CapabilityRegistry
from game_test_agent.contracts.models import CapabilityDescriptor
from game_test_agent.mcp_gateway.config import McpProviderConfig
from game_test_agent.mcp_gateway.gateway import ActionDispatcher, ProviderAdapter
from game_test_agent.mcp_gateway.http_provider import McpStreamableHttpProvider


@dataclass(frozen=True, slots=True)
class ProviderDiscoveryResult:
    provider_id: str
    available: bool
    capability_count: int = 0
    error_code: str | None = None


class DiscoverableProvider(ProviderAdapter, Protocol):
    async def discover_capabilities(self) -> list[CapabilityDescriptor]: ...

    async def fetch_artifact(self, artifact_ref: str) -> tuple[bytes, str]: ...


class ProviderManager:
    def __init__(
        self,
        registry: CapabilityRegistry,
        configs: list[McpProviderConfig],
        *, native_tools: bool = False,
    ) -> None:
        from game_test_agent.mcp_gateway.native import NativeMcpProvider

        def build(config):
            if native_tools:
                return NativeMcpProvider(config)
            if config.contract == "lyra_v1":
                from game_test_agent.adapters.lyra_mcp import LyraMcpProvider
                return LyraMcpProvider(config)
            return McpStreamableHttpProvider(config)

        self.registry = registry
        self.providers: dict[str, DiscoverableProvider] = {
            config.provider_id: build(config)
            for config in configs
        }
        self.dispatcher = ActionDispatcher()
        for provider in self.providers.values():
            self.dispatcher.register(provider)
        for config in configs:
            provider = self.providers[config.provider_id]
            for project_id in config.project_ids:
                for descriptor in self.registry.repository.list_capabilities(project_id):
                    if descriptor.provider_id == config.provider_id:
                        provider.bind_capability(descriptor)

    async def discover_all(self) -> list[ProviderDiscoveryResult]:
        results = await asyncio.gather(
            *(self._discover(provider) for provider in self.providers.values())
        )
        return sorted(results, key=lambda item: item.provider_id)

    async def _discover(self, provider: DiscoverableProvider) -> ProviderDiscoveryResult:
        try:
            descriptors = await provider.discover_capabilities()
            for descriptor in descriptors:
                self.registry.register(descriptor)
            return ProviderDiscoveryResult(
                provider_id=provider.provider_id,
                available=True,
                capability_count=len(descriptors),
            )
        except Exception as error:
            return ProviderDiscoveryResult(
                provider_id=provider.provider_id,
                available=False,
                error_code=getattr(error, "code", type(error).__name__),
            )

    def provider(self, provider_id: str) -> DiscoverableProvider:
        try:
            return self.providers[provider_id]
        except KeyError as error:
            raise LookupError(f"provider {provider_id!r} is not allowlisted") from error

    async def close(self) -> None:
        for provider in self.providers.values():
            close = getattr(provider, "close", None)
            if close is not None:
                await close()
