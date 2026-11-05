import asyncio
import json

import pytest

from game_test_agent.adapters.lyra_mcp import LyraMcpProvider
from game_test_agent.capabilities import CapabilityRegistry
from game_test_agent.config import Settings
from game_test_agent.mcp_gateway import ProviderManager, load_provider_configs
from game_test_agent.mcp_gateway.http_provider import McpStreamableHttpProvider


def test_manager_assembles_only_mcp_providers(repository):
    configs = load_provider_configs(json.dumps([
        {"provider_id": "lyra", "contract": "lyra_v1", "url": "https://lyra.test/mcp",
         "project_ids": ["game-a"]},
        {"provider_id": "generic", "url": "https://generic.test/mcp", "project_ids": ["game-a"]},
    ]))
    manager = ProviderManager(CapabilityRegistry(repository), configs)
    assert isinstance(manager.provider("lyra"), LyraMcpProvider)
    assert isinstance(manager.provider("generic"), McpStreamableHttpProvider)
    assert manager.provider("lyra").catalog_supported
    assert not manager.provider("generic").catalog_supported
    asyncio.run(manager.close())


def test_legacy_http_provider_environment_fails_explicitly_without_leaking_secrets(monkeypatch):
    monkeypatch.setenv("GTA_INTEGRATION_PROVIDERS_JSON", '[{"token":"private-test-key"}]')
    with pytest.raises(ValueError, match="GTA_MCP_PROVIDERS_JSON") as error:
        Settings.from_env()
    assert "private-test-key" not in str(error.value)


def test_mcp_config_rejects_legacy_api_fields_and_multiple_lyra_projects():
    with pytest.raises(ValueError):
        load_provider_configs(json.dumps([{
            "provider_id": "lyra", "base_url": "https://lyra.test", "project_id": "game-a",
        }]))
    with pytest.raises(ValueError, match="exactly one project"):
        load_provider_configs(json.dumps([{
            "provider_id": "lyra", "contract": "lyra_v1", "url": "https://lyra.test/mcp",
            "project_ids": ["game-a", "game-b"],
        }]))
