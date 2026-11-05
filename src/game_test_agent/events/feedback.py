"""Run feedback is untrusted data, never a user command or a permission grant."""
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from game_test_agent.contracts.models import RunRef


class RunSubscription(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    source: str = Field(min_length=1, max_length=128)
    run_ref: RunRef

    @field_validator("run_ref")
    @classmethod
    def bounded_run_identity(cls, value):
        if any(not getattr(value, key) or len(getattr(value, key)) > bound
               for key, bound in (("provider_id", 128), ("project_id", 128), ("run_id", 256))):
            raise ValueError("run reference exceeds supported identity bounds")
        return value


class RunFeedbackData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_ref: RunRef
    result_revision: int = Field(ge=1)
    kind: Literal["progress", "failed", "recovered", "counterevidence", "phase"]
    status: str = Field(min_length=1, max_length=64)
    summary: str = Field(default="", max_length=2000)
    details: dict[str, Any] = Field(default_factory=dict)

    _bounded_run_identity = field_validator("run_ref")(RunSubscription.bounded_run_identity.__func__)


class RunFeedbackEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"] = "1.0"
    event_type: Literal["game_test.run.feedback"]
    event_id: str = Field(min_length=1, max_length=256)
    source: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    occurred_at: datetime
    published_at: datetime
    root_event_id: str = Field(min_length=1, max_length=256)
    causation_id: str | None = Field(default=None, max_length=256)
    origin: Literal["platform"]
    data: RunFeedbackData

    @field_validator("occurred_at", "published_at")
    @classmethod
    def timezone_required(cls, value):
        if value.tzinfo is None:
            raise ValueError("feedback timestamps need an explicit timezone")
        return value
