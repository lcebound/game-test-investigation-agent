from __future__ import annotations

from typing import Any


_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "credentials",
    "fencing_token",
    "password",
    "private_key",
    "secret",
    "token",
}


def redact(value: Any) -> Any:
    """Recursively removes common credential fields before audit/trace persistence."""
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if str(key).lower() in _SENSITIVE_KEYS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    return value
