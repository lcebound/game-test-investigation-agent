"""Exact JSON-field locations over bounded immutable original evidence."""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from game_test_agent.evidence.assertions import _unique_object, json_pointer
from game_test_agent.storage import canonical_json
from game_test_agent.workflows.native_recall import utf8_prefix


def _reject_constant(_value):
    raise ValueError("Nonfinite JSON is unsupported.")


def _space(text, position):
    while position < len(text) and text[position] in " \t\r\n":
        position += 1
    return position


def _children(decoder, text, start):
    """Yield direct child tokens, parsed values and exact character spans."""
    is_object = text[start] == "{"
    position, index = _space(text, start + 1), 0
    while text[position] not in "}]":
        if is_object:
            token, position = decoder.raw_decode(text, position)
            position = _space(text, position)
            if text[position] != ":":
                raise ValueError("Invalid JSON object.")
            position = _space(text, position + 1)
        else:
            token = str(index)
        value, end = decoder.raw_decode(text, position)
        yield str(token), value, position, end
        index += 1
        position = _space(text, end)
        if text[position] == ",":
            position = _space(text, position + 1)
        else:
            break


def _kind(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return "string" if isinstance(value, str) else "number"


def read_json_field(raw: bytes, request, *, evidence_id, sha256, source_revision, source):
    """Return bounded original fragments, never a re-serialized pseudo-quote."""
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("JSON input exceeds 4 MiB.")
    if hashlib.sha256(raw).hexdigest() != sha256:
        raise ValueError("Evidence hash mismatch.")
    text = raw.decode("utf-8")
    decoder = json.JSONDecoder(object_pairs_hook=_unique_object, parse_float=Decimal,
                               parse_constant=_reject_constant)
    start = _space(text, 0)
    root, end = decoder.raw_decode(text, start)
    if _space(text, end) != len(text):
        raise ValueError("Trailing JSON data.")
    value = json_pointer(root, request.pointer)
    # Locate the selected value without re-encoding escapes, numeric precision,
    # whitespace, or object key order. The full decode rejected duplicate keys.
    for part in request.pointer.split("/")[1:] if request.pointer else []:
        part = part.replace("~1", "/").replace("~0", "~")
        for token, _child, left, right in _children(decoder, text, start):
            if token == part:
                start, end = left, right
                break
        else:
            raise ValueError("JSON field is unavailable.")
    kind = _kind(value)
    if request.offset and kind not in {"object", "array"}:
        raise ValueError("offset is only valid for containers.")
    payload = {
        "resource_kind": "json_field", "evidence_id": evidence_id,
        "read_ref": "evidence/" + evidence_id, "pointer": request.pointer,
        "value_type": kind, "sha256": sha256, "source_revision": source_revision,
        "source": source, "total_bytes": len(raw),
        "byte_offset": len(text[:start].encode()),
        "value_bytes": len(text[start:end].encode()), "matched": True,
        "result": "", "truncated": True,
        "excerpt_sha256": "0" * 64,
        "next_byte_offset": len(text[:start].encode()),
        "scope": "Only this field in this saved artifact; not remote-page or run completeness.",
    }
    # Reserve the source/locator envelope first; max_bytes bounds the entire
    # result, including structure cards, rather than just each displayed value.
    if len(canonical_json(payload).encode()) + 32 > request.max_bytes:
        raise ValueError("max_bytes cannot fit the field metadata.")
    budget = request.max_bytes - len(canonical_json(payload).encode()) - 32
    fragment = utf8_prefix(text[start:end], min(256, budget) if kind in {"object", "array"} else budget)
    payload["result"] = fragment
    fragment_end = payload["byte_offset"] + len(fragment.encode())
    complete = len(fragment.encode()) == payload["value_bytes"]
    payload["truncated"] = not complete
    payload["next_byte_offset"] = None if complete else fragment_end
    if kind in {"object", "array"}:
        payload.update(entries=[], total_items=len(value), next_offset=None)
        for index, (token, child, left, right) in enumerate(_children(decoder, text, start)):
            if index < request.offset:
                continue
            pointer = request.pointer + "/" + token.replace("~", "~0").replace("/", "~1")
            entry = {
                "pointer": pointer if len(pointer) <= 500 and pointer.count("/") <= 32 else None,
                "value_type": _kind(child), "byte_offset": len(text[:left].encode()),
                "value_bytes": len(text[left:right].encode()),
            }
            if isinstance(child, (dict, list)):
                entry["total_items"] = len(child)
            else:
                entry["literal_preview"] = utf8_prefix(text[left:right], 80)
                entry["truncated"] = entry["literal_preview"] != text[left:right]
            payload["entries"].append(entry)
            payload["next_offset"] = index + 1 if index + 1 < len(value) else None
            if len(canonical_json(payload).encode()) > request.max_bytes:
                payload["entries"].pop()
                payload["next_offset"] = index
                if not payload["entries"]:
                    raise ValueError("max_bytes cannot fit a structure entry.")
                break
            if len(payload["entries"]) >= request.limit:
                break
    # JSON escaping can cost more bytes than the raw fragment. Trim only that
    # fragment, leaving its position and explicit partial marker trustworthy.
    while len(canonical_json(payload).encode()) > request.max_bytes and payload["result"]:
        payload["result"] = utf8_prefix(payload["result"], len(payload["result"].encode()) // 2)
        payload["truncated"] = True
        payload["next_byte_offset"] = payload["byte_offset"] + len(payload["result"].encode())
    if len(canonical_json(payload).encode()) > request.max_bytes:
        raise ValueError("max_bytes cannot fit the result.")
    payload["excerpt_sha256"] = hashlib.sha256(payload["result"].encode()).hexdigest()
    if len(canonical_json(payload).encode()) > request.max_bytes:
        raise ValueError("max_bytes cannot fit the excerpt hash.")
    return payload
