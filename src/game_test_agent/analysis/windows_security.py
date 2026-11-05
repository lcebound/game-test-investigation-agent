"""Direct Windows PSEC bindings. No executable, SDK runtime or model dependency.

The wire contract is Microsoft's ProcessSecurityEnvironment.fbs (MIT), see
docs/windows-sandbox-references.md. This module implements its small required
subset using Python's standard library; unsupported contracts fail closed.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
import os
from pathlib import Path
import struct


class _FlatBuffer:
    """Forward-only writer for the Windows PSEC schema, not a general serializer."""

    def __init__(self):
        self.data = bytearray(b"\0\0\0\0PSEC")

    def align(self, boundary):
        self.data.extend(b"\0" * (-len(self.data) % boundary))

    def string(self, value):
        self.align(4)
        location = len(self.data)
        encoded = value.encode("utf-8")
        self.data.extend(struct.pack("<I", len(encoded)) + encoded + b"\0")
        return location

    def vector(self, values):
        self.align(4)
        location = len(self.data)
        self.data.extend(struct.pack("<I", len(values)) + b"\0" * (4*len(values)))
        for index, value in enumerate(values):
            pointer = location + 4 + index*4
            target = self.string(value)
            struct.pack_into("<I", self.data, pointer, target-pointer)
        return location

    def table(self, fields):
        self.align(2)
        vtable = len(self.data)
        self.data.extend(b"\0" * (4 + 2*len(fields)))
        self.align(8)
        location = len(self.data)
        positions, size = [], 4
        for field in fields:
            if field is None:
                positions.append(0)
                continue
            kind, _ = field
            width, alignment = {"ref":(4,4), "version":(4,2), "bool":(1,1), "u64":(8,8)}[kind]
            size += -size % alignment
            positions.append(size)
            size += width
        size += -size % 8
        self.data.extend(b"\0" * size)
        struct.pack_into("<i", self.data, location, location-vtable)
        struct.pack_into("<HH", self.data, vtable, 4+2*len(fields), size)
        for index, (field, offset) in enumerate(zip(fields, positions)):
            struct.pack_into("<H", self.data, vtable+4+index*2, offset)
            if field is None:
                continue
            kind, value = field
            pointer = location+offset
            if kind == "ref":
                target = value()
                struct.pack_into("<I", self.data, pointer, target-pointer)
            elif kind == "version":
                struct.pack_into("<HH", self.data, pointer, *value)
            else:
                struct.pack_into("<Q" if kind == "u64" else "<B", self.data, pointer, value)
        return location


def compile_policy(policy: dict) -> bytes:
    """Encode only server-owned allowlists and a closed/open per-job network bit."""
    if set(policy) != {"read_only", "read_write", "deny", "network"}:
        raise ValueError("native sandbox policy has missing or unknown fields")
    if type(policy["network"]) is not bool:
        raise ValueError("native network grant must be a boolean")
    for key in ("read_only", "read_write", "deny"):
        if not isinstance(policy[key], list) or not policy[key]:
            raise ValueError("native filesystem policy requires explicit paths")
        if any(not isinstance(path, str) or not Path(path).is_absolute() or "\0" in path for path in policy[key]):
            raise ValueError("native filesystem policy requires absolute local paths")
    builder = _FlatBuffer()
    online = policy["network"]
    capabilities = "registryRead"
    if online:
        capabilities += ",internetClient,privateNetworkClientServer,networkLoopback"
    def network():
        return builder.table([None,
            ("ref", lambda: builder.table([("bool", int(online)), None, None])),
            None,
            ("ref", lambda: builder.table([("bool", 0), ("bool", int(online))])),
        ])
    root = builder.table([
        ("version", (1,1)), ("ref", lambda: builder.string(capabilities)),
        # Windows PowerShell/.NET initializes user32 even for noninteractive
        # scripts. Filesystem and network confinement remain enforced by PSEC;
        # do not claim Win32k syscall lockdown for this interpreter profile.
        ("bool", 0), ("u64", 0),
        ("ref", lambda: builder.vector(policy["read_write"])),
        ("ref", lambda: builder.vector(policy["read_only"])),
        ("ref", lambda: builder.vector(policy["deny"])),
        ("ref", network), None,
    ])
    struct.pack_into("<I", builder.data, 0, root)
    return bytes(builder.data)


def _check_hresult(result, operation):
    if result < 0:
        raise RuntimeError(f"{operation} failed (HRESULT 0x{result & 0xffffffff:08x}); no fallback allowed")


class WindowsSecurityEnvironment:
    def __init__(self, policy):
        if os.name != "nt":
            raise RuntimeError("native Windows security environments require Windows")
        # LOAD_LIBRARY_SEARCH_SYSTEM32 prevents DLL search-order hijacking.
        self.api = ctypes.WinDLL("processmodel.dll", use_last_error=True, winmode=0x800)
        self.handle = w.HANDLE()
        for name, args, result in [
            ("QueryProcessSecurityEnvironmentSupport", [ctypes.POINTER(ctypes.c_uint64)], ctypes.c_long),
            ("IsProcessSecurityEnvironmentVersionSupported", [w.DWORD,ctypes.POINTER(w.BYTE),ctypes.POINTER(w.DWORD)], ctypes.c_long),
            ("CreateProcessSecurityEnvironment", [ctypes.c_void_p,w.DWORD,w.DWORD,ctypes.POINTER(w.HANDLE)], ctypes.c_long),
            ("CloseProcessSecurityEnvironment", [w.HANDLE], None),
        ]:
            function = getattr(self.api, name)
            function.argtypes, function.restype = args, result
        flags, available, minor = ctypes.c_uint64(), w.BYTE(), w.DWORD()
        _check_hresult(self.api.QueryProcessSecurityEnvironmentSupport(ctypes.byref(flags)), "PSEC support query")
        _check_hresult(self.api.IsProcessSecurityEnvironmentVersionSupported(1, ctypes.byref(available), ctypes.byref(minor)), "PSEC version query")
        if not available.value or minor.value < 1 or flags.value & 0x9 != 0x9:
            raise RuntimeError("Windows PSEC 1.1 with filesystem deny and network ingress is required")
        self.specification = compile_policy(policy)
        blob = ctypes.create_string_buffer(self.specification)
        # TERMINATE_ON_CLOSE also covers a supervisor crash between suspended
        # process creation and confirmed resource Job assignment.
        _check_hresult(self.api.CreateProcessSecurityEnvironment(blob, len(self.specification), 1, ctypes.byref(self.handle)), "CreateProcessSecurityEnvironment")
        if not self.handle:
            raise RuntimeError("Windows returned an empty security environment")

    def close(self):
        if self.handle:
            self.api.CloseProcessSecurityEnvironment(self.handle)
            self.handle = w.HANDLE()

    def startup(self, kernel, startup_type, stdio, job):
        """Prepare PSEC, a handle whitelist and the requested resource Job list."""
        class Extended(ctypes.Structure):
            _fields_ = [("info", startup_type), ("attributes", ctypes.c_void_p)]
        kernel.InitializeProcThreadAttributeList.argtypes = [ctypes.c_void_p,w.DWORD,w.DWORD,ctypes.POINTER(ctypes.c_size_t)]
        kernel.InitializeProcThreadAttributeList.restype = w.BOOL
        kernel.UpdateProcThreadAttribute.argtypes = [ctypes.c_void_p,w.DWORD,ctypes.c_size_t,ctypes.c_void_p,ctypes.c_size_t,ctypes.c_void_p,ctypes.c_void_p]
        kernel.UpdateProcThreadAttribute.restype = w.BOOL
        kernel.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
        count = ctypes.c_size_t()
        kernel.InitializeProcThreadAttributeList(None, 3, 0, ctypes.byref(count))
        if ctypes.get_last_error() != 122 or not count.value:
            raise ctypes.WinError(ctypes.get_last_error())
        storage = ctypes.create_string_buffer(count.value)
        if not kernel.InitializeProcThreadAttributeList(storage, 3, 0, ctypes.byref(count)):
            raise ctypes.WinError(ctypes.get_last_error())
        info = Extended()
        info.attributes = ctypes.cast(storage, ctypes.c_void_p)
        info.info.cb, info.info.dwFlags = ctypes.sizeof(info), 0x100
        info.info.hStdInput, info.info.hStdOutput, info.info.hStdError = stdio
        handles = (w.HANDLE*3)(*stdio)
        jobs = (w.HANDLE*1)(job)
        environment_handle = w.HANDLE(self.handle.value)
        try:
            for attribute, value in [(0x20023, environment_handle), (0x20002, handles), (0x2000D, jobs)]:
                if not kernel.UpdateProcThreadAttribute(storage, 0, attribute, ctypes.byref(value), ctypes.sizeof(value), None, None):
                    raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            kernel.DeleteProcThreadAttributeList(storage)
            raise
        # Keep every backing allocation alive until CreateProcessW returns.
        return info, (storage, handles, jobs, environment_handle)
