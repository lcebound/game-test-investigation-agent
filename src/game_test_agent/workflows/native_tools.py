"""Progressively expose authorized MCP schemas without changing their contracts."""
import re
from pydantic import BaseModel, ConfigDict, Field


class ToolLookup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=300)
    limit: int = Field(default=3, ge=1, le=5)


def rank_tools(bindings, query):
    terms = set(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", query.lower()))
    def score(name):
        text = (name + " " + (bindings[name][1].description or "")).lower()
        return sum(term in text for term in terms)
    return sorted(bindings, key=lambda name: (-score(name), name))


def lookup_tools(bindings, memory, arguments):
    request = ToolLookup.model_validate_json(arguments)
    names = rank_tools(bindings, request.query)[:request.limit]
    memory.enabled_tools = list(dict.fromkeys(memory.enabled_tools + names))[-12:]
    return {"tools": [{"name": name, "description": bindings[name][1].description,
                        "parameters": bindings[name][1].input_schema} for name in names],
            "result": "These authorized schemas are enabled for the next model turn."}, False
