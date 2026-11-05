"""Exercise real MCP sessions for both supported platform contracts, with simulated servers."""
import asyncio
from dataclasses import replace
import json
import hashlib
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx
import pytest

from game_test_agent.capabilities import CapabilityRegistry
from game_test_agent.api import create_app
from game_test_agent.contracts.models import ActionSpec
from game_test_agent.mcp_gateway import ProviderManager
from game_test_agent.mcp_gateway.config import McpProviderConfig


@pytest.fixture(params=["generic_v2", "lyra_v1"])
def mcp_endpoint(request, tmp_path):
    root = Path(__file__).parents[1]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {**os.environ, "PYTHONPATH": str(root / "src"), "PYTHONUTF8": "1",
           "GTA_REFERENCE_MCP_PORT": str(port)}
    command = ([sys.executable, "-m", "game_test_agent.adapters.reference_mcp"]
               if request.param == "generic_v2" else
               [sys.executable, str(root / "tests/fixtures/lyra_mcp_server.py"), str(port)])
    with (tmp_path / "mcp-server.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command, cwd=root, env=env, stdout=log, stderr=log,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            deadline = time.monotonic() + 15
            while True:
                if process.poll() is not None:
                    pytest.fail("MCP fixture exited before startup")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    if time.monotonic() > deadline:
                        pytest.fail("MCP fixture startup timed out")
                    time.sleep(0.05)
            yield request.param, f"http://127.0.0.1:{port}/mcp"
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def test_platform_lifecycle_uses_mcp_transport_only(mcp_endpoint, repository, monkeypatch):
    contract, url = mcp_endpoint
    lyra = contract == "lyra_v1"
    provider_id, project_id = ("platform-local", "default") if lyra else ("reference-simulated", "game-a")

    def no_direct_platform_api(*args, **kwargs):
        pytest.fail("Agent attempted a direct platform HTTP API call")

    monkeypatch.setattr(httpx, "AsyncClient", no_direct_platform_api)
    manager = ProviderManager(CapabilityRegistry(repository), [McpProviderConfig(
        provider_id=provider_id, contract=contract, url=url, project_ids=[project_id],
        allow_insecure_localhost=True,
    )])

    async def run():
        try:
            result = await manager.discover_all()
            assert result[0].available and result[0].capability_count == 1
            instance = manager.provider(provider_id)
            if lyra:
                catalog = await instance.list_tests("fixture", page=2, page_size=5)
                assert catalog.page == 2 and catalog.items[0].test_ref.test_id == "case:fixture"
            action = ActionSpec(
                tenant_id="studio-a", project_id=project_id, investigation_id="inv-fixture",
                action_id="act-fixture", idempotency_key="stable-fixture", capability_id="test.run",
                capability_version="1.0.0", provider_id=provider_id,
                test_ref={"test_id": "case:fixture"},
                parameters={"capture_profile": "none"} if lyra else {"case": "smoke"},
                target_selector={"target_kind": "windows_client" if lyra else "simulated"},
                constraints={"root_event_id": "event-fixture", "causation_id": "inv-fixture",
                             "investigation_depth": 1},
                expected_observation="simulated protocol result", plan_revision=1,
            )
            assert await instance.lookup_action("never-submitted") is None
            operation = await instance.submit(action)
            assert (await instance.lookup_action(action.action_id)).run_ref == operation.run_ref
            run_result = await instance.query_operation(operation.operation_id)
            assert run_result.test_verdict == "passed" and run_result.execution_source == "simulated"
            evidence = run_result.evidence_manifest[0]
            content, mime = await instance.fetch_artifact(evidence.artifact_ref)
            assert hashlib.sha256(content).hexdigest() == evidence.sha256 and mime == evidence.mime_type
            if lyra:
                assert (await instance.query_source_run(operation.run_ref)).run_ref == operation.run_ref
            assert (await instance.cancel(action.action_id))["accepted"]
        finally:
            await manager.close()

    asyncio.run(run())


def test_agent_api_investigation_completes_through_mcp(mcp_endpoint, settings):
    """Real MCP transport and real Agent workflow, with explicitly simulated execution."""
    contract, url = mcp_endpoint
    lyra = contract == "lyra_v1"
    provider_id, project_id = ("platform-local", "default") if lyra else ("reference-simulated", "game-a")
    configured = replace(settings, mcp_providers_json=json.dumps([{
        "provider_id": provider_id, "contract": contract, "url": url,
        "project_ids": [project_id], "allow_insecure_localhost": True,
    }]))

    async def run():
        app = create_app(configured)
        auth = {"X-Principal-ID": "tester", "X-Tenant-ID": "studio-a", "X-Project-ID": project_id,
                "X-Scopes": "tests:read,runs:read,runs:submit,evidence:read"}
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post('/v1/investigations', headers={**auth, 'Idempotency-Key': 'mcp-api'}, json={
                    'tenant_id': 'studio-a', 'project_id': project_id,
                    'objective': {'objective_id': 'smoke', 'description': 'Run the simulated MCP test'},
                    'constraints': {
                        'provider_allowlist': [provider_id],
                        'action_parameters': {'test_ref': {'test_id': 'case:fixture'}, 'capture_profile': 'none'} if lyra else {'case': 'smoke'},
                        'action_target_selector': {'target_kind': 'windows_client' if lyra else 'simulated'},
                    },
                })
                assert response.status_code == 202
                investigation_id = response.json()['investigation_id']
                for _ in range(200):
                    snapshot = (await client.get(f'/v1/investigations/{investigation_id}/workspace', headers=auth)).json()
                    if snapshot['investigation']['status'] in {'completed', 'failed'}:
                        break
                    await asyncio.sleep(0.05)
                assert snapshot['investigation']['status'] == 'completed'
                assert snapshot['report'] is not None
                assert len(snapshot['actions']) == len(snapshot['runs']) == 1
                assert snapshot['runs'][0]['result']['execution_source'] == 'simulated'
                assert snapshot['runs'][0]['result']['test_verdict'] == 'passed'
                assert snapshot['runs'][0]['ingestion_state'] == 'ingested'
                assert len(snapshot['evidence']) == 1 and snapshot['evidence'][0]['retrieval_status'] == 'ready'
                assert snapshot['investigation']['request']['budget']['max_wall_seconds'] == 7200
                assert snapshot['actions'][0]['action']['constraints']['root_event_id']
    asyncio.run(run())
