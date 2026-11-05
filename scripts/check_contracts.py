"""CI check: current v2 schemas match code; published v1 snapshots remain readable."""
from __future__ import annotations

import json
import hashlib
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from export_schemas import V2_MODELS  # noqa: E402


def main():
    root = ROOT / "src/game_test_agent/contracts"
    for line in (root / "v1.sha256").read_text(encoding="utf-8").splitlines():
        expected_hash, name = line.split("  ", 1)
        if hashlib.sha256((root / "v1" / name).read_bytes()).hexdigest() != expected_hash:
            raise SystemExit("Published v1 contract changed: " + name)
    for path in (root / "v1").glob("*.json"):
        Draft202012Validator.check_schema(json.loads(path.read_text(encoding="utf-8")))
    issues = []
    for name, model in V2_MODELS.items():
        expected = model.model_json_schema(by_alias=True)
        expected.update({"$id": f"contract://v2/{name}", "$schema": "https://json-schema.org/draft/2020-12/schema"})
        path = root / "v2" / f"{name}.json"
        actual = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        if expected != actual:
            issues.append(name)
    if issues:
        raise SystemExit("Stale v2 schemas; run scripts/export_schemas.py: " + ", ".join(issues))
    print("Versioned contracts match current code.")


if __name__ == "__main__":
    main()
