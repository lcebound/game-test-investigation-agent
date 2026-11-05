"""Provider tool-call metadata must not invalidate an otherwise valid native turn."""
import asyncio
from types import SimpleNamespace

import pytest

from game_test_agent.models import ModelCallError
from game_test_agent.workflows.native_model import invoke_native_model
from test_native_reliability import setup


def raw_response(*calls):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content="", tool_calls=list(calls)))],
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20))


def provider_call(ident="call-1", name="agent_read_collected_evidence", arguments='{"evidence_id":"e1"}', **extra):
    item = SimpleNamespace(id=ident, type="function",
        function=SimpleNamespace(name=name, arguments=arguments), **extra)
    item.model_dump = lambda **kwargs: {"id": item.id, "type": item.type,
        "function": {"name": item.function.name, "arguments": item.function.arguments}, **extra}
    return item


def test_native_provider_index_is_projected_without_changing_tool_arguments(repository, settings):
    async def scenario():
        view, controller, _ = setup(repository, settings)
        arguments = '{ "evidence_id": "e1", "query": "原文" }'
        calls = [provider_call(arguments=arguments, index=0), provider_call(ident="call-2", index=1)]
        async def model(*args):
            return raw_response(*calls)
        controller._model_call = model
        result = await invoke_native_model(controller, view, 1, [], [], 20)
        assert result.output.tool_calls[0].function.arguments == arguments
        assert [c.id for c in result.output.tool_calls] == ["call-1", "call-2"]
        assert "index" not in result.output.tool_calls[0].model_dump()
        assert result.usage.input_tokens == 100
    asyncio.run(scenario())


@pytest.mark.parametrize("invalid", ["duplicate_id", "nonstring_arguments", "wrong_type"])
def test_provider_projection_keeps_executable_envelope_checks(repository, settings, invalid):
    async def scenario():
        view, controller, _ = setup(repository, settings)
        calls = [provider_call(index=0)]
        if invalid == "duplicate_id":
            calls.append(provider_call(index=1))
        elif invalid == "nonstring_arguments":
            calls[0].function.arguments = {"evidence_id": "e1"}
        else:
            calls[0].type = "custom"
        async def model(*args):
            return raw_response(*calls)
        controller._model_call = model
        with pytest.raises(ModelCallError):
            await invoke_native_model(controller, view, 1, [], [], 20)
    asyncio.run(scenario())
