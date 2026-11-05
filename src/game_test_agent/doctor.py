from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass

from game_test_agent.capabilities import CapabilityRegistry
from game_test_agent.analysis.code_execution import build_code_execution
from game_test_agent.config import Settings
from game_test_agent.events.policy import load_event_source_policies
from game_test_agent.mcp_gateway import ProviderManager, load_provider_configs
from game_test_agent.storage import Repository, create_artifact_store, create_repository


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    name: str
    ok: bool
    detail: str


async def run_checks(settings: Settings, *, network: bool = False) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    repository: Repository | None = None
    try:
        repository = create_repository(settings)
        checks.append(
            DoctorCheck(
                "database",
                repository.ping(),
                f"schema_version={repository.schema_version()}",
            )
        )
    except Exception as error:
        checks.append(DoctorCheck("database", False, type(error).__name__))
    try:
        artifact_store = create_artifact_store(settings)
        stored = artifact_store.put_bytes("doctor", "doctor", b"game-test-agent-doctor")
        ok = artifact_store.read_bytes(stored.ref) == b"game-test-agent-doctor"
        checks.append(DoctorCheck("artifact_store", ok, f"sha256={stored.sha256}"))
    except Exception as error:
        checks.append(DoctorCheck("artifact_store", False, type(error).__name__))
    try:
        provider_configs = load_provider_configs(settings.mcp_providers_json)
        checks.append(DoctorCheck("mcp_configuration", True, f"providers={len(provider_configs)}"))
    except Exception as error:
        provider_configs = []
        checks.append(DoctorCheck("mcp_configuration", False, type(error).__name__))
    try:
        policies = load_event_source_policies(settings.event_source_policies_json)
        checks.append(DoctorCheck("event_source_policies", True, f"policies={len(policies)}"))
    except Exception as error:
        checks.append(DoctorCheck("event_source_policies", False, type(error).__name__))
    try:
        settings.validate()
        enabled = bool((settings.sandbox_image_digests or settings.sandbox_runtime_digest) and settings.sandbox_runner_ids)
        checks.append(
            DoctorCheck(
                "analysis_sandbox_configuration",
                True,
                (
                    f"enabled=true images={len(settings.sandbox_image_digests)} "
                    f"runners={len(settings.sandbox_runner_ids)}"
                    if enabled
                    else "enabled=false"
                ),
            )
        )
    except Exception as error:
        checks.append(DoctorCheck("analysis_sandbox_configuration", False, type(error).__name__))
    if settings.sandbox_backend == "windows" and repository is not None:
        try:
            executor = build_code_execution(settings, repository, create_artifact_store(settings))
            await executor.runner.verify()
            checks.append(DoctorCheck("code_execution_runtime", True,
                                      "isolation_kind=" + executor.runner.isolation_kind))
        except Exception as error:
            checks.append(DoctorCheck("code_execution_runtime", False, type(error).__name__))
    if network and repository is not None and provider_configs:
        manager = ProviderManager(CapabilityRegistry(repository), provider_configs)
        results = await manager.discover_all()
        for result in results:
            checks.append(
                DoctorCheck(
                    f"mcp_provider:{result.provider_id}",
                    result.available,
                    (
                        f"capabilities={result.capability_count}"
                        if result.available
                        else f"error={result.error_code}"
                    ),
                )
            )
    if repository is not None:
        repository.close()
    return checks


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate Game Test Agent configuration")
    parser.add_argument(
        "--network", action="store_true", help="also connect to allowlisted MCP providers"
    )
    args = parser.parse_args()
    checks = asyncio.run(run_checks(Settings.from_env(), network=args.network))
    print(json.dumps([asdict(item) for item in checks], ensure_ascii=False, indent=2))
    if not all(item.ok for item in checks):
        sys.exit(1)


if __name__ == "__main__":
    main()
