"""Create or verify a code/prompt/model fingerprint for live acceptance trials."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
from importlib.metadata import version
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODEL_ENV = (
    "GTA_ENABLE_MODEL",
    "GTA_MODEL",
    "GTA_MODEL_API",
    "GTA_MODEL_OUTPUT_MODE",
    "GTA_MODEL_EXTRA_BODY_JSON",
    "GTA_MODEL_TIMEOUT_SECONDS",
    "GTA_MODEL_MAX_OUTPUT_TOKENS",
    "GTA_MODEL_CONTEXT_WINDOW_TOKENS",
    "GTA_MODEL_REASONING_EFFORT",
    "GTA_WORKFLOW_BACKEND",
    "OPENAI_BASE_URL",
    "GTA_MCP_PROVIDERS_JSON",
)


def fingerprint() -> dict:
    files = [ROOT / "pyproject.toml"]
    for directory in ("src", "scripts", "tests"):
        files.extend(
            path
            for path in (ROOT / directory).rglob("*")
            if path.is_file() and path.suffix in {".py", ".json"}
        )
    records = [
        {
            "path": path.relative_to(ROOT).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in sorted(files)
    ]
    material = json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
    prompt_paths = (
        ROOT / "src/game_test_agent/agents/investigator.py",
        ROOT / "src/game_test_agent/planning/action_planner.py",
    )
    prompt_material = b"\n".join(path.read_bytes() for path in prompt_paths)
    return {
        "code_sha256": hashlib.sha256(material).hexdigest(),
        "prompt_sha256": hashlib.sha256(prompt_material).hexdigest(),
        "file_count": len(records),
        "model_configuration": {name: os.getenv(name, "") for name in MODEL_ENV},
        "runtime_versions": {
            "python": platform.python_version(),
            **{
                package: version(package)
                for package in ("openai", "openai-agents", "pydantic", "httpx")
            },
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["create", "verify"])
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    current = fingerprint()
    if args.mode == "create":
        if args.manifest.exists():
            raise FileExistsError(args.manifest)
        args.manifest.write_text(
            json.dumps(current, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
    else:
        expected = json.loads(args.manifest.read_text(encoding="utf-8"))
        if current != expected:
            raise SystemExit(
                "acceptance freeze mismatch: code, prompt, model config or runtime changed"
            )
    print(
        json.dumps(
            {
                "code_sha256": current["code_sha256"],
                "prompt_sha256": current["prompt_sha256"],
                "file_count": current["file_count"],
                "mode": args.mode,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
