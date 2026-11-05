"""Bounded Windows-only desktop visibility check for a real PSEC shell job."""

from __future__ import annotations
import argparse
import asyncio
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main(output):
    if os.name != "nt":
        raise RuntimeError("Windows visibility validation requires Windows")
    from game_test_agent.analysis.windows_runner import WindowsSandboxRunner, clean_environment
    from game_test_agent.analysis.windows_job import WindowsJobProcess

    config = json.loads(Path("data/local/sandbox.env.json").read_text(encoding="utf-8-sig"))
    api = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    api.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    api.IsWindowVisible.argtypes = [wintypes.HWND]
    api.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

    def visible_consoles():
        windows = set()

        def visit(handle, parameter):
            name = ctypes.create_unicode_buffer(256)
            api.GetClassNameW(handle, name, len(name))
            if api.IsWindowVisible(handle) and name.value in {
                "ConsoleWindowClass",
                "CASCADIA_HOSTING_WINDOW_CLASS",
            }:
                windows.add(int(handle))
            return True

        if not api.EnumWindows(callback_type(visit), 0):
            raise ctypes.WinError(ctypes.get_last_error())
        return windows

    baseline = visible_consoles()
    detected = set()
    stop = threading.Event()
    samples = [0]
    monitor_errors = []

    def monitor():
        try:
            while not stop.is_set():
                detected.update(visible_consoles() - baseline)
                samples[0] += 1
                stop.wait(0.005)
        except Exception as error:
            monitor_errors.append(type(error).__name__)

    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="gta-visible-window-") as folder:
            directory = Path(folder)
            (directory / "inputs").mkdir()
            work = directory / "work"
            work.mkdir()
            runner = WindowsSandboxRunner(
                None,
                directory,
                "visibility-check",
                config["GTA_SANDBOX_RUNTIME_DIGEST"],
                python_path=Path(config["GTA_SANDBOX_PYTHON_PATH"]),
            )
            asyncio.run(runner.verify())
            bootstrap = directory / "inputs/shell-bootstrap.py"
            bootstrap.write_bytes(
                Path("src/game_test_agent/analysis/windows_shell.py").read_bytes()
            )
            program = directory / "inputs/program.ps1"
            program.write_text(
                "Write-Output 'hidden-window-probe'; Start-Sleep -Seconds 2", encoding="utf-8-sig"
            )
            environment = clean_environment(work)
            environment["GTA_PROGRAM_FILE"] = str(program)
            process = WindowsJobProcess(
                [str(runner.python), "-I", "-X", "utf8", str(bootstrap), str(runner.powershell)],
                env=environment,
                cwd=work,
                name="Local\\gta-window-check-" + uuid.uuid4().hex,
                memory_mb=512,
                processes=16,
                cpu_seconds=20,
                security_policy=runner._policy(directory),
                private_console=True,
            )
            try:
                deadline = time.monotonic() + 20
                while process.poll() is None:
                    if time.monotonic() >= deadline:
                        process.terminate()
                        raise TimeoutError("visibility job timed out")
                    time.sleep(0.02)
                code = process.poll()
                stdout = process.stdout.read().decode("utf-8")
                stderr = process.stderr.read().decode("utf-8")
                assert code == 0 and stdout.strip() == "hidden-window-probe", (code, stderr)
            finally:
                process.close()
    finally:
        stop.set()
        thread.join(2)
    result = {
        "passed": not detected and not monitor_errors,
        "new_visible_console_windows": len(detected),
        "samples": samples[0],
        "monitor_errors": monitor_errors,
        "limitation": "One bounded desktop observation; not a guarantee against arbitrary programs creating their own UI.",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))
    if not result["passed"]:
        raise RuntimeError("visible console or observation failure detected")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("data/local/sandbox-window-check.json"))
    main(parser.parse_args().output)
