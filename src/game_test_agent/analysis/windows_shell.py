"""Trusted bootstrap executed INSIDE PSEC, never on the unconfined host.

A private hidden console sets UTF-8 before PowerShell initializes .NET Console.
This avoids changing system console settings or bypassing ConstrainedLanguage.
The PowerShell child inherits both PSEC and the kernel resource Job.
"""
from __future__ import annotations

import ctypes
import subprocess
import sys


if __name__ == "__main__":
    api = ctypes.WinDLL("kernel32", use_last_error=True, winmode=0x800)
    if not api.SetConsoleOutputCP(65001) or not api.SetConsoleCP(65001):
        raise ctypes.WinError(ctypes.get_last_error())
    bootstrap = "$ErrorActionPreference='Stop'; & $env:GTA_PROGRAM_FILE; if ($LASTEXITCODE) { exit $LASTEXITCODE }"
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    raise SystemExit(subprocess.call([sys.argv[1], "-NoLogo", "-NoProfile", "-NonInteractive",
                                     "-ExecutionPolicy", "Bypass", "-Command", bootstrap],
                                    startupinfo=startup))
