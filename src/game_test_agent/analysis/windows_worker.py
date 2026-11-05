"""Trusted, detached supervisor. Generated code is launched ONLY through PSEC.

This process survives an API/Temporal worker restart and owns the Windows Job
Object. Its lifetime, output capture and cancellation are independent of the LLM.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import stat
import sys
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from windows_job import WindowsJobProcess, process_identity  # noqa: E402


def save(path: Path, value) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=True, sort_keys=True), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def workspace_size(root: Path) -> int:
    """Do not follow reparse points into host directories while monitoring size."""
    total, count, pending = 0, 0, [root]
    while pending:
        directory = pending.pop()
        meta = directory.lstat()
        if getattr(meta, "st_file_attributes", 0) & 0x400:
            raise ValueError("unsupported_output_file")
        for path in directory.iterdir():
            meta = path.lstat()
            if getattr(meta, "st_file_attributes", 0) & 0x400:
                raise ValueError("unsupported_output_file")
            count += 1
            if count > 1024:
                raise ValueError("workspace_file_count_exceeded")
            if stat.S_ISDIR(meta.st_mode):
                pending.append(path)
            elif stat.S_ISREG(meta.st_mode):
                total += meta.st_size
            else:
                raise ValueError("unsupported_output_file")
    return total


def run(directory: Path) -> None:
    # Atomic x-create prevents duplicate supervisors after a lost submit response.
    # Never steal this lease just because a PID is absent: a lost supervisor means
    # an unknown/aborted operation, not permission to execute the code again.
    try:
        with (directory / "acceptance.lock").open("x", encoding="utf-8") as stream:
            json.dump({"pid": os.getpid(), "creation_token": process_identity(os.getpid()),
                       "started_at": datetime.now(timezone.utc).isoformat()}, stream)
    except FileExistsError:
        return
    save(directory / "accepted.json", {"pid": os.getpid(), "creation_token": process_identity(os.getpid()),
                                        "started_at": datetime.now(timezone.utc).isoformat()})
    request = json.loads((directory / "request.json").read_bytes())
    limits = request["limits"]
    work = directory / "work"
    started = datetime.now(timezone.utc)
    failure, process, exit_code = None, None, None
    streams = {"stdout": bytearray(), "stderr": bytearray()}
    captured = 0
    usage = {}
    try:
        if (directory / "cancel-requested.json").exists():
            failure = "execution_canceled"
        else:
            process = WindowsJobProcess(
                request["arguments"], env=request["environment"], cwd=work,
                name=request["job_object_name"], memory_mb=limits["memory_mb"],
                processes=limits["process_count"], cpu_seconds=limits["cpu_seconds"],
                security_policy=request["security_policy"],
                private_console=request.get("private_console", False),
            )
            save(directory / "running.json", {"pid": process.pid, "supervisor_pid": os.getpid()})
            messages = queue.Queue(maxsize=8)

            def pump(name, stream):
                try:
                    while block := stream.read(65536):
                        messages.put((name, block))
                finally:
                    messages.put((name, None))
                    stream.close()

            for name in streams:
                threading.Thread(target=pump, args=(name, getattr(process, name)), daemon=True).start()
            closed = set()
            deadline = time.monotonic() + max(1, limits["wall_seconds"] - 2)
            while len(closed) < 2 or process.poll() is None:
                if (directory / "cancel-requested.json").exists():
                    failure = "execution_canceled"
                elif time.monotonic() > deadline:
                    failure = "wall_time_exceeded"
                else:
                    try:
                        if workspace_size(work) > limits["disk_mb"] * 1024 * 1024:
                            failure = "workspace_limit_exceeded"
                    except FileNotFoundError:
                        pass  # A file can disappear between scandir/stat.
                    except ValueError as error:
                        failure = str(error)
                if failure:
                    process.terminate()
                    break
                try:
                    name, block = messages.get(timeout=.03)
                except queue.Empty:
                    continue
                if block is None:
                    closed.add(name)
                    continue
                keep = block[:max(0, limits["output_bytes"] - captured)]
                streams[name].extend(keep)
                captured += len(keep)
                if len(keep) != len(block):
                    failure = "output_limit_exceeded"
                    process.terminate()
                    break
            # Terminate descendants even after a successful foreground command.
            exit_code = process.poll()
            usage = process.usage()
            process.terminate()
            for _ in range(100):
                if process.usage()["active_processes"] == 0:
                    break
                time.sleep(.01)
            else:
                raise RuntimeError("process_tree_cleanup_not_confirmed")
            if exit_code is None:
                exit_code = process.poll()
    except BaseException as error:
        failure = failure or "native_supervisor_error:" + type(error).__name__
        diagnostic = ("Native sandbox error: " + str(error))[:1000].encode("utf-8")
        diagnostic = diagnostic[:max(0, limits["output_bytes"] - captured)]
        streams["stderr"].extend(diagnostic)
        captured += len(diagnostic)
    finally:
        if process is not None:
            process.close()  # KILL_ON_JOB_CLOSE is the final crash/cleanup boundary.
    outputs = []
    for name, content in streams.items():
        path = directory / (name + ".bin")
        path.write_bytes(content)
        outputs.append({"name": name, "path": str(path), "size": len(content)})
    output_root = work / "output"
    try:
        if output_root.lstat().st_file_attributes & 0x400:
            raise ValueError("unsupported_output_file")
        for path in sorted(output_root.iterdir()):
            if len(outputs) >= 18:
                raise ValueError("output_file_count_exceeded")
            meta = path.lstat()
            if (getattr(meta, "st_file_attributes", 0) & 0x400
                    or not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1):
                raise ValueError("unsupported_output_file")
            import re
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", path.name) or path.name in {".", ".."}:
                raise ValueError("unsupported_output_name")
            if meta.st_size + captured > limits["output_bytes"]:
                raise ValueError("output_limit_exceeded")
            # All processes are stopped before host reads untrusted output files.
            content = path.read_bytes()
            if len(content) != meta.st_size:
                raise ValueError("output_changed_after_execution")
            target = directory / ("attachment-" + str(len(outputs)) + ".bin")
            target.write_bytes(content)
            captured += len(content)
            outputs.append({"name": "file:" + path.name, "path": str(target), "size": len(content)})
    except (OSError, ValueError) as error:
        failure = failure or (str(error) if isinstance(error, ValueError) else "output_collection_failed")
    if exit_code != 0 and not failure:
        failure = "program_error"
    finished = datetime.now(timezone.utc)
    save(directory / "completed.json", {
        "operation_id": request["operation_id"], "fencing_token": request["fencing_token"],
        "exit_code": exit_code, "failure_kind": failure, "outputs": outputs,
        "started_at": started.isoformat(), "finished_at": finished.isoformat(), "usage": usage,
    })


if __name__ == "__main__":
    run(Path(sys.argv[1]).resolve())
