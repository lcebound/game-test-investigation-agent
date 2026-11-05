from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field


class EventSourcePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    scopes: list[str] = Field(default_factory=list)
    policy_profile: str = "bounded-diagnosis"
    allow_observation_investigations: bool = False
    max_observation_tasks: int = Field(default=4, ge=1, le=20)


def load_event_source_policies(raw_json: str) -> dict[tuple[str, str, str], EventSourcePolicy]:
    raw = json.loads(raw_json or "[]")
    if not isinstance(raw, list):
        raise ValueError("GTA_EVENT_SOURCE_POLICIES_JSON must contain a JSON array")
    result: dict[tuple[str, str, str], EventSourcePolicy] = {}
    for item in raw:
        policy = EventSourcePolicy.model_validate(item)
        key = (policy.source, policy.tenant_id, policy.project_id)
        if key in result:
            raise ValueError(f"duplicate event source policy for {key}")
        result[key] = policy
    return result
