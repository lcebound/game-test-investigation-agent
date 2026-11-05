"""Windows PSEC process creation and Job Object resource supervision."""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
import os
import subprocess

try:
    from .windows_security import WindowsSecurityEnvironment
except ImportError:  # Detached trusted worker imports this file as a sibling.
    from windows_security import WindowsSecurityEnvironment


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class BASIC_LIMITS(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong), ("LimitFlags", w.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", w.DWORD), ("Affinity", ctypes.c_size_t),
                ("PriorityClass", w.DWORD), ("SchedulingClass", w.DWORD)]


class EXTENDED_LIMITS(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", BASIC_LIMITS), ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class ACCOUNTING(ctypes.Structure):
    _fields_ = [("TotalUserTime", ctypes.c_longlong), ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong), ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", w.DWORD), ("TotalProcesses", w.DWORD),
                ("ActiveProcesses", w.DWORD), ("TotalTerminatedProcesses", w.DWORD)]


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", w.DWORD), ("lpSecurityDescriptor", w.LPVOID), ("bInheritHandle", w.BOOL)]


class STARTUPINFO(ctypes.Structure):
    _fields_ = [("cb", w.DWORD), ("lpReserved", w.LPWSTR), ("lpDesktop", w.LPWSTR),
                ("lpTitle", w.LPWSTR), ("dwX", w.DWORD), ("dwY", w.DWORD),
                ("dwXSize", w.DWORD), ("dwYSize", w.DWORD), ("dwXCountChars", w.DWORD),
                ("dwYCountChars", w.DWORD), ("dwFillAttribute", w.DWORD),
                ("dwFlags", w.DWORD), ("wShowWindow", w.WORD), ("cbReserved2", w.WORD),
                ("lpReserved2", ctypes.POINTER(w.BYTE)), ("hStdInput", w.HANDLE),
                ("hStdOutput", w.HANDLE), ("hStdError", w.HANDLE)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", w.HANDLE), ("hThread", w.HANDLE),
                ("dwProcessId", w.DWORD), ("dwThreadId", w.DWORD)]


class WindowsJobProcess:
    """Confirm PSEC and Job membership before resuming any program."""

    def __init__(self, arguments, *, env, cwd, name, memory_mb, processes, cpu_seconds, security_policy,
                 private_console=False):
        if os.name != "nt":
            raise RuntimeError("native Windows execution requires Windows")
        import msvcrt
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        api = self.api
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
            "SetInformationJobObject": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
            "QueryInformationJobObject": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p], w.BOOL),
            "CreatePipe": ([ctypes.POINTER(w.HANDLE), ctypes.POINTER(w.HANDLE), ctypes.POINTER(SECURITY_ATTRIBUTES), w.DWORD], w.BOOL),
            "SetHandleInformation": ([w.HANDLE, w.DWORD, w.DWORD], w.BOOL),
            "CreateFileW": ([w.LPCWSTR, w.DWORD, w.DWORD, ctypes.POINTER(SECURITY_ATTRIBUTES), w.DWORD, w.DWORD, w.HANDLE], w.HANDLE),
            "CreateProcessW": ([w.LPCWSTR, w.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, w.BOOL, w.DWORD, ctypes.c_void_p, w.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(PROCESS_INFORMATION)], w.BOOL),
            "AssignProcessToJobObject": ([w.HANDLE, w.HANDLE], w.BOOL),
            "IsProcessInJob": ([w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)], w.BOOL),
            "TerminateJobObject": ([w.HANDLE, w.UINT], w.BOOL),
            "TerminateProcess": ([w.HANDLE, w.UINT], w.BOOL),
            "WaitForSingleObject": ([w.HANDLE, w.DWORD], w.DWORD),
            "GetExitCodeProcess": ([w.HANDLE, ctypes.POINTER(w.DWORD)], w.BOOL),
            "ResumeThread": ([w.HANDLE], w.DWORD),
            "CloseHandle": ([w.HANDLE], w.BOOL),
        }
        for key, (args, result) in signatures.items():
            function = getattr(api, key)
            function.argtypes, function.restype = args, result
        self.job = api.CreateJobObjectW(None, name)
        self.process = None
        self.security_environment = None
        if not self.job:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = EXTENDED_LIMITS()
        limits.BasicLimitInformation.LimitFlags = 0x2000 | 0x8 | 0x200 | 0x4
        limits.BasicLimitInformation.ActiveProcessLimit = processes
        limits.BasicLimitInformation.PerJobUserTimeLimit = cpu_seconds * 10_000_000
        limits.JobMemoryLimit = memory_mb * 1024 * 1024
        attributes = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), None, True)
        reads, writes = [], []
        null = None
        attributes_storage = None
        try:
            self.security_environment = WindowsSecurityEnvironment(security_policy)
            self._check(api.SetInformationJobObject(self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
            for _ in range(2):
                read, write = w.HANDLE(), w.HANDLE()
                self._check(api.CreatePipe(ctypes.byref(read), ctypes.byref(write), ctypes.byref(attributes), 0))
                reads.append(read.value)
                writes.append(write.value)
                self._check(api.SetHandleInformation(read, 1, 0))
            null = api.CreateFileW("NUL", 0x80000000, 3, ctypes.byref(attributes), 3, 0, None)
            if null == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            startup, attributes_storage = self.security_environment.startup(
                api, STARTUPINFO, (null, writes[0], writes[1]), self.job)
            startup.info.dwFlags |= 1  # STARTF_USESHOWWINDOW
            startup.info.wShowWindow = 0  # SW_HIDE, including brokered child creation
            information = PROCESS_INFORMATION()
            command = ctypes.create_unicode_buffer(subprocess.list2cmdline(arguments))
            environment = ctypes.create_unicode_buffer("\0".join(f"{k}={v}" for k, v in sorted(env.items())) + "\0\0")
            self._check(api.CreateProcessW(str(arguments[0]), command, None, None, True,
                0x4 | 0x400 | (0x10 if private_console else 0x08000000) | 0x80000,
                environment, str(cwd), ctypes.byref(startup), ctypes.byref(information)))
            self.process, self.pid = information.hProcess, information.dwProcessId
            try:
                # Some PSEC builds broker creation without honoring the caller's
                # Job-list attribute. Confirm membership while still suspended;
                # never resume unaccounted code or trust an empty Job's metrics.
                member = w.BOOL()
                self._check(api.IsProcessInJob(self.process, self.job, ctypes.byref(member)))
                if not member.value:
                    self._check(api.AssignProcessToJobObject(self.job, self.process))
                    self._check(api.IsProcessInJob(self.process, self.job, ctypes.byref(member)))
                    if not member.value:
                        raise RuntimeError("native resource Job membership was not enforced")
                if api.ResumeThread(information.hThread) == 0xFFFFFFFF:
                    raise ctypes.WinError(ctypes.get_last_error())
            except BaseException:
                api.TerminateProcess(self.process, 1)
                raise
            finally:
                api.CloseHandle(information.hThread)
            self.stdout = os.fdopen(msvcrt.open_osfhandle(reads.pop(0), os.O_RDONLY | os.O_BINARY), "rb", buffering=0)
            self.stderr = os.fdopen(msvcrt.open_osfhandle(reads.pop(0), os.O_RDONLY | os.O_BINARY), "rb", buffering=0)
        except BaseException:
            self.close()
            raise
        finally:
            if attributes_storage:
                api.DeleteProcThreadAttributeList(attributes_storage[0])
            for handle in [*reads, *writes, null]:
                if handle:
                    api.CloseHandle(handle)

    @staticmethod
    def _check(value):
        if not value:
            raise ctypes.WinError(ctypes.get_last_error())

    def poll(self):
        if self.api.WaitForSingleObject(self.process, 0) == 258:
            return None
        code = w.DWORD()
        self._check(self.api.GetExitCodeProcess(self.process, ctypes.byref(code)))
        return code.value

    def terminate(self):
        self._check(self.api.TerminateJobObject(self.job, 1))

    def usage(self):
        limits, accounting = EXTENDED_LIMITS(), ACCOUNTING()
        self._check(self.api.QueryInformationJobObject(self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits), None))
        self._check(self.api.QueryInformationJobObject(self.job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None))
        return {"cpu_seconds": (accounting.TotalUserTime + accounting.TotalKernelTime) / 10_000_000,
                "peak_memory_mb": limits.PeakJobMemoryUsed / (1024*1024),
                "active_processes": accounting.ActiveProcesses}

    def close(self):
        if self.job:
            self.api.CloseHandle(self.job)
            self.job = None
        if self.process:
            self.api.CloseHandle(self.process)
            self.process = None
        if self.security_environment:
            self.security_environment.close()
            self.security_environment = None


def process_identity(pid: int) -> str | None:
    """Creation time protects recovery checks from Windows PID reuse."""
    if os.name != "nt":
        return None
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes, api.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
    api.GetProcessTimes.argtypes = [w.HANDLE, *([ctypes.POINTER(w.FILETIME)] * 4)]
    api.WaitForSingleObject.argtypes, api.WaitForSingleObject.restype = [w.HANDLE, w.DWORD], w.DWORD
    api.CloseHandle.argtypes = [w.HANDLE]
    handle = api.OpenProcess(0x1000 | 0x100000, False, pid)
    if not handle:
        return None
    try:
        if api.WaitForSingleObject(handle, 0) != 258:
            return None
        values = [w.FILETIME() for _ in range(4)]
        if not api.GetProcessTimes(handle, *(ctypes.byref(v) for v in values)):
            raise ctypes.WinError(ctypes.get_last_error())
        return str((values[0].dwHighDateTime << 32) | values[0].dwLowDateTime)
    finally:
        api.CloseHandle(handle)


def terminate_job(name: str) -> bool:
    """Stop only the deterministic named job belonging to this operation."""
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenJobObjectW.argtypes, api.OpenJobObjectW.restype = [w.DWORD, w.BOOL, w.LPCWSTR], w.HANDLE
    api.TerminateJobObject.argtypes, api.TerminateJobObject.restype = [w.HANDLE, w.UINT], w.BOOL
    api.CloseHandle.argtypes = [w.HANDLE]
    handle = api.OpenJobObjectW(0x8, False, name)
    if not handle:
        if ctypes.get_last_error() == 2:
            return True
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not api.TerminateJobObject(handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())
        return True
    finally:
        api.CloseHandle(handle)
