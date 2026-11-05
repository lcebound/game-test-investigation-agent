"""Native tool-call envelopes use the same model gateway as typed decisions."""
from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from game_test_agent.contracts.models import ModelProfile, ModelUsage
from game_test_agent.models import ModelCallError, ModelGateway, ModelProviderResponse
from game_test_agent.workflows.native_context import native_request_bound


class NativeFunction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64)
    arguments: str = Field(max_length=100_000)


class NativeToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=256)
    type: Literal["function"] = "function"
    function: NativeFunction


class NativeModelMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str | None = Field(default=None, max_length=100_000)
    tool_calls: list[NativeToolCall] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def unique_call_ids(self):
        if not self.tool_calls and not self.content:
            raise ValueError("model response is empty")
        if len({c.id for c in self.tool_calls}) != len(self.tool_calls):
            raise ValueError("tool call IDs must be unique")
        return self


def executable_tool_call(call):
    raw = call.model_dump(exclude_none=True)
    return {"id": raw["id"], "type": raw["type"],
            "function": {"name": raw["function"]["name"],
                         "arguments": raw["function"]["arguments"]}}


class NativeResponseError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool):
        super().__init__(code)
        self.code, self.retryable = code, retryable


def model_error_class(error: Exception) -> tuple[str, bool]:
    if isinstance(error, NativeResponseError):
        return error.code, error.retryable
    status = getattr(error, "status_code", None)
    code = getattr(error, "code", None)
    if code in {"context_length_exceeded", "max_context_length_exceeded"}:
        return "CONTEXT_LIMIT", True
    if status in {401, 403}:
        return "MODEL_AUTHORIZATION_FAILED", False
    if status == 429:
        return "MODEL_RATE_LIMITED", True
    if status in {400, 404, 422}:
        return "MODEL_CONFIGURATION_ERROR", False
    if isinstance(error, TimeoutError):
        return "MODEL_TIMEOUT", True
    if status is not None and status < 500:
        return "MODEL_REQUEST_REJECTED", False
    return "MODEL_UNAVAILABLE", True


async def invoke_native_model(controller, view, turn, messages, tools, remaining, *,
                              report_reserve_tokens=0, report_reserve_cost=0.0):
    deadline = min(view.created_at.timestamp() + view.request.budget.max_wall_seconds, time.time() + remaining)
    async with controller.execution_limits.slot("models", deadline=deadline, scope=(view.request.tenant_id, view.request.project_id)):
        with controller.tracer.span(view.investigation_id, "model.invoke", turn=turn):
            return await _invoke_native_model(controller, view, turn, messages, tools, deadline - time.time(),
                report_reserve_tokens=report_reserve_tokens, report_reserve_cost=report_reserve_cost)


async def _invoke_native_model(controller, view, turn, messages, tools, remaining, *,
                               report_reserve_tokens=0, report_reserve_cost=0.0):
    settings = controller.settings
    input_bound = max(1, native_request_bound(messages, tools))
    output_bound = settings.model_max_output_tokens
    cost_bound = (input_bound * settings.model_cost_per_input_token
                  + output_bound * settings.model_cost_per_output_token)
    controller.repository.reserve_native_model_call(
        view.investigation_id, turn, tokens=input_bound + output_bound,
        estimated_cost=cost_bound, token_limit=settings.model_max_total_tokens,
        cost_limit=settings.model_max_estimated_cost, round_limit=view.request.budget.max_rounds,
        report_reserve_tokens=report_reserve_tokens, report_reserve_cost=report_reserve_cost,
    )
    # No hidden SDK retry. Each attempt is reserved and shares the investigation deadline.
    profile = ModelProfile(
        profile_id="native-investigator", profile_version="2", provider="configured",
        model_name=settings.model_name, api_mode="chat_completions", output_mode="typed",
        timeout_seconds=min(remaining, settings.model_timeout_seconds), max_attempts=1,
        context_limit_tokens=settings.model_context_window_tokens,
        max_output_tokens=output_bound, max_total_tokens=input_bound + output_bound,
        cost_per_input_token=settings.model_cost_per_input_token,
        cost_per_output_token=settings.model_cost_per_output_token,
    )

    async def caller(_profile: ModelProfile, request: dict[str, Any]):
        raw = await controller._model_call(request["messages"], request["tools"])
        usage = getattr(raw, "usage", None)
        actual_in = getattr(usage, "prompt_tokens", None)
        actual_out = getattr(usage, "completion_tokens", None)
        valid_in = isinstance(actual_in, int) and not isinstance(actual_in, bool) and actual_in >= 0
        valid_out = isinstance(actual_out, int) and not isinstance(actual_out, bool) and actual_out >= 0
        accounted_in = actual_in if valid_in else input_bound
        accounted_out = actual_out if valid_out else output_bound
        normalized = ModelUsage(
            input_tokens=accounted_in, output_tokens=accounted_out,
            estimated_cost=(accounted_in * settings.model_cost_per_input_token
                            + accounted_out * settings.model_cost_per_output_token),
            provider_request_id=getattr(raw, "_request_id", None),
            provider_model_id=getattr(raw, "model", None),
        )
        choices = getattr(raw, "choices", None)
        finish_reason = getattr(choices[0], "finish_reason", None) if choices else None
        details = getattr(usage, "completion_tokens_details", None)
        reasoning_tokens = getattr(details, "reasoning_tokens", None)
        # Settle before parsing the message, so malformed/truncated responses still count.
        controller.repository.record_investigation_event(view.investigation_id,
            "mcp.native.model_settled", {"turn": turn,
                "tokens": normalized.input_tokens + normalized.output_tokens,
                "estimated_cost": normalized.estimated_cost,
                "input_tokens": accounted_in, "output_tokens": accounted_out,
                "finish_reason": finish_reason if finish_reason in
                    {"stop", "tool_calls", "length", "content_filter", "function_call"} else None,
                "reasoning_tokens": reasoning_tokens if isinstance(reasoning_tokens, int)
                    and not isinstance(reasoning_tokens, bool) and reasoning_tokens >= 0 else None,
                "accounting": "provider_usage" if valid_in and valid_out
                else "conservative_estimate"})
        if finish_reason == "length":
            # Never execute a partially emitted batch, even if some calls parse.
            raise NativeResponseError("MODEL_OUTPUT_TRUNCATED", retryable=True)
        if finish_reason == "content_filter":
            raise NativeResponseError("MODEL_RESPONSE_FILTERED", retryable=False)
        if not choices:
            return ModelProviderResponse(content={}, usage=normalized)
        try:
            message = choices[0].message
            payload = {"content": message.content,
                       # Provider extensions (for example DeepSeek's index) are
                       # transport metadata, not part of the executable envelope.
                       # Project explicitly; keep strict validation of IDs, type,
                       # function name and the original argument string below.
                       "tool_calls": [executable_tool_call(c) for c in message.tool_calls or []]}
        except (AttributeError, TypeError, ValueError, KeyError):
            payload = {"invalid_native_response": True}
        return ModelProviderResponse(content=payload, usage=normalized)

    gateway = ModelGateway([profile], caller)
    try:
        result = await gateway.invoke(profile.profile_id, {"messages": messages, "tools": tools}, NativeModelMessage)
    except ModelCallError as error:
        if error.last_exception is not None:
            raise error.last_exception
        raise
    return result
