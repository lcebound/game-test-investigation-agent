"""Load a private local JSON environment file without evaluating shell code."""

import json
import os
import re
from pathlib import Path


def load_local_environment(path: Path | str | None = None) -> Path | None:
    selected = path or os.getenv("GTA_LOCAL_CONFIG_FILE")
    candidate = Path(selected or "data/local/workbench.env.json").resolve()
    if not candidate.is_file():
        if selected:
            raise ValueError("local configuration file is missing")
        return None
    if candidate.stat().st_size > 65536:
        raise ValueError("local configuration file exceeds the size limit")
    try:
        values = json.loads(candidate.read_text(encoding="utf-8-sig"))
    except (ValueError, UnicodeError) as error:
        raise ValueError("local configuration file is not valid UTF-8 JSON") from error
    if not isinstance(values, dict) or any(
        not isinstance(key, str)
        or re.fullmatch(r"[A-Z][A-Z0-9_]{1,127}", key) is None
        or not isinstance(value, str)
        for key, value in values.items()
    ):
        raise ValueError("local configuration must map environment names to strings")
    for key, value in values.items():
        os.environ.setdefault(key, value)
    os.environ["GTA_LOCAL_CONFIG_FILE"] = str(candidate)
    return candidate
