"""Real localhost MCP transport validation, with a simulated readonly operation service."""

import argparse
import asyncio
import json
from pathlib import Path
import subprocess
import socket
import sys
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def serve(port):
    from mcp.server.mcpserver import MCPServer
    from mcp import types

    server = MCPServer(name="readonly-operation-validation", version="1")
    jobs = {}

    @server.tool(structured_output=True, annotations=types.ToolAnnotations(read_only_hint=True))
    async def scan(request_id: str) -> dict[str, Any]:
        if request_id not in jobs:
            jobs[request_id] = {"operation_id": request_id, "status": "running", "submissions": 1}
        return {"operation_id": request_id, "status": jobs[request_id]["status"]}

    @server.tool(structured_output=True, annotations=types.ToolAnnotations(read_only_hint=True))
    async def lookup(request_id: str) -> dict[str, Any]:
        return jobs.get(request_id, {"status": "not_found"})

    @server.tool(structured_output=True, annotations=types.ToolAnnotations(read_only_hint=True))
    async def query(operation_id: str) -> dict[str, Any]:
        job = jobs[operation_id]
        return {
            "operation_id": operation_id,
            "status": "completed",
            "result": {"measurement": 47, "submissions": job["submissions"]},
        }

    @server.tool(structured_output=True)
    async def cancel(operation_id: str) -> dict[str, Any]:
        jobs[operation_id]["status"] = "canceled"
        return {"status": "canceled"}

    server.run(
        transport="streamable-http",
        host="127.0.0.1",
        port=port,
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=False,
    )


async def validate(args):
    from game_test_agent.mcp_gateway.config import McpProviderConfig
    from game_test_agent.mcp_gateway.native import NativeMcpProvider
    from game_test_agent.workflows.native_operations import NativeOperations
    from game_test_agent.storage import Repository
    from game_test_agent.contracts.models import InvestigationRequest
    from game_test_agent.runtime.worker_ownership import WorkerOwnership

    if args.port == 0:
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            args.port = reservation.getsockname()[1]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    log = (args.output.parent / "mcp-server.log").open("w", encoding="utf-8")
    launcher = "import sys,json,runpy;sys.path[:0]=json.loads(sys.argv[1]);sys.argv=sys.argv[2:];runpy.run_path(sys.argv[0],run_name='__main__')"
    process = subprocess.Popen(
        [
            getattr(sys, "_base_executable", sys.executable),
            "-c",
            launcher,
            json.dumps(sys.path),
            str(Path(__file__).resolve()),
            "--serve",
            "--port",
            str(args.port),
        ],
        stdout=log,
        stderr=log,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    provider = NativeMcpProvider(
        McpProviderConfig(
            provider_id="validation-jobs",
            url=f"http://127.0.0.1:{args.port}/mcp",
            project_ids=["offline"],
            allow_insecure_localhost=True,
            allowed_tools=["scan", "lookup", "query", "cancel"],
            native_operations={
                "scan": {"lookup_tool": "lookup", "query_tool": "query", "cancel_tool": "cancel"}
            },
        )
    )
    try:
        tools = None
        for _ in range(50):
            try:
                tools = await provider.native_tools()
                break
            except Exception:
                if process.poll() is not None:
                    raise RuntimeError("validation MCP server failed")
                await asyncio.sleep(0.2)
        assert tools is not None and [tool.name for tool in tools] == ["scan"]
        path = args.output.parent / "operations.db"
        repo = Repository(path)
        import uuid

        view, _ = repo.create_investigation(
            InvestigationRequest(
                tenant_id="validation",
                project_id="offline",
                objective={
                    "objective_id": "operation",
                    "description": "Readonly fixture query; no real test.",
                },
            ),
            uuid.uuid4().hex,
        )
        controller = SimpleNamespace(
            repository=repo, providers=SimpleNamespace(providers={provider.provider_id: provider})
        )
        async with WorkerOwnership(repo).hold(view.investigation_id, "local"):
            assert (
                await NativeOperations(controller).execute(view, "read-1", provider, tools[0], {})
                is None
            )
        repo.close()
        repo = Repository(path)
        controller.repository = repo
        async with WorkerOwnership(repo).hold(view.investigation_id, "local"):
            result = await NativeOperations(controller).execute(
                view, "read-1", provider, tools[0], {}
            )
            assert result.structured_content == {"measurement": 47, "submissions": 1}
            assert not repo.pending_cleanup_resources(view.investigation_id)
        repo.close()
        args.output.write_text(
            json.dumps(
                {
                    "passed": True,
                    "transport": "real MCP streamable HTTP",
                    "remote_service": "simulated readonly job",
                    "scenarios": [
                        "hidden_control_tools",
                        "durable_submit",
                        "client_restart",
                        "collect_once",
                        "cleanup_confirmed",
                    ],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print("MCP readonly operation transport passed")
    finally:
        await provider.close()
        if process.poll() is None:
            process.terminate()
            await asyncio.to_thread(process.wait, 10)
        log.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument(
        "--output", type=Path, default=Path("data/local/harness-final-mcp/result.json")
    )
    args = parser.parse_args()
    if args.serve:
        serve(args.port)
    else:
        asyncio.run(validate(args))
