"""Standalone Windows sandbox: direct PSEC/BFS APIs and a trusted supervisor."""
from __future__ import annotations

import asyncio
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile

from game_test_agent.contracts.models import (
    AnalysisJobResult, AnalysisJobSpec, AnalysisOutputItem, SandboxEnvironment, SandboxOperationRef,
)
from game_test_agent.storage import canonical_json, payload_hash
from game_test_agent.analysis.windows_job import process_identity, terminate_job
from game_test_agent.analysis.permissions import validate_directories

_HASH_CACHE: dict[tuple, str] = {}


def file_hash(path: Path) -> str:
    meta = path.stat()
    key = (str(path.resolve()), meta.st_size, meta.st_mtime_ns)
    if key not in _HASH_CACHE:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while block := stream.read(1024*1024):
                digest.update(block)
        _HASH_CACHE[key] = digest.hexdigest()
    return _HASH_CACHE[key]


def runtime_lock_hash() -> str:
    return payload_hash({name: file_hash(Path(__file__).with_name(name)) for name in (
        "windows_runner.py", "windows_worker.py", "windows_job.py", "windows_security.py", "windows_shell.py",
        "permissions.py", "approval_keys.py",
    )})


def clean_environment(work: Path) -> dict[str, str]:
    allowed = {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "USERPROFILE",
               "USERNAME", "USERDOMAIN", "APPDATA", "LOCALAPPDATA", "PROGRAMFILES",
               "PROGRAMFILES(X86)", "PROGRAMDATA", "SYSTEMDRIVE", "ALLUSERSPROFILE",
               "PROGRAMW6432", "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)", "COMMONPROGRAMW6432",
               "HOMEDRIVE", "HOMEPATH", "OS", "PROCESSOR_ARCHITECTURE", "PUBLIC"}
    env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    env.update({"TEMP": str(work), "TMP": str(work), "PYTHONUTF8": "1",
                "PYTHONDONTWRITEBYTECODE": "1"})
    return env


class WindowsSandboxRunner:
    isolation_kind = "windows_psec"

    def __init__(self, artifacts, root: Path, runner_id: str, runtime_digest: str,
                 *, python_path: Path | None = None,
                 permission_store=None, approval_key_path: Path | None = None):
        self.artifacts, self.root, self.runner_id = artifacts, Path(root).resolve(), runner_id
        self.runtime_digest = runtime_digest
        self.permission_store = permission_store
        self.approval_key_path = approval_key_path
        self.python = Path(python_path or sys.executable).resolve()
        self.powershell = Path(shutil.which("pwsh") or
            Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe").resolve()
        self.dependency_lock_hash = runtime_lock_hash()
        self._verified = False
        self.root.mkdir(parents=True, exist_ok=True)

    def fingerprint(self) -> str:
        return "sha256:" + payload_hash({
            "isolation_kind": self.isolation_kind,
            "processmodel": file_hash(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/processmodel.dll"),
            "python": file_hash(self.python), "powershell": file_hash(self.powershell),
            "policy_lock": self.dependency_lock_hash, "windows_version": sys.getwindowsversion().build,
        })

    @property
    def environment(self) -> SandboxEnvironment:
        return SandboxEnvironment(runner_id=self.runner_id, isolation_kind=self.isolation_kind,
            runtime_digest=self.runtime_digest, dependency_lock_hash=self.dependency_lock_hash)

    def normalize_permissions(self, permissions):
        if permissions is None:
            return None
        protected = [self.root, Path(__file__).resolve().parents[3], self.python.parent,
                     Path(sys.base_prefix), self.powershell.parent,
                     Path(os.environ.get("SystemRoot", "C:/Windows"))]
        artifact_root = getattr(self.artifacts, "root", None)
        if artifact_root:
            protected.append(Path(artifact_root))
        database_path = getattr(getattr(self.permission_store, "repository", None), "path", None)
        if database_path and not str(database_path).startswith("mysql:"):
            protected.append(Path(database_path))
        if os.environ.get("GTA_LOCAL_CONFIG_FILE"):
            protected.append(Path(os.environ["GTA_LOCAL_CONFIG_FILE"]))
        if self.approval_key_path:
            protected.append(self.approval_key_path)
        protected.extend(Path.home() / name for name in (".ssh", ".aws", ".azure", ".kube", ".config", ".codex"))
        try:
            return validate_directories(permissions, protected)
        except PermissionError:
            raise
        except OSError as error:
            raise ValueError("requested directories must exist and be locally accessible") from error

    def _policy(self, directory: Path, permissions=None) -> dict:
        windows = Path(os.environ.get("SystemRoot", "C:/Windows"))
        runtime_paths = [self.python.parent, Path(sys.base_prefix).resolve(), self.powershell.parent,
                         windows / "System32", windows / "Microsoft.NET", windows / "assembly",
                         windows / "WinSxS", windows / "Globalization",
                         windows / "SysWOW64/WindowsPowerShell"]
        runtime_paths.extend(Path(os.environ[key]) / "WindowsPowerShell"
                             for key in ("ProgramFiles", "ProgramFiles(x86)") if os.environ.get(key))
        readonly = [str(path) for path in runtime_paths if path.is_dir()] + [str(directory / "inputs")]
        writable = [str(directory / "work")]
        if permissions:
            for item in permissions.directories:
                (writable if item.access == "write" else readonly).append(item.path)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True, winmode=0x800)
        kernel.GetLogicalDrives.restype = ctypes.c_uint32
        drives = kernel.GetLogicalDrives()
        if not drives:
            raise ctypes.WinError(ctypes.get_last_error())
        return {"read_only": list(dict.fromkeys(readonly)), "read_write": writable,
                "deny": [chr(65+index)+":\\" for index in range(26) if drives & (1 << index)],
                "network": bool(permissions and permissions.network)}

    async def verify(self):
        if self._verified:
            if await asyncio.to_thread(self.fingerprint) != self.runtime_digest:
                raise PermissionError("native sandbox runtime changed; rerun scripts/setup_sandbox.py")
            return
        if os.name != "nt":
            raise RuntimeError("Windows PSEC sandbox requires native Windows")
        if await asyncio.to_thread(self.fingerprint) != self.runtime_digest:
            raise PermissionError("native sandbox runtime changed; rerun scripts/setup_sandbox.py")
        with tempfile.TemporaryDirectory(prefix="verify-", dir=self.root) as temporary:
            directory = Path(temporary)
            (directory / "work").mkdir()
            (directory / "work/output").mkdir()
            (directory / "inputs").mkdir()
            (directory / "outside.txt").write_bytes(b"outside-canary")
            (directory / "inputs/canary.txt").write_bytes(b"input-canary")
            listener = socket.socket()
            try:
                listener.bind(("127.0.0.1", 0))
                listener.listen()
                probe = (
                    "import pathlib,socket,json; r=pathlib.Path(" + repr(str(directory)) + ");"
                    "\nassert (r/'inputs/canary.txt').read_bytes()==b'input-canary'"
                    "\nfor path,mode in [(r/'outside.txt','rb'),(r/'inputs/canary.txt','wb')]:"
                    "\n try: f=open(path,mode)"
                    "\n except OSError: pass"
                    "\n else: f.close();raise RuntimeError('filesystem isolation unavailable')"
                    "\n(r/'work/probe.txt').write_text('ok')"
                    "\ntry: socket.create_connection(('127.0.0.1'," + str(listener.getsockname()[1]) + "),timeout=.5)"
                    "\nexcept OSError as e: assert e.winerror==10013, 'network boundary not enforced'"
                    "\nelse: raise RuntimeError('network isolation unavailable')"
                    "\nprint('GTA_PSEC_VERIFIED')"
                )
                job_name = "Local\\gta-psec-verify-" + directory.name
                request = {"arguments": [str(self.python), "-I", "-X", "utf8", "-u", "-c", probe],
                    "security_policy": self._policy(directory), "environment": clean_environment(directory / "work"),
                    "limits": {"cpu_seconds":20,"memory_mb":512,"process_count":16,"disk_mb":64,
                               "wall_seconds":20,"output_bytes":65536},
                    "operation_id":"verify", "fencing_token":"verify", "job_object_name":job_name}
                process = await self._start_worker(directory, request)
                try:
                    for _ in range(600):
                        if (directory / "completed.json").exists():
                            break
                        if process.poll() is not None:
                            raise RuntimeError("native security probe supervisor exited before completion")
                        await asyncio.sleep(.05)
                    else:
                        raise TimeoutError("native security probe timed out")
                    completed = json.loads((directory / "completed.json").read_bytes())
                    stdout = (directory / "stdout.bin").read_bytes()
                    if completed["failure_kind"] or completed["exit_code"] != 0 or b"GTA_PSEC_VERIFIED" not in stdout:
                        detail = (directory / "stderr.bin").read_bytes()[:1000].decode("utf-8",errors="replace")
                        raise RuntimeError("native PSEC isolation probe failed; no fallback allowed: " + detail)
                except BaseException:
                    self._save(directory / "cancel-requested.json", {"requested":True})
                    terminate_job(job_name)
                    raise
                finally:
                    try:
                        await asyncio.to_thread(process.wait, 5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        await asyncio.to_thread(process.wait)
            finally:
                listener.close()
        self._verified = True

    def _directory(self, job_id):
        return self.root / hashlib.sha256(job_id.encode()).hexdigest()

    @staticmethod
    def _save(path: Path, value):
        import uuid
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_text(canonical_json(value), encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _immutable(path: Path, content: bytes):
        try:
            with path.open("xb") as stream:
                stream.write(content)
        except FileExistsError:
            if path.read_bytes() != content:
                if path.suffix == ".json" and payload_hash(json.loads(path.read_bytes())) == payload_hash(json.loads(content)):
                    return
                raise PermissionError("native operation cannot change accepted inputs")

    def _reference(self, spec):
        identity = payload_hash([str(self.root), self.runner_id, spec.job_id, payload_hash(spec)])
        return SandboxOperationRef(job_id=spec.job_id, runner_id=self.runner_id,
            operation_id="gta-native-" + identity[:40], fencing_token=identity, execution_status="queued")

    def _spec(self, operation):
        directory = self._directory(operation.job_id)
        spec = AnalysisJobSpec.model_validate_json((directory / "spec.json").read_bytes())
        if self._reference(spec) != operation.model_copy(update={"execution_status": "queued"}):
            raise PermissionError("native operation identity or fencing token mismatch")
        return spec

    def authorize_inputs(self, spec, evidence):
        directory = self._directory(spec.job_id)
        directory.mkdir(parents=True, exist_ok=True)
        selected = {e.evidence_id: {"artifact_ref": e.artifact_ref, "sha256": e.sha256,
                                   "mime_type": e.mime_type}
                    for e in evidence if e.evidence_id in spec.input_evidence_ids}
        self._immutable(directory / "authorized-inputs.json", canonical_json(selected).encode())

    async def submit(self, spec):
        directory = self._directory(spec.job_id)
        directory.mkdir(parents=True, exist_ok=True)
        self._immutable(directory / "spec.json", canonical_json(spec).encode())
        operation = self._reference(spec)
        if (directory / "accepted.json").exists() or (directory / "result.json").exists():
            return operation
        await self.verify()
        permissions = None
        if spec.permissions:
            if self.permission_store is None:
                raise PermissionError("permission approval store is required")
            permissions = self.permission_store.authorize_spec(spec)
            if self.normalize_permissions(permissions) != permissions:
                raise PermissionError("approved directories changed before execution")
        expected_environment = self.environment.model_copy(update={
            "network_access": "approved" if permissions and permissions.network else "disabled"})
        if spec.environment != expected_environment:
            raise PermissionError("native job runtime does not match the admitted environment")
        if spec.program.language not in {"python", "shell", "powershell"}:
            raise ValueError("unsupported native program language")
        inputs, work = directory / "inputs", directory / "work"
        inputs.mkdir(exist_ok=True)
        (work / "output").mkdir(parents=True, exist_ok=True)
        code = self.artifacts.read_bytes(spec.program.code_artifact_ref)
        if hashlib.sha256(code).hexdigest() != spec.program.code_sha256:
            raise PermissionError("native program hash mismatch")
        program = inputs / ("program.py" if spec.program.language == "python" else "program.ps1")
        # UTF-8 BOM lets Windows PowerShell 5.1 read exact Unicode source without
        # invoking .NET methods that system ConstrainedLanguage may prohibit.
        self._immutable(program, code if spec.program.language == "python" else b"\xef\xbb\xbf" + code)
        selected = json.loads((directory / "authorized-inputs.json").read_bytes())
        if set(selected) != set(spec.input_evidence_ids):
            raise PermissionError("native input manifest mismatch")
        manifest = {"evidence": []}
        for index, evidence_id in enumerate(spec.input_evidence_ids):
            item = selected[evidence_id]
            if not self.artifacts.validate_scope(item["artifact_ref"], spec.tenant_id, spec.project_id):
                raise PermissionError("native input is outside project scope")
            content = self.artifacts.read_bytes(item["artifact_ref"])
            if hashlib.sha256(content).hexdigest() != item["sha256"].lower():
                raise PermissionError("native input hash mismatch")
            path = inputs / f"evidence-{index:03d}.bin"
            self._immutable(path, content)
            manifest["evidence"].append({"evidence_id": evidence_id, "path": str(path),
                                         "sha256": item["sha256"], "mime_type": item["mime_type"]})
        self._immutable(inputs / "manifest.json", canonical_json(manifest).encode())
        environment = clean_environment(work)
        environment.update({"GTA_INPUT_MANIFEST": str(inputs / "manifest.json"),
                            "GTA_OUTPUT_DIR": str(work / "output"), "GTA_PROGRAM_FILE": str(program)})
        # -I ignores PYTHONUTF8; force UTF-8 explicitly for Chinese paths/results.
        command = [str(self.python), "-I", "-X", "utf8", "-u", str(program)]
        if spec.program.language != "python":
            shell_bootstrap = inputs / "shell-bootstrap.py"
            self._immutable(shell_bootstrap, Path(__file__).with_name("windows_shell.py").read_bytes())
            command = [str(self.python), "-I", "-X", "utf8", "-u", str(shell_bootstrap), str(self.powershell)]
        request = {"arguments": command, "security_policy": self._policy(directory, permissions), "environment": environment,
            "limits": spec.limits.model_dump(), "operation_id": operation.operation_id,
            "fencing_token": operation.fencing_token, "job_object_name": "Local\\" + operation.operation_id,
            "private_console": spec.program.language != "python"}
        await self._start_worker(directory, request)
        return operation

    async def _start_worker(self, directory, request):
        self._immutable(directory / "request.json", canonical_json(request).encode())
        # The trusted supervisor directly calls Windows APIs; generated source
        # only starts after PSEC policy and Job membership are applied in-kernel.
        supervisor = subprocess.Popen(
            [sys.executable, "-I", str(Path(__file__).with_name("windows_worker.py")), str(directory)],
            env=clean_environment(directory / "work"), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        for _ in range(100):
            if (directory / "accepted.json").exists():
                return supervisor
            if supervisor.poll() is not None and not (directory / "acceptance.lock").exists():
                raise RuntimeError("native supervisor failed before accepting the task")
            await asyncio.sleep(.05)
        raise RuntimeError("native supervisor acceptance timed out")

    async def query(self, operation):
        spec = self._spec(operation)
        directory = self._directory(operation.job_id)
        saved = directory / "result.json"
        if saved.exists():
            return AnalysisJobResult.model_validate_json(saved.read_bytes())
        completed = directory / "completed.json"
        if not completed.exists():
            accepted = directory / "accepted.json"
            if not accepted.exists():
                lease = directory / "acceptance.lock"
                if not lease.exists():
                    return None
                try:
                    identity = json.loads(lease.read_bytes())
                except json.JSONDecodeError:
                    return None  # An exclusive lease is still being written.
            else:
                identity = json.loads(accepted.read_bytes())
            fenced = identity.get("closed_before_accept", False)
            if not fenced and process_identity(identity["pid"]) == identity.get("creation_token"):
                return None
            # Supervisor death closes its kill-on-close Job. Never rerun accepted
            # code implicitly; produce an aborted result that the model can inspect.
            if not fenced:
                terminate_job("Local\\" + operation.operation_id)
            result_data = {"operation_id": operation.operation_id, "fencing_token": operation.fencing_token,
                "exit_code": None, "failure_kind": "execution_canceled" if fenced else "native_supervisor_lost", "outputs": [], "usage": {},
                "started_at": identity["started_at"], "finished_at": datetime.now(timezone.utc).isoformat()}
            self._save(completed, result_data)
        result_data = json.loads(completed.read_bytes())
        if (result_data["operation_id"], result_data["fencing_token"]) != (operation.operation_id, operation.fencing_token):
            raise PermissionError("native result identity mismatch")
        outputs = []
        total = 0
        for item in result_data["outputs"]:
            path = Path(item["path"])
            if path.resolve().parent != directory or path.is_symlink():
                raise PermissionError("native result path escaped operation directory")
            content = path.read_bytes()
            total += len(content)
            if total > spec.limits.output_bytes or len(content) != item["size"]:
                raise ValueError("native output budget exceeded")
            stored = self.artifacts.put_bytes(spec.tenant_id, spec.project_id, content)
            name = item["name"]
            outputs.append(AnalysisOutputItem(output_id=name, artifact_ref=stored.ref,
                sha256=stored.sha256, size_bytes=stored.size, kind="code_execution_output",
                mime_type=(mimetypes.guess_type(name[5:])[0] or "application/octet-stream") if name.startswith("file:") else "text/plain",
                limitations=["generated_code_output_is_not_independently_verified"]))
        failure = result_data["failure_kind"]
        status = "canceled" if failure == "execution_canceled" else ("failed" if failure else "succeeded")
        usage = result_data["usage"]
        result = AnalysisJobResult(job_id=spec.job_id, result_revision=1, execution_status=status,
            exit_code=result_data["exit_code"], outputs=outputs, isolation_kind=self.isolation_kind,
            environment_runtime_digest=spec.environment.runtime_digest,
            dependency_lock_hash=spec.environment.dependency_lock_hash,
            network_access=spec.environment.network_access,
            failure_kind=failure, started_at=result_data["started_at"], finished_at=result_data["finished_at"],
            resource_usage={"observed_cpu_seconds": usage.get("cpu_seconds", 0),
                            "observed_peak_job_memory_mb": usage.get("peak_memory_mb", 0)})
        self._save(saved, result.model_dump(mode="json"))
        return result

    async def cancel(self, operation):
        self._spec(operation)
        directory = self._directory(operation.job_id)
        if (directory / "completed.json").exists() or (directory / "result.json").exists():
            return True
        self._save(directory / "cancel-requested.json", {"requested": True})
        for _ in range(200):
            if await self.query(operation) is not None:
                return True
            await asyncio.sleep(.05)
        terminate_job("Local\\" + operation.operation_id)
        return False

    async def recover_operation(self, spec):
        directory = self._directory(spec.job_id)
        # A saved request may already have spawned a supervisor whose acceptance
        # acknowledgement was lost. Recover its identity rather than launch again.
        return self._reference(spec) if any((directory / name).exists() for name in
            ("accepted.json", "request.json", "acceptance.lock")) else None

    async def request_cancel(self, operation):
        """Persist cancellation without waiting for process termination."""
        self._spec(operation)
        directory = self._directory(operation.job_id)
        self._save(directory / "cancel-requested.json", {"requested": True})
        if not (directory / "accepted.json").exists():
            try:
                # Win the same exclusive lease used by the supervisor. A late
                # supervisor can no longer accept or execute this request.
                with (directory / "acceptance.lock").open("x", encoding="utf-8") as stream:
                    json.dump({"closed_before_accept": True,
                               "started_at": datetime.now(timezone.utc).isoformat()}, stream)
            except FileExistsError:
                pass  # Existing ownership is not proof of completion; query it.
        return True  # Requested, not confirmed. query() supplies terminal proof.

    async def remove(self, operation):
        if await self.query(operation) is None:
            raise RuntimeError("native process cleanup not confirmed")
        terminate_job("Local\\" + operation.operation_id)
        self._save(self._directory(operation.job_id) / "cleanup-confirmed.json", {"confirmed": True})
