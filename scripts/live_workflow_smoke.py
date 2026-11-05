"""Submit one bounded real test through the generic investigation API/workflow.

The platform token is read only from the configured token environment variable.
This script never writes credentials or raw artifact contents to stdout.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import json
import time
import uuid

from fastapi.testclient import TestClient

from game_test_agent.mcp_gateway import load_provider_configs
from game_test_agent.mcp_gateway.http_provider import McpStreamableHttpProvider
from game_test_agent.adapters.lyra_mcp import LyraMcpProvider
from game_test_agent.api import create_app
from game_test_agent.config import Settings


async def preflight(settings: Settings, args: argparse.Namespace) -> None:
    """Read capabilities and reject insufficient test budgets before submitting work."""
    required_runs = 2 if args.scenario == "repeatability" else 1
    if args.max_real_runs < required_runs or args.max_rounds < required_runs + 1:
        raise ValueError(
            f"scenario requires at least {required_runs} runs and {required_runs + 1} rounds"
        )
    if not math.isfinite(args.device_minute_budget) or args.device_minute_budget <= 0:
        raise ValueError("device-minute budget must be finite and positive")
    configs = load_provider_configs(settings.mcp_providers_json)
    if len(configs) != 1:
        raise ValueError("preflight requires exactly one configured MCP provider")
    providers = [
        LyraMcpProvider(config) if config.contract == "lyra_v1"
        else McpStreamableHttpProvider(config)
        for config in configs
    ]
    try:
        capabilities = await providers[0].discover_capabilities()
        candidates = [
            item
            for item in capabilities
            if item.capability_id == "test.run"
            and item.availability == "ready"
            and item.operation_kind == "experiment"
            and item.creates_run
        ]
        if len(candidates) != 1:
            raise ValueError("preflight requires exactly one ready test.run capability")
        per_run = float(candidates[0].estimated_cost.get("device_minutes", 0))
        if not math.isfinite(per_run) or per_run < 0:
            raise ValueError("provider has an invalid device-minute estimate")
        required = required_runs * per_run
        if required > args.device_minute_budget:
            raise ValueError(
                f"scenario budget insufficient: required_device_minutes={required:g}, "
                f"configured={args.device_minute_budget:g}; no run submitted"
            )
        print(
            json.dumps(
                {
                    "preflight": "passed",
                    "required_runs": required_runs,
                    "required_device_minutes": required,
                }
            ),
            flush=True,
        )
    finally:
        for provider in providers:
            await provider.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--test-id", required=True)
    parser.add_argument("--target", default="windows_client")
    parser.add_argument("--scenario", choices=["single", "repeatability"], default="single")
    parser.add_argument("--wait-seconds", type=int, default=300)
    parser.add_argument("--device-minute-budget", type=float, default=10)
    parser.add_argument("--max-real-runs", type=int, default=1)
    parser.add_argument("--max-rounds", type=int, default=2)
    args = parser.parse_args()

    settings = Settings.from_env()
    if settings.workflow_backend != "local":
        raise ValueError("this bounded smoke script requires the local workflow backend")
    configured = load_provider_configs(settings.mcp_providers_json)
    if len(configured) != 1 or configured[0].provider_id != args.provider:
        raise ValueError("the smoke script requires exactly the selected MCP provider")
    if args.project not in configured[0].project_ids:
        raise ValueError("the selected MCP provider is not configured for this project")
    asyncio.run(preflight(settings, args))
    app = create_app(settings)
    nonce = uuid.uuid4().hex
    headers = {
        "Idempotency-Key": f"live-workflow-{nonce}",
        "X-Principal-ID": "codex-live-workflow",
        "X-Tenant-ID": args.tenant,
        "X-Project-ID": args.project,
        "X-Scopes": "runs:submit",
    }
    objective = (
        {
            "objective_id": "single-run-verdict",
            "description": (
                "判断指定真实用例的本次 Windows Client 运行是否通过。只要求一次真实运行；"
                "若平台终态判定和报告已返回，应停止，不要为凑运行次数重复测试。"
            ),
            "acceptance_criteria": [
                "One real terminal run of the specified test and target has provider verdict passed."
            ],
        }
        if args.scenario == "single"
        else {
            "objective_id": "two-run-repeatability",
            "description": (
                "验证指定用例在相同 Windows Client 目标上至少两次独立真实运行均通过。"
                "第一次通过仍不足以判断重复性；应保持用例、目标和采集配置不变进行第二次受控运行。"
                "达到两次后应停止，不为凑预算继续运行，也不宣称指定构建已验收。"
            ),
            "acceptance_criteria": [
                "At least two distinct real run IDs for the same test_ref and target_kind have provider verdict passed."
            ],
        }
    )
    request = {
        "tenant_id": args.tenant,
        "project_id": args.project,
        "objective": objective,
        "root_event_id": f"user-live-{nonce}",
        "constraints": {
            "action_parameters": {
                "test_ref": {"test_id": args.test_id},
                "capture_profile": "none",
            },
            "action_target_selector": {"target_kind": args.target},
            "action_constraints": {"timeout_seconds": 180},
        },
        "budget": {
            "max_rounds": args.max_rounds,
            "max_real_runs": args.max_real_runs,
            "max_tool_reads": 40,
            "max_device_minutes": args.device_minute_budget,
            "max_wall_seconds": args.wait_seconds,
        },
    }
    with TestClient(app) as client:
        started = time.monotonic()
        response = client.post("/v1/investigations", json=request, headers=headers)
        response.raise_for_status()
        investigation_id = response.json()["investigation_id"]
        print(f"investigation_id={investigation_id}", flush=True)
        deadline = time.monotonic() + args.wait_seconds
        while time.monotonic() < deadline:
            response = client.get(f"/v1/investigations/{investigation_id}", headers=headers)
            response.raise_for_status()
            detail = response.json()
            status = detail["status"]
            if status in {"completed", "failed", "canceled", "waiting_input"}:
                break
            time.sleep(2)
        else:
            raise TimeoutError(f"investigation {investigation_id} did not finish")

        repository = app.state.container.repository
        evidence = repository.list_evidence(investigation_id)
        decisions = [
            record
            for round_number in range(1, request["budget"]["max_rounds"] + 1)
            if (record := repository.get_decision_record(investigation_id, round_number))
            is not None
        ]
        actions = repository.list_actions(investigation_id)
        latest = [repository.get_latest_run_result(item["action"]["action_id"]) for item in actions]
        report = (
            repository.get_latest_report(investigation_id)[0] if status == "completed" else None
        )
        print(
            json.dumps(
                {
                    "scenario": args.scenario,
                    "investigation_id": investigation_id,
                    "elapsed_seconds": round(time.monotonic() - started, 2),
                    "status": status,
                    "resolution": detail.get("resolution"),
                    "stop_reason": detail.get("stop_reason"),
                    "runs": [
                        {
                            "action_id": item.action_id,
                            "run_id": item.result.run_ref.run_id,
                            "run_sequence": actions[index]["action"].get("run_sequence"),
                            "execution_status": item.result.execution_status,
                            "test_verdict": item.result.test_verdict,
                            "execution_source": item.result.execution_source,
                        }
                        for index, item in enumerate(latest)
                        if item is not None
                    ],
                    "evidence_count": len(evidence),
                    "decisions": [
                        {
                            "kind": item.decision.kind,
                            "expected_observation": item.decision.expected_observation,
                            "reason_summary": item.decision.reason_summary,
                        }
                        for item in decisions
                    ],
                    "report_summary": report.summary if report else None,
                    "report_hash": repository.get_latest_report(investigation_id)[2]
                    if report
                    else None,
                },
                ensure_ascii=True,
                sort_keys=True,
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
