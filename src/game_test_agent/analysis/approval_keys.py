"""A separate local review credential; never included in tool/model context."""
from __future__ import annotations

import os
from pathlib import Path
import re
import secrets


def approval_key_path(settings) -> Path:
    return settings.sandbox_approval_key_file or settings.database_path.parent / "code-approval.key"


def local_approval_key(settings) -> str | None:
    if settings.auth_mode != "dev_headers":
        return None
    path = approval_key_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(handle, "w", encoding="ascii") as stream:
            stream.write(secrets.token_urlsafe(48))
    key = path.read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{64}", key):
        raise ValueError("invalid local code approval key; restore its protected key file")
    return key
