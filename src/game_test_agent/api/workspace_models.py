from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from game_test_agent.contracts.models import Budget, BuildRef, validate_provider_allowlist


class CodePermissionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["approve", "deny"]
    reason: str = Field(default="", max_length=2000)


class ConversationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    expected_revision: int = Field(ge=0)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    pinned: bool | None = None
    archived: bool | None = None

    @model_validator(mode="after")
    def has_changes(self):
        if all(getattr(self, key) is None for key in ["title", "pinned", "archived"]):
            raise ValueError("at least one conversation field is required")
        return self


class ConversationMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    content: str = Field(min_length=1, max_length=4000)
    expected_investigation_id: str = Field(min_length=1, max_length=96)
    budget: Budget | None = None
    build_ref: BuildRef | None = None
    constraints: dict[str, Any] | None = None
    acceptance_criteria: list[str] | None = Field(default=None, max_length=50)

    @model_validator(mode="after")
    def provider_allowlist(self):
        if self.constraints is not None:
            validate_provider_allowlist(self.constraints)
        return self


class ConversationRouteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    content: str = Field(min_length=1, max_length=4000)
    context_investigation_id: str | None = Field(default=None, min_length=1, max_length=96)
    bound_investigation_id: str | None = Field(default=None, min_length=1, max_length=96)
