"""Verify the standalone Windows PSEC sandbox and pin its runtime configuration."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from game_test_agent.analysis.windows_runner import WindowsSandboxRunner  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-path", type=Path, default=Path(sys.executable))
    parser.add_argument("--runner-root", type=Path, default=ROOT / "data/local/windows-sandbox-runner")
    parser.add_argument("--config", type=Path, default=ROOT / "data/local/sandbox.env.json")
    args = parser.parse_args()
    runner = WindowsSandboxRunner(None, args.runner_root, "local-windows-psec", "",
                                 python_path=args.python_path)
    runner.runtime_digest = runner.fingerprint()
    asyncio.run(runner.verify())
    config = {
        "GTA_SANDBOX_BACKEND": "windows", "GTA_SANDBOX_RUNTIME": "psec",
        "GTA_SANDBOX_APPROVAL_KEY_FILE": str(ROOT / "data/local/code-approval.key"),
        "GTA_SANDBOX_RUNTIME_DIGEST": runner.runtime_digest,
        "GTA_SANDBOX_PYTHON_PATH": str(runner.python),
        "GTA_SANDBOX_RUNNER_IDS": runner.runner_id,
        "GTA_SANDBOX_RUNNER_ROOT": str(runner.root),
        "GTA_SANDBOX_MAX_REPAIRS": "2",
    }
    args.config.parent.mkdir(parents=True, exist_ok=True)
    if args.config.exists():
        previous = json.loads(args.config.read_text(encoding="utf-8-sig"))
        config.update({k: v for k, v in previous.items() if k.startswith("GTA_SANDBOX_MAX_")})
        backup = args.config.with_name(args.config.name + ".before-windows-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
        shutil.copyfile(args.config, backup)
    args.config.write_text(json.dumps(config, indent=2), encoding="utf-8")
    print(json.dumps({"config": str(args.config), "runtime_digest": runner.runtime_digest,
                      "isolation_kind": runner.isolation_kind, "verified": True}))


if __name__ == "__main__":
    main()
