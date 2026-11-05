"""Opt-in live model smoke with a simulated provider; never submits real tests.

Credentials are read from the process environment. Output contains only sanitized
run summaries, not prompts, credentials, or raw provider errors.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

from game_test_agent.activities import ActionService, ReasoningService
from game_test_agent.adapters import ReferenceProviderAdapter
from game_test_agent.agents import Investigator
from game_test_agent.capabilities import CapabilityRegistry
from game_test_agent.contracts.models import (
    Budget,
    CapabilityDescriptor,
    EvidenceManifestItem,
    InvestigationRequest,
    TestObjective,
)
from game_test_agent.evidence import EvidenceManifestIngestor
from game_test_agent.mcp_gateway import ActionDispatcher
from game_test_agent.planning import ActionPlanner
from game_test_agent.policies import PolicyEngine
from game_test_agent.reports import ReportService
from game_test_agent.storage import LocalArtifactStore, Repository
from game_test_agent.workflows import InvestigationController
from pydantic import ValidationError


class DiagnosticInvestigator(Investigator):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.schema_failures = []

    def _parse_prompt_decision(self, raw):
        try:
            return super()._parse_prompt_decision(raw)
        except ValidationError as error:
            self.schema_failures.append(
                [
                    {"location": list(item["loc"]), "type": item["type"], "message": item["msg"]}
                    for item in error.errors(include_input=False, include_url=False)
                ]
            )
            raise


class SimulatedMetricsProvider(ReferenceProviderAdapter):
    content = b"frame_ms\n16\n17\n18\n17\n"

    async def submit(self, action):
        operation = await super().submit(action)
        self._results[operation.operation_id] = self._results[operation.operation_id].model_copy(
            update={
                "evidence_manifest": [
                    EvidenceManifestItem(
                        evidence_id="metrics",
                        artifact_ref="simulated://metrics",
                        kind="frame_metrics",
                        mime_type="text/csv",
                        sha256=hashlib.sha256(self.content).hexdigest(),
                        locator={"kind": "sample_window", "value": "all"},
                        quality="simulated",
                        producer=self.provider_id,
                        producer_version="1",
                    )
                ]
            }
        )
        return operation

    async def fetch_artifact(self, _ref):
        return self.content, "text/csv"


async def run_case(required_runs: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="agent-model-smoke-") as directory:
        root = Path(directory)
        repository = Repository(root / "ledger.db")
        try:
            provider = SimulatedMetricsProvider()
            registry = CapabilityRegistry(repository)
            registry.register(
                CapabilityDescriptor(
                    capability_id="test.run",
                    capability_version="1",
                    provider_id=provider.provider_id,
                    provider_revision="1",
                    tool_name="test.run",
                    project_ids=["smoke"],
                    input_schema={
                        "type": "object",
                        "properties": {"case": {"type": "string"}},
                        "required": ["case"],
                        "additionalProperties": False,
                    },
                    risk_level="bounded_execution",
                    execution_owner=provider.provider_id,
                    resource_domain="simulated",
                    estimated_cost={"device_minutes": 1},
                )
            )
            investigation, _ = repository.create_investigation(
                InvestigationRequest(
                    tenant_id="smoke",
                    project_id="smoke",
                    objective=TestObjective(
                        objective_id=f"simulated-{required_runs}-runs",
                        description=(
                            f"这是模拟 provider 的协议验收，不验证真实游戏构建。要求同配置至少 {required_runs} "
                            "个独立模拟 run 的平台判定 passed。运行不足则继续实验，达到要求立即报告。"
                        ),
                        acceptance_criteria=[
                            f"At least {required_runs} distinct simulated run IDs with test_verdict passed. "
                            "Only simulated runs are required; never request real build provenance."
                        ],
                    ),
                    constraints={"action_parameters": {"case": "frame-p95-smoke"}},
                    budget=Budget(
                        max_rounds=4, max_real_runs=3, max_device_minutes=5, max_wall_seconds=480
                    ),
                ),
                f"model-smoke-{required_runs}",
            )
            investigator = DiagnosticInvestigator(
                "qwen3.8-flash",
                enabled=True,
                model_api="chat_completions",
                output_mode="json_object",
                timeout_seconds=120,
                max_output_tokens=2048,
                extra_body={"enable_thinking": False},
            )
            dispatcher = ActionDispatcher()
            dispatcher.register(provider)
            controller = InvestigationController(
                repository,
                registry,
                ReasoningService(repository, investigator),
                ActionPlanner("disabled"),
                ActionService(repository, PolicyEngine(repository, registry), dispatcher),
                EvidenceManifestIngestor(repository, LocalArtifactStore(root / "artifacts")),
                ReportService(repository),
                provider_lookup=lambda _: provider,
            )
            controller.context_builder.window_limit_tokens = 8192
            controller.context_builder.reserved_output_tokens = 2048
            started = time.monotonic()
            report = await controller.run(
                investigation.investigation_id, principal_id="smoke", scopes=set()
            )
            decisions = [
                item
                for index in range(1, 5)
                if (item := repository.get_decision_record(investigation.investigation_id, index))
            ]
            kinds = [item.decision.kind for item in decisions]
            return {
                "required_runs": required_runs,
                "execution_source": "simulated",
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "actual_runs": len(provider._actions),
                "decisions": kinds,
                "reasons": [item.decision.reason_summary for item in decisions],
                "evidence_references": [item.decision.evidence_ids for item in decisions],
                "stop_reason": report.stop_reason if report else "waiting_input",
                "passed": kinds == ["experiment"] * required_runs + ["report"]
                and len(provider._actions) == required_runs,
                "model_usage": repository.model_usage(investigation.investigation_id),
                "schema_failures": investigator.schema_failures,
            }
        finally:
            repository.close()


async def run_cases(output: Path):
    # The SDK's shared async HTTP client must stay on one event loop.
    results = []
    for count in (1, 2):
        try:
            result = await run_case(count)
        except Exception as error:
            result = {"required_runs": count, "passed": False, "error_type": type(error).__name__}
        results.append(result)
        print(json.dumps(result, ensure_ascii=True), flush=True)
        output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return all(item["passed"] for item in results)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not os.environ.get("OPENAI_API_KEY") or not os.environ.get("OPENAI_BASE_URL"):
        raise SystemExit("Set OPENAI_API_KEY and OPENAI_BASE_URL in the process environment.")
    raise SystemExit(0 if asyncio.run(run_cases(args.output)) else 1)


if __name__ == "__main__":
    main()
