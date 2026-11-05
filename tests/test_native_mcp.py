import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp import types
from native_plan_helpers import initial_plan_call

from game_test_agent.capabilities import CapabilityRegistry
from game_test_agent.contracts.models import InvestigationRequest
from game_test_agent.mcp_gateway.config import McpProviderConfig
from game_test_agent.mcp_gateway.manager import ProviderManager
from game_test_agent.mcp_gateway.native import NativeMcpProvider
from game_test_agent.reports import ReportService
from game_test_agent.storage import LocalArtifactStore
from game_test_agent.workflows.native_mcp import NativeMcpController


def test_native_discovery_accepts_arbitrary_mcp_tool_schema_without_a_platform_catalog(
    repository, monkeypatch
):
    config = McpProviderConfig(
        provider_id="custom-lab",
        url="https://lab.test/mcp",
        project_ids=["game-a"],
        allowed_tools=["inspect_incident"],
    )
    manager = ProviderManager(CapabilityRegistry(repository), [config], native_tools=True)
    provider = manager.provider("custom-lab")
    assert isinstance(provider, NativeMcpProvider)
    tool = types.Tool(
        name="inspect_incident",
        description="Read original incident details",
        input_schema={
            "type": "object",
            "properties": {"incident": {"type": "string"}},
            "required": ["incident"],
        },
    )
    monkeypatch.setattr(provider, "native_tools", AsyncMock(return_value=[tool]))
    descriptors = asyncio.run(provider.discover_capabilities())
    assert descriptors[0].input_schema == tool.input_schema
    assert descriptors[0].tool_name == "inspect_incident"
    assert not descriptors[0].creates_run
    with pytest.raises(Exception):
        asyncio.run(provider.call_native(tool, {"wrong_key": "bad"}))


@pytest.mark.parametrize("mode", ["normal", "repeat", "pagination", "unknown_tool", "search_focus"])
def test_native_model_calls_original_schema_and_publishes_cited_report(repository, settings, mode):
    async def run():
        view, _ = repository.create_investigation(
            InvestigationRequest(
                tenant_id="studio-a",
                project_id="game-a",
                objective={
                    "objective_id": "diagnose",
                    "description": "Investigate pressure failure",
                },
                constraints={"provider_allowlist": ["custom-lab"]},
                budget={"max_rounds": 8 if mode == "pagination" else 3},
            ),
            "native-test",
        )
        tool = types.Tool(
            name="inspect_incident",
            description="Read evidence",
            input_schema={
                "type": "object",
                "properties": {"incident": {"type": "string"}},
                "required": ["incident"],
            },
        )
        provider = SimpleNamespace(
            config=SimpleNamespace(project_ids=["game-a"], max_response_bytes=10000),
            provider_id="custom-lab",
            native_tools=AsyncMock(return_value=[tool]),
            call_native=AsyncMock(
                return_value=types.CallToolResult(
                    content=[
                        types.TextContent(
                            type="text",
                            text=("x" * 2500 if mode == "search_focus" else "")
                            + "Measured game thread frame time: 48 ms"
                            + ("x" * 4000 if mode in {"pagination", "search_focus"} else ""),
                        )
                    ]
                )
            ),
        )
        if mode == "pagination":

            async def page_result(tool, args):
                return types.CallToolResult(
                    content=[
                        types.TextContent(
                            type="text",
                            text=args["incident"]
                            + " Measured game thread frame time: 48 ms"
                            + "x" * 4000,
                        )
                    ]
                )

            provider.call_native.side_effect = page_result
        controller = NativeMcpController(
            replace(
                settings,
                enable_model=True,
                # Five distinct pages now also retain bounded original excerpts.
                # Other modes keep the tight window to exercise group eviction.
                model_context_window_tokens=16384 if mode == "pagination" else 8192,
                model_max_output_tokens=512,
            ),
            repository,
            SimpleNamespace(providers={"custom-lab": provider}),
            LocalArtifactStore(settings.artifact_root),
            ReportService(repository),
        )
        count = 0

        async def model(messages, tools):
            nonlocal count
            count += 1
            if any(t["function"]["name"] == "inspect_incident" for t in tools):
                assert tools[0]["function"]["parameters"] == tool.input_schema
            pending = set()
            for m in messages:
                if m["role"] == "assistant":
                    assert not pending
                    pending = {c["id"] for c in m.get("tool_calls", [])}
                elif m["role"] == "tool":
                    pending.remove(m["tool_call_id"])
                else:
                    assert not pending
            assert not pending
            if (
                count == 1
                or (mode == "repeat" and count == 2)
                or (mode == "pagination" and count < 6)
            ):
                args = json.dumps(
                    {"incident": f"page-{count}" if mode == "pagination" else "pressure-1"}
                )
                call = SimpleNamespace(
                    function=SimpleNamespace(name="inspect_incident", arguments=args),
                    id=f"call-native-{count}",
                )
                call.model_dump = lambda **_: {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.function.name, "arguments": call.function.arguments},
                }
                message = SimpleNamespace(content=None, tool_calls=[call])
                if count == 1:
                    message.tool_calls.insert(0, initial_plan_call())
                if mode == "unknown_tool":
                    rejected = SimpleNamespace(
                        function=SimpleNamespace(name="not_authorized", arguments="{}"),
                        id="rejected",
                    )
                    rejected.model_dump = lambda **_: {
                        "id": "rejected",
                        "type": "function",
                        "function": {"name": "not_authorized", "arguments": "{}"},
                    }
                    message.tool_calls.append(rejected)
            elif mode == "search_focus" and count == 2:
                evidence_id = json.loads(messages[2]["content"])["evidence_catalog"][-1][
                    "evidence_id"
                ]
                call = SimpleNamespace(
                    function=SimpleNamespace(
                        name="agent_read_collected_evidence",
                        arguments=json.dumps(
                            {
                                "evidence_id": evidence_id,
                                "query": "48 ms",
                                "max_bytes": 6000,
                            }
                        ),
                    ),
                    id="search-hit",
                )
                call.model_dump = lambda **_: {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.function.name, "arguments": call.function.arguments},
                }
                message = SimpleNamespace(content=None, tool_calls=[call])
            else:
                if mode in {"repeat", "pagination", "search_focus"}:
                    assert {t["function"]["name"] for t in tools} in ({"agent_submit_report"}, {"agent_update_plan", "agent_submit_report"})
                if mode == "search_focus":
                    assert "48 ms" in messages[2]["content"]
                    searched = next(
                        json.loads(m["content"])
                        for m in reversed(messages)
                        if m["role"] == "tool" and json.loads(m["content"]).get("query") == "48 ms"
                    )
                    assert "48 ms" in searched["result"]
                # Tight windows may remove complete tool groups. The retained
                # original excerpt must still provide both the ID and quote.
                excerpts = [json.loads(m["content"]) for m in reversed(messages)
                            if m["role"] == "tool"]
                excerpts += json.loads(messages[2]["content"])["collected_evidence_search_hits"]
                evidence_id = next(p["evidence_id"] for p in excerpts
                                   if "48 ms" in p.get("result", p.get("original_excerpt", "")))
                message = SimpleNamespace(
                    tool_calls=[],
                    content=json.dumps(
                        {
                            "summary": "压力期间游戏线程帧耗时达到48毫秒，原因仍需对照实验验证。",
                            "citations": [{"evidence_id": evidence_id, "quote": "48 ms"}],
                            "limitations": ["未验证修复"],
                            "recommended_actions": ["减少注入压力后对照重测"],
                        }
                    ),
                )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=message)],
                usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50),
            )

        controller._model_call = model
        report = await controller.run(
            view.investigation_id, principal_id="tester", scopes={"runs:read", "evidence:read"}
        )
        if mode == "pagination":
            assert provider.call_native.await_count == 5
        else:
            provider.call_native.assert_awaited_once_with(tool, {"incident": "pressure-1"})
        assert report.stop_reason == "native_mcp_investigation_complete"
        assert report.claims[0].supporting_excerpt == "48 ms"
        assert report.claims[0].verification_status == "verified"
        assert not repository.list_actions(view.investigation_id)
        assert repository.pending_events()[0]["payload"]["data"]["summary"] == report.summary
        evidence_id = report.claims[0].evidence_ids[0]
        excerpt = controller._read_collected_evidence(
            view,
            {
                "evidence_id": evidence_id,
                "query": "48 ms",
                "max_bytes": 128,
            },
        )
        assert excerpt["matched"] and "48 ms" in excerpt["result"]
        other, _ = repository.create_investigation(
            InvestigationRequest(
                tenant_id="studio-a",
                project_id="game-a",
                objective={"objective_id": "other", "description": "Separate investigation"},
            ),
            "native-other",
        )
        with pytest.raises(LookupError):
            controller._read_collected_evidence(other, {"evidence_id": evidence_id})

    asyncio.run(run())
