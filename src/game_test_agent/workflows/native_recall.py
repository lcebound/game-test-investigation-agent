"""Bounded, investigation-local references over existing authoritative storage."""
from __future__ import annotations

import hashlib
import re

from pydantic import BaseModel, ConfigDict, Field, model_validator

from game_test_agent.storage import canonical_json


def utf8_prefix(text: str, max_bytes: int) -> str:
    return text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


def input_card(row: dict, max_bytes: int = 1200) -> dict:
    content = row["payload"]["content"]
    preview = utf8_prefix(content, max_bytes)
    return {
        "command_id": row["command_id"],
        "created_at": row["created_at"],
        "content": preview,
        "truncated": preview != content,
        "total_bytes": len(content.encode("utf-8")),
        "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "read_ref": "input/" + row["command_id"],
    }


class ContextRead(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ref: str = Field(min_length=1, max_length=512)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=10, ge=1, le=20)
    byte_offset: int = Field(default=0, ge=0)
    max_bytes: int = Field(default=4096, ge=128, le=4096)
    query: str | None = Field(default=None, max_length=256)
    expected_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    pointer: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def coherent_pointer(self):
        if self.pointer is not None:
            if (not self.ref.startswith("evidence/") or self.byte_offset or self.query
                    or self.pointer and not self.pointer.startswith("/")
                    or re.search(r"~(?![01])", self.pointer)
                    or self.pointer.count("/") > 32):
                raise ValueError("JSON Pointer requires evidence, no query/byte_offset, and a valid bounded path.")
        return self


def input_page(repository, investigation_id: str, request: ContextRead) -> dict:
    """Read only this investigation's inputs, preserving UTF-8 byte positions."""
    rows = repository.list_user_inputs(investigation_id)
    if request.ref == "inputs":
        if request.byte_offset or request.expected_sha256:
            raise ValueError("Use a specific input reference for content reads.")
        query = (request.query or "").casefold()
        cursor, scanned = min(request.offset, len(rows)), 0
        payload = {"resource_kind": "input_directory", "items": [],
                   "total_items": len(rows), "next_offset": None, "read_ref": "inputs",
                   "searched_from_offset": request.offset, "complete": False, "scanned_items": 100}
        while cursor < len(rows) and cursor - request.offset < 100:
            row = rows[cursor]
            raw = row["payload"]["content"]
            size = len(raw.encode())
            if scanned + size > 4 * 1024 * 1024:
                break
            if not query or query in raw.casefold():
                payload["items"].append(input_card(row, 80))
                payload["next_offset"] = cursor + 1 if cursor + 1 < len(rows) else None
                if len(canonical_json(payload).encode()) > request.max_bytes:
                    payload["items"].pop()
                    if not payload["items"]:
                        raise ValueError("max_bytes cannot fit one input card.")
                    break
            scanned += size
            cursor += 1
            if len(payload["items"]) >= request.limit:
                break
        payload["next_offset"] = cursor if cursor < len(rows) else None
        payload["scanned_items"] = max(0, cursor - request.offset)
        payload["complete"] = cursor == len(rows)
        if len(canonical_json(payload).encode()) > request.max_bytes:
            raise ValueError("max_bytes cannot fit the directory metadata.")
        return payload
    kind, separator, identity = request.ref.partition("/")
    if kind != "input" or not separator or not identity or request.offset:
        raise ValueError("Unknown context reference.")
    row = next((item for item in rows if item["command_id"] == identity), None)
    if row is None:
        raise PermissionError("Input is unavailable in this investigation.")
    raw = row["payload"]["content"].encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    if request.expected_sha256 and request.expected_sha256 != digest:
        raise ValueError("Input reference is stale.")
    start = request.byte_offset
    if start > len(raw) or start < len(raw) and raw[start] & 0xC0 == 0x80:
        raise ValueError("Input byte offset must be a valid UTF-8 boundary.")
    if request.query:
        query = request.query.encode("utf-8")
        if len(query) > request.max_bytes:
            raise ValueError("max_bytes must fit the literal query.")
        found = raw.find(query, start)
        if found < 0:
            return {"resource_kind": "user_input", "input_id": identity,
                    "read_ref": request.ref, "sha256": digest, "matched": False,
                    "total_bytes": len(raw), "searched_from_byte": start}
        start = max(start, found - min(300, request.max_bytes - len(query)))
        while start < len(raw) and raw[start] & 0xC0 == 0x80:
            start += 1
    text = raw[start:start + request.max_bytes].decode("utf-8", errors="ignore")
    end = start + len(text.encode("utf-8"))
    return {
        "resource_kind": "user_input", "input_id": identity,
        "read_ref": request.ref, "created_at": row["created_at"],
        "result": text, "matched": True, "query": request.query or None,
        "byte_offset": start, "next_byte_offset": end if end < len(raw) else None,
        "total_bytes": len(raw), "truncated": end < len(raw) or start > 0,
        "sha256": digest,
        "excerpt_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def diagnostic_requested(objective: str, inputs: list[dict]) -> bool:
    """Use full authoritative follow-ups, not only their in-window previews."""
    pattern = r"crash|frame|performance|cpu|gpu|崩溃|帧|性能|耗时|卡顿"
    return any(re.search(pattern, text, re.I) for text in [
        objective, *(row["payload"]["content"] for row in inputs),
    ])
