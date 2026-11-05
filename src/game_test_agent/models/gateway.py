from __future__ import annotations

import hashlib
import asyncio
import inspect
import json
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from game_test_agent.contracts.models import ModelAttempt, ModelProfile, ModelUsage


T = TypeVar("T", bound=BaseModel)
ProviderCaller = Callable[
    [ModelProfile, dict[str, Any]],
    "ModelProviderResponse | Awaitable[ModelProviderResponse]",
]


@dataclass(slots=True)
class ModelProviderResponse:
    """Supplier-neutral response. Raw content is retained by reference, not logged."""

    content: str | dict[str, Any] | BaseModel
    usage: ModelUsage = field(default_factory=ModelUsage)


@dataclass(slots=True)
class GatewayResult(Generic[T]):
    output: T
    attempts: list[ModelAttempt]
    raw_output_ref: str
    normalization_log: list[str]
    usage: ModelUsage


class ModelCallError(RuntimeError):
    def __init__(
        self,
        message: str,
        attempts: list[ModelAttempt],
        last_exception: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.last_exception = last_exception


def usage_from_agents_result(result: Any) -> ModelUsage:
    """Extract normalized usage from an Agents SDK result without coupling callers to it."""

    wrapper = getattr(result, "context_wrapper", None)
    usage = getattr(wrapper, "usage", None)
    raw_responses = getattr(result, "raw_responses", []) or []
    last_response = raw_responses[-1] if raw_responses else None
    return ModelUsage(
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        provider_request_id=(
            getattr(last_response, "request_id", None)
            or getattr(last_response, "response_id", None)
        ),
    )


class ModelGateway:
    """Finite retry, normalization, validation, and usage boundary for model calls."""

    def __init__(self, profiles: list[ModelProfile], caller: ProviderCaller, *, execution_limits=None, tracer=None) -> None:
        self._profiles = {item.profile_id: item for item in profiles}
        if len(self._profiles) != len(profiles):
            raise ValueError("model profile IDs must be unique")
        self._caller = caller
        self.execution_limits = execution_limits
        self.tracer = tracer

    def profile(self, profile_id: str) -> ModelProfile:
        try:
            return self._profiles[profile_id]
        except KeyError as error:
            raise LookupError(f"unknown model profile {profile_id!r}") from error

    async def invoke(
        self, profile_id: str, request: dict[str, Any], output_type: type[T], *,
        max_total_tokens: int | None = None, max_estimated_cost: float | None = None,
        investigation_id: str | None = None,
        project_scope=None,
    ) -> GatewayResult[T]:
        timeout = self.profile(profile_id).timeout_seconds
        deadline = time.perf_counter() + timeout
        async with AsyncExitStack() as stack:
            if self.execution_limits:
                await stack.enter_async_context(self.execution_limits.slot("models", deadline=time.time() + timeout, scope=project_scope))
            if self.tracer and investigation_id:
                stack.enter_context(self.tracer.span(investigation_id, "model.invoke"))
            return await self._invoke(profile_id, request, output_type,
                max_total_tokens=max_total_tokens, max_estimated_cost=max_estimated_cost,
                total_deadline=deadline)

    async def _invoke(
        self,
        profile_id: str,
        request: dict[str, Any],
        output_type: type[T],
        *,
        max_total_tokens: int | None = None,
        max_estimated_cost: float | None = None,
        total_deadline: float | None = None,
    ) -> GatewayResult[T]:
        profile = self.profile(profile_id)
        if max_total_tokens is None:
            token_limit = (
                profile.max_total_tokens
                or (profile.context_limit_tokens + profile.max_output_tokens) * profile.max_attempts
            )
        else:
            token_limit = max_total_tokens
        cost_limit = (
            profile.max_estimated_cost if max_estimated_cost is None else max_estimated_cost
        )
        attempts: list[ModelAttempt] = []
        total_input = 0
        total_output = 0
        total_cost = 0.0
        total_latency = 0.0
        last_error = "model call failed"
        last_exception: Exception | None = None
        deadline = total_deadline or time.perf_counter() + profile.timeout_seconds
        for attempt_number in range(1, profile.max_attempts + 1):
            started = time.perf_counter()
            remaining = deadline - started
            if remaining <= 0:
                raise ModelCallError("model call exceeded its total deadline", attempts)
            try:
                response = self._caller(profile, request)
                if inspect.isawaitable(response):
                    response = await asyncio.wait_for(response, timeout=remaining)
            except Exception as error:  # provider adapters own their detailed taxonomy
                latency = (time.perf_counter() - started) * 1_000
                attempts.append(
                    ModelAttempt(
                        attempt=attempt_number,
                        status="transport_error",
                        error=f"{type(error).__name__}: {error}",
                        usage=ModelUsage(latency_ms=latency),
                    )
                )
                last_error = str(error)
                last_exception = error
                continue

            usage = response.usage.model_copy(
                update={
                    "latency_ms": response.usage.latency_ms
                    or (time.perf_counter() - started) * 1_000,
                    "estimated_cost": response.usage.estimated_cost
                    or (
                        response.usage.input_tokens * profile.cost_per_input_token
                        + response.usage.output_tokens * profile.cost_per_output_token
                    ),
                }
            )
            total_input += usage.input_tokens
            total_output += usage.output_tokens
            total_cost += usage.estimated_cost
            total_latency += usage.latency_ms
            raw = self._serialize(response.content)
            raw_ref = "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
            budget_error = self._budget_error(
                total_input,
                total_output,
                total_cost,
                token_limit=token_limit,
                cost_limit=cost_limit,
            )
            if budget_error is not None:
                attempts.append(
                    ModelAttempt(
                        attempt=attempt_number,
                        status="budget_error",
                        raw_output_ref=raw_ref,
                        error=budget_error,
                        usage=usage,
                    )
                )
                raise ModelCallError(budget_error, attempts)
            try:
                normalized, changes = self._normalize(response.content, output_type)
            except (json.JSONDecodeError, TypeError, ValueError) as error:
                attempts.append(
                    ModelAttempt(
                        attempt=attempt_number,
                        status="format_error",
                        raw_output_ref=raw_ref,
                        error=str(error),
                        usage=usage,
                    )
                )
                last_error = str(error)
                continue
            try:
                output = (
                    normalized
                    if isinstance(normalized, output_type)
                    else output_type.model_validate(normalized)
                )
            except ValidationError as error:
                attempts.append(
                    ModelAttempt(
                        attempt=attempt_number,
                        status="schema_error",
                        raw_output_ref=raw_ref,
                        normalization_log=changes,
                        error=str(error),
                        usage=usage,
                    )
                )
                last_error = str(error)
                continue
            attempts.append(
                ModelAttempt(
                    attempt=attempt_number,
                    status="valid",
                    raw_output_ref=raw_ref,
                    normalization_log=changes,
                    usage=usage,
                )
            )
            aggregate = ModelUsage(
                input_tokens=total_input,
                output_tokens=total_output,
                latency_ms=total_latency,
                estimated_cost=total_cost,
                provider_request_id=usage.provider_request_id,
                provider_model_id=usage.provider_model_id,
                provider_model_version=usage.provider_model_version,
            )
            return GatewayResult(output, attempts, raw_ref, changes, aggregate)
        raise ModelCallError(last_error, attempts, last_exception) from last_exception

    @staticmethod
    def _budget_error(
        input_tokens: int,
        output_tokens: int,
        estimated_cost: float,
        *,
        token_limit: int | None,
        cost_limit: float | None,
    ) -> str | None:
        if token_limit is not None and input_tokens + output_tokens > token_limit:
            return "model call exceeded its total token budget"
        if cost_limit is not None and estimated_cost > cost_limit:
            return "model call exceeded its estimated cost budget"
        return None

    @staticmethod
    def _serialize(content: str | dict[str, Any] | BaseModel) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, BaseModel):
            return content.model_dump_json()
        return json.dumps(content, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _normalize(
        content: str | dict[str, Any] | BaseModel,
        output_type: type[T],
    ) -> tuple[dict[str, Any] | BaseModel, list[str]]:
        if isinstance(content, output_type):
            return content, []
        if isinstance(content, BaseModel):
            value: Any = content.model_dump(mode="json")
        elif isinstance(content, str):
            value = json.loads(content)
        else:
            value = dict(content)
        if not isinstance(value, dict):
            raise TypeError("model output must be a JSON object")
        changes: list[str] = []
        # The only semantic-free compatibility repair retained from the old adapter.
        if (
            output_type.__name__ == "NextStep"
            and value.get("kind") == "report"
            and value.get("expected_observation") is None
        ):
            value["expected_observation"] = "Report observed evidence and remaining gaps."
            changes.append("filled_null_report_expected_observation")
        return value, changes
