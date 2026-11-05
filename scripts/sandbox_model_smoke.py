"""Live configured model + native Windows PSEC; MCP data is a synthetic fixture."""
from __future__ import annotations

import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


async def main():
    from mcp import types
    from game_test_agent.analysis.code_execution import build_code_execution
    from game_test_agent.config import Settings
    from game_test_agent.contracts.models import InvestigationRequest
    from game_test_agent.reports import ReportService
    from game_test_agent.storage import LocalArtifactStore, Repository
    from game_test_agent.workflows.native_mcp import NativeMcpController
    for name in ["workbench.env.json", "sandbox.env.json"]:
        for key, value in json.loads((ROOT / "data/local" / name).read_text(encoding="utf-8-sig")).items():
            os.environ.setdefault(key, value)
    output = ROOT / "data/native-sandbox-smoke" / datetime.now().strftime("%Y%m%d-%H%M%S")
    output.mkdir(parents=True)
    os.environ["GTA_DATA_ROOT"] = str(output)
    settings = Settings.from_env()
    repository = Repository(output / "agent.db")
    artifacts = LocalArtifactStore(output / "artifacts")
    code = build_code_execution(settings, repository, artifacts)
    if code is None or not settings.enable_model:
        raise RuntimeError("configured model and sandbox are required")
    tool = types.Tool(name="read_metric_fixture", description="Read the synthetic CSV fixture for analysis; this is not a real game test.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False})
    async def native_tools():
        return [tool]
    async def call_native(*_):
        return types.CallToolResult(content=[types.TextContent(type="text", text=(
            "Synthetic CSV fixture; frame_ms is duration, no business threshold is supplied.\n"
            "phase,frame_ms\nbefore,10\nbefore,20\nafter,40\nafter,60\n"
        ))])
    provider = SimpleNamespace(provider_id="synthetic-metrics", config=SimpleNamespace(
        project_ids=["sandbox-smoke"], max_response_bytes=4096),
        native_tools=native_tools, call_native=call_native)
    providers = SimpleNamespace(providers={"synthetic-metrics": provider})
    view, _ = repository.create_investigation(InvestigationRequest(
        tenant_id="sandbox-smoke", project_id="sandbox-smoke",
        objective={"objective_id": "compare", "description": (
            "先读取 synthetic CSV，再必须用 agent_execute_code 自主编写 Python，"
            "计算 before/after 两组 frame_ms 的均值、after/before 比值和百分比变化，"
            "将 JSON 写入环境变量 GTA_OUTPUT_DIR 指定目录中的 comparison.json，并 print 相同 JSON。"
            "如果程序报错，依据 stderr 修复并传 repair_of_job_id。"
            "最终引用真实结果，说明这是合成样本统计，不能据此确认游戏故障根因。"
        )}, constraints={"provider_allowlist": ["synthetic-metrics"]},
        budget={"max_rounds": 16, "max_tool_reads": 24, "max_wall_seconds": 600},
    ), "live-model-code-smoke")
    controller = NativeMcpController(settings, repository, providers, artifacts, ReportService(repository), code_execution=code)
    try:
        report = await controller.run(view.investigation_id, principal_id="sandbox-smoke",
                                      scopes={"runs:read", "evidence:read"})
        jobs = repository.list_analysis_jobs(view.investigation_id)
        results = []
        for job in jobs:
            result = repository.get_latest_analysis_result(job.spec.job_id)
            results.append({"job_id": job.spec.job_id, "language": job.spec.program.language,
                "code_sha256": job.spec.program.code_sha256, "state": job.state,
                "failure_kind": result.result.failure_kind if result else None,
                "outputs": [o.output_id for o in result.result.outputs] if result else []})
        summary = {"investigation_id": view.investigation_id, "model": settings.model_name,
                   "platform_data": "synthetic_fixture", "executor": "windows_psec",
                   "report_stop_reason": report.stop_reason if report else None, "jobs": results}
        (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({**summary, "output_directory": str(output)}, ensure_ascii=True))
        if not any(j.state == "succeeded" for j in jobs):
            raise RuntimeError("model did not complete a sandbox analysis; inspect local trace")
    finally:
        await code.cancel_investigation(view.investigation_id)
        await controller.close()
        repository.close()


if __name__ == "__main__":
    asyncio.run(main())
