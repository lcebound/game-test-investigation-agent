from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from game_test_agent.analysis.code_execution import build_code_execution
from game_test_agent.contracts.models import (
    CodeExecutionRequest, EvidenceRecord, InvestigationRequest, NextStep,
)
from game_test_agent.evidence import EvidenceService
from game_test_agent.storage import LocalArtifactStore


@pytest.fixture
def native_code(repository, settings, monkeypatch):
    if os.name != "nt" or os.getenv("GTA_RUN_NATIVE_SANDBOX_TESTS") != "1":
        pytest.skip("set GTA_RUN_NATIVE_SANDBOX_TESTS=1 after scripts/setup_sandbox.py on Windows")
    config = json.loads((Path(__file__).parents[1] / "data/local/sandbox.env.json").read_bytes())
    configured = replace(settings, sandbox_backend="windows",
        sandbox_runtime_digest=config["GTA_SANDBOX_RUNTIME_DIGEST"],
        sandbox_python_path=Path(config["GTA_SANDBOX_PYTHON_PATH"]),
        sandbox_runner_ids=("test-native-windows",),
        sandbox_runner_root=settings.artifact_root.parent / "sandbox-runner",
        sandbox_max_jobs_per_investigation=8,
    )
    view, _ = repository.create_investigation(InvestigationRequest(
        tenant_id="studio-a", project_id="game-a",
        objective={"objective_id": "analyze", "description": "比较故障前后的采样"},
        budget={"max_rounds": 12},
    ), "code-test")
    artifacts = LocalArtifactStore(settings.artifact_root)
    stored = artifacts.put_bytes("studio-a", "game-a", b"phase,frame_ms\nbefore,10\nbefore,20\nafter,40\nafter,60\n")
    source = EvidenceRecord(
        evidence_id="ev-metrics", tenant_id="studio-a", project_id="game-a",
        artifact_ref=stored.ref, sha256=stored.sha256, kind="metrics", mime_type="text/csv",
        producer="test", producer_version="1", collected_at=datetime.now(timezone.utc),
        locator={"kind": "region", "value": "whole"}, quality="partial",
        source_type="event", source_evidence_id="metrics",
    )
    EvidenceService(repository).ingest(view.investigation_id, source)
    service = build_code_execution(configured, repository, artifacts)
    yield view, service, source, configured
    asyncio.run(service.cancel_investigation(view.investigation_id))


def execute(fixture, code, *, language="python", decision="first", repair=None):
    view, service, source, _ = fixture
    return asyncio.run(service.execute(view.investigation_id, decision, CodeExecutionRequest(
        language=language, code=code, purpose="分析采样", input_evidence_ids=[source.evidence_id],
        repair_of_job_id=repair,
    )))


def test_native_python_real_analysis_repair_and_restart(native_code):
    view, service, source, configured = native_code
    failed = execute(native_code, "print(undefined_value)")
    assert failed["execution_status"] == "failed"
    assert "NameError" in failed["stderr"]
    code = '''import csv, json, os
from pathlib import Path
from statistics import mean
manifest=json.loads(Path(os.environ['GTA_INPUT_MANIFEST']).read_text())
rows=list(csv.DictReader(open(manifest['evidence'][0]['path'])))
averages={phase:mean(float(r['frame_ms']) for r in rows if r['phase']==phase) for phase in ['before','after']}
result={'before':averages['before'],'after':averages['after'],'ratio':averages['after']/averages['before']}
(Path(os.environ['GTA_OUTPUT_DIR'])/'comparison.json').write_text(json.dumps(result))
print(json.dumps(result))
'''
    result = execute(native_code, code, decision="repair", repair=failed["job_id"])
    assert result["execution_status"] == "succeeded", result
    assert json.loads(result["stdout"])["ratio"] == pytest.approx(50/15)
    assert any(o["output_id"] == "file:comparison.json" for o in result["outputs"])
    evidence = [e for e in service.repository.list_current_evidence(view.investigation_id)
                if e.analysis_job_id == result["job_id"]]
    assert evidence and all(e.quality == "untrusted" and source.evidence_id in e.derived_from for e in evidence)
    assert not service.repository.list_current_facts(view.investigation_id)
    jobs_before = len(service.repository.list_analysis_jobs(view.investigation_id))
    # A new runner/service instance must replay persisted results, never rerun code.
    resumed = build_code_execution(configured, service.repository, service.artifacts)
    resumed.runner.submit = AsyncMock(side_effect=AssertionError("must not resubmit completed job"))
    replay = asyncio.run(resumed.execute(view.investigation_id, "repair", CodeExecutionRequest(
        language="python", code=code, purpose="分析采样", input_evidence_ids=[source.evidence_id],
        repair_of_job_id=failed["job_id"],
    )))
    assert replay["job_id"] == result["job_id"]
    assert len(service.repository.list_analysis_jobs(view.investigation_id)) == jobs_before
    assert not service.repository.pending_cleanup_resources(view.investigation_id)


def test_native_shell_and_isolation(native_code, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "host-only-canary")
    result = execute(native_code, '''
if ($env:OPENAI_API_KEY) { throw 'host credential leaked' }
$manifest = Get-Content -LiteralPath $env:GTA_INPUT_MANIFEST -Raw | ConvertFrom-Json
$blocked = $false
try { Set-Content -LiteralPath $manifest.evidence[0].path -Value 'changed' -ErrorAction Stop }
catch { $blocked = $true }
if (-not $blocked) { throw 'input was writable' }
Write-Output 'shell-isolated'
''', language="shell")
    assert result["execution_status"] == "succeeded", result
    assert result["stdout"].strip() == "shell-isolated"
    net = execute(native_code, '''import socket, sys
assert sys.platform == 'win32'
try:
    socket.create_connection(('1.1.1.1', 443), timeout=.5)
except OSError as error:
    assert error.winerror == 10013
    print('network-blocked')
else:
    raise RuntimeError('network should be disabled')
''', decision="network")
    assert net["execution_status"] == "succeeded" and "network-blocked" in net["stdout"]


def test_native_standalone_launch_and_descendants_are_confined(native_code, tmp_path, monkeypatch):
    import shutil
    original_which = shutil.which

    def no_codex(name, *args, **kwargs):
        assert "codex" not in str(name).lower(), "sandbox must not discover Codex CLI"
        return original_which(name, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", no_codex)
    view, service, source, configured = native_code
    service = build_code_execution(configured, service.repository, service.artifacts)
    outside = tmp_path / "host-secret.txt"
    outside.write_text("host-only-canary")
    child = f'''import ctypes, socket
from ctypes import wintypes as w
k=ctypes.WinDLL('kernel32',use_last_error=True)
a=ctypes.WinDLL('advapi32',use_last_error=True)
k.GetCurrentProcess.restype=w.HANDLE
a.OpenProcessToken.argtypes=[w.HANDLE,w.DWORD,ctypes.POINTER(w.HANDLE)]
a.GetTokenInformation.argtypes=[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD,ctypes.POINTER(w.DWORD)]
k.CloseHandle.argtypes=[w.HANDLE]
token=w.HANDLE(); size=w.DWORD(); confined=w.DWORD()
assert a.OpenProcessToken(k.GetCurrentProcess(),8,ctypes.byref(token))
try:
    assert a.GetTokenInformation(token,29,ctypes.byref(confined),4,ctypes.byref(size))
    assert confined.value==1, 'child lost AppContainer token'
finally: k.CloseHandle(token)
try: open({str(outside)!r}).read()
except PermissionError: pass
else: raise AssertionError('child escaped filesystem policy')
try: socket.create_connection(('127.0.0.1',9),timeout=.5)
except OSError as e: assert e.winerror==10013
else: raise AssertionError('child escaped network policy')
print('child-confined')
'''
    code = "import subprocess,sys\nsubprocess.run([sys.executable,'-I','-X','utf8','-c'," + repr(child) + "],check=True)"
    result = execute((view, service, source, configured), code)
    assert result["execution_status"] == "succeeded", result
    assert result["isolation_kind"] == "windows_psec"
    assert "child-confined" in result["stdout"]
    request = json.loads((service.runner._directory(result["job_id"]) / "request.json").read_bytes())
    assert Path(request["arguments"][0]) == service.runner.python
    assert not request["security_policy"]["network"]


def test_native_fingerprint_mismatch_never_starts_a_process(native_code):
    runner = native_code[1].runner
    runner.runtime_digest = "sha256:" + "0" * 64
    runner._start_worker = AsyncMock(side_effect=AssertionError("must fail before creating worker"))
    with pytest.raises(PermissionError, match="runtime changed"):
        asyncio.run(runner.verify())
    runner._start_worker.assert_not_called()


def test_native_security_environment_close_terminates_live_process(native_code):
    import time
    from game_test_agent.analysis.windows_job import WindowsJobProcess
    from game_test_agent.analysis.windows_runner import clean_environment
    runner = native_code[1].runner
    directory = runner.root / "terminate-on-close"
    (directory / "inputs").mkdir(parents=True)
    work = directory / "work"
    work.mkdir()
    process = WindowsJobProcess([str(runner.python), "-I", "-u", "-c",
        "import time;print('ready',flush=True);time.sleep(60)"], env=clean_environment(work),
        cwd=work, name="Local\\gta-psec-close-" + directory.parent.name,
        memory_mb=512, processes=16, cpu_seconds=20, security_policy=runner._policy(directory))
    try:
        assert process.stdout.readline().strip() == b"ready"
        assert process.poll() is None
        # Leave the resource Job open: this specifically tests PSEC's independent
        # close boundary, including the pre-Job-assignment crash window.
        process.security_environment.close()
        for _ in range(100):
            if process.poll() is not None:
                break
            time.sleep(.02)
        assert process.poll() is not None
    finally:
        process.close()


def test_native_windows_powershell_51_preserves_unicode(native_code):
    from game_test_agent.analysis.windows_job import WindowsJobProcess
    from game_test_agent.analysis.windows_runner import clean_environment
    runner = native_code[1].runner
    directory = runner.root / "powershell-51-check"
    (directory / "inputs").mkdir(parents=True)
    work = directory / "work"
    work.mkdir()
    bootstrap = directory / "inputs/shell-bootstrap.py"
    bootstrap.write_bytes(Path(__file__).parents[1].joinpath(
        "src/game_test_agent/analysis/windows_shell.py").read_bytes())
    program = directory / "inputs/program.ps1"
    program.write_text("Write-Output '中文结果🧪'", encoding="utf-8-sig")
    environment = clean_environment(work)
    environment["GTA_PROGRAM_FILE"] = str(program)
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    process = WindowsJobProcess([str(runner.python), "-I", "-X", "utf8", str(bootstrap), str(powershell)],
        env=environment, cwd=work, name="Local\\gta-ps51-" + directory.parent.name,
        memory_mb=512, processes=16, cpu_seconds=20, security_policy=runner._policy(directory), private_console=True)
    try:
        assert process.stdout.read().decode("utf-8").strip() == "中文结果🧪"
        assert not process.stderr.read()
    finally:
        process.close()


@pytest.mark.parametrize("language,code", [
    ("python", "print('中文结果🧪')"),
    ("powershell", "Write-Output '中文结果🧪'"),
])
def test_native_unicode_output_is_utf8(native_code, language, code):
    result = execute(native_code, code, language=language)
    assert result["execution_status"] == "succeeded", result
    assert result["stdout"].strip() == "中文结果🧪"


async def wait_for_permission(service, investigation_id):
    for _ in range(100):
        records = service.permissions.list(investigation_id)
        if records:
            return records[0]
        await asyncio.sleep(.05)
    pytest.fail("permission request was not persisted")


def test_native_human_approval_enables_only_requested_directories_and_network(native_code, tmp_path):
    from game_test_agent.api.auth import Principal
    read_root, write_root, denied_root = [tmp_path / name for name in ("external-read", "external-write", "denied")]
    for path in (read_root, write_root, denied_root):
        path.mkdir()
    (read_root / "input.txt").write_text("authorized input")
    (denied_root / "secret.txt").write_text("not authorized")

    async def run():
        async def hello(reader, writer):
            writer.write(b"mxc-approved-network")
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        server = await asyncio.start_server(hello, "127.0.0.1", 0)
        view, service, source, _ = native_code
        principal = Principal("human", "studio-a", "game-a", frozenset({"agent:analysis:approve"}))
        code = f'''from pathlib import Path
import socket
read_root=Path({str(read_root)!r})
write_root=Path({str(write_root)!r})
assert (read_root/'input.txt').read_text()=='authorized input'
for path,mode in [(read_root/'input.txt','w'),(Path({str(denied_root)!r})/'secret.txt','r')]:
    try: f=open(path,mode)
    except OSError: pass
    else: f.close();raise RuntimeError('permission boundary failed')
with socket.create_connection(('127.0.0.1',{server.sockets[0].getsockname()[1]}),timeout=3) as connection:
    assert connection.recv(100)==b'mxc-approved-network'
(write_root/'result.txt').write_text('approved single execution')
print('approved-files-and-network')
'''
        request = CodeExecutionRequest(language="python", code=code, purpose="analyze external input",
            input_evidence_ids=[source.evidence_id], permissions={"directories": [
                {"path": str(read_root), "access": "read"}, {"path": str(write_root), "access": "write"}],
                "network": True, "reason": "read logs, save a result and query an explicitly reviewed local fixture"})
        task = asyncio.create_task(service.execute(view.investigation_id, "permission-job", request))
        try:
            approval = await wait_for_permission(service, view.investigation_id)
            assert approval["request"]["code"] == request.code
            assert not task.done() and not (write_root / "result.txt").exists()
            assert not service.repository.list_analysis_jobs(view.investigation_id)
            service.permissions.decide(approval["approval_id"], approval["request_hash"], "approve", principal)
            result = await asyncio.wait_for(task, timeout=30)
            assert result["execution_status"] == "succeeded", result
            assert (write_root / "result.txt").read_text() == "approved single execution"
            assert (read_root / "input.txt").read_text() == "authorized input"
            job = service.repository.list_analysis_jobs(view.investigation_id)[0]
            saved = service.repository.get_latest_analysis_result(job.spec.job_id).result
            assert saved.network_access == "approved"
            for changed in [job.spec.model_copy(update={"job_id": "forged-job"}),
                            job.spec.model_copy(update={"purpose": "different purpose"}),
                            job.spec.model_copy(update={"program": job.spec.program.model_copy(update={"code_sha256":"f"*64})})]:
                with pytest.raises(PermissionError, match="exact code job"):
                    service.permissions.authorize_spec(changed)
            with pytest.raises(PermissionError):
                await service.runner.submit(job.spec.model_copy(update={"job_id": "forged-job"}))
            # Historical replay must work after an external folder disappears.
            (write_root / "result.txt").unlink()
            write_root.rmdir()
            replay = await service.execute(view.investigation_id, "permission-job", request)
            assert replay["job_id"] == result["job_id"]
            assert not write_root.exists()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            server.close()
            await server.wait_closed()
    asyncio.run(run())


@pytest.mark.parametrize("decision", ["deny", "expire", "cancel"])
def test_native_no_code_runs_without_approval(native_code, decision):
    from game_test_agent.api.auth import Principal
    async def run():
        view, service, source, _ = native_code
        service.runner.submit = AsyncMock(side_effect=AssertionError("unapproved code must not run"))
        task = asyncio.create_task(service.execute(view.investigation_id, "needs-network", CodeExecutionRequest(
            language="python", code="print('must not run')", purpose="check gate",
            input_evidence_ids=[source.evidence_id], permissions={"network": True, "reason": "test explicit gate"})))
        approval = await wait_for_permission(service, view.investigation_id)
        if decision == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert service.permissions.get(approval["approval_id"])["status"] == "canceled"
        else:
            if decision == "deny":
                service.permissions.decide(approval["approval_id"], approval["request_hash"], "deny",
                    Principal("human", "studio-a", "game-a", frozenset({"agent:analysis:approve"})))
            else:
                service.repository._connection.execute("UPDATE code_permission_requests SET expires_at=? WHERE approval_id=?",
                    (datetime(2000,1,1,tzinfo=timezone.utc).isoformat(), approval["approval_id"]))
            result = await asyncio.wait_for(task, timeout=3)
            assert result["execution_status"] == "not_executed"
            assert result["failure_kind"] == ("permission_denied" if decision == "deny" else "permission_expired")
        service.runner.submit.assert_not_called()
        assert not service.repository.list_analysis_jobs(view.investigation_id)
    asyncio.run(run())


def test_native_recovers_a_pending_permission_request_without_rewriting_code(native_code):
    from game_test_agent.api.auth import Principal
    from game_test_agent.storage import payload_hash
    async def run():
        view, service, source, configured = native_code
        decision_id = "recover-permission"
        job_id = "code-" + payload_hash([view.investigation_id, decision_id])[:40]
        request = CodeExecutionRequest(language="python", code="print('recovered-permission')",
            purpose="test approval recovery", input_evidence_ids=[source.evidence_id],
            permissions={"network": True,"reason":"test single-task approval recovery"})
        approval = service.permissions.propose(view.investigation_id, decision_id, job_id,
            request, service.runner.runtime_digest, {source.evidence_id:source.sha256})
        resumed = build_code_execution(configured, service.repository, service.artifacts)
        task = asyncio.create_task(resumed.resume_investigation(view.investigation_id))
        await asyncio.sleep(.1)
        assert not task.done()
        service.permissions.decide(approval["approval_id"], approval["request_hash"], "approve",
            Principal("human","studio-a","game-a",frozenset({"agent:analysis:approve"})))
        results = await asyncio.wait_for(task, timeout=30)
        assert len(results) == 1 and results[0]["execution_status"] == "succeeded", results
        assert results[0]["job_id"] == job_id
        assert len(service.permissions.list(view.investigation_id)) == 1
        assert len(service.repository.list_analysis_jobs(view.investigation_id)) == 1
        resumed.runner.submit = AsyncMock(side_effect=AssertionError("completed approved code must not rerun"))
        assert (await resumed.resume_investigation(view.investigation_id))[0]["job_id"] == job_id
        resumed.runner.submit.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize("code,failure", [
    ("print('x' * 400000)", "output_limit_exceeded"),
    ("import os; from pathlib import Path; (Path(os.environ['GTA_OUTPUT_DIR'])/'nested').mkdir()", "unsupported_output_file"),
    ("import time; time.sleep(30)", "wall_time_exceeded"),
])
def test_native_execution_limits(native_code, code, failure):
    if "sleep" in code:
        native_code[1].sandbox.policy.max_wall_seconds = 6
    result = execute(native_code, code)
    assert result["execution_status"] == "failed", result
    assert result["failure_kind"] in {failure, "wall_time_or_process_limit_exceeded"}, result


def test_native_adopts_accepted_job_after_lost_submit_response(native_code):
    view, service, source, configured = native_code
    original = service.runner.submit
    async def lost_response(spec):
        await original(spec)
        raise RuntimeError("simulated worker restart after native supervisor accepted the task")
    service.runner.submit = lost_response
    with pytest.raises(RuntimeError, match="simulated worker restart"):
        execute(native_code, "print('exactly-once')")
    job = service.repository.list_analysis_jobs(view.investigation_id)[0]
    resumed = build_code_execution(configured, service.repository, service.artifacts)
    accepted = resumed.runner._directory(job.spec.job_id) / "accepted.json"
    before = accepted.read_bytes()
    result = asyncio.run(resumed.execute(view.investigation_id, "first", CodeExecutionRequest(
        language="python", code="print('exactly-once')", purpose="分析采样",
        input_evidence_ids=[source.evidence_id],
    )))
    assert result["execution_status"] == "succeeded", result
    assert result["job_id"] == job.spec.job_id
    assert accepted.read_bytes() == before
    assert len(service.repository.list_analysis_jobs(view.investigation_id)) == 1


def test_native_cancellation_stops_the_process_tree_and_settles_cleanup(native_code):
    async def run():
        view, service, source, _ = native_code
        submitted = asyncio.Event()
        original = service.runner.submit
        async def submit(spec):
            operation = await original(spec)
            submitted.set()
            return operation
        service.runner.submit = submit
        task = asyncio.create_task(service.execute(view.investigation_id, "cancel", CodeExecutionRequest(
            language="python", code="import time; time.sleep(30)", purpose="cancellation test",
            input_evidence_ids=[source.evidence_id],
        )))
        await asyncio.wait_for(submitted.wait(), timeout=20)
        await asyncio.sleep(.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        job = service.repository.list_analysis_jobs(view.investigation_id)[0]
        assert job.state == "canceled"
        assert not service.repository.pending_cleanup_resources(view.investigation_id)
        assert (service.runner._directory(job.spec.job_id) / "cleanup-confirmed.json").exists()
    asyncio.run(run())


def test_native_repair_budget_is_enforced(native_code):
    first = execute(native_code, "raise ValueError('one')")
    second = execute(native_code, "raise ValueError('two')", decision="second", repair=first["job_id"])
    third = execute(native_code, "raise ValueError('three')", decision="third", repair=second["job_id"])
    with pytest.raises(ValueError, match="repair budget exhausted"):
        execute(native_code, "print('four')", decision="fourth", repair=third["job_id"])
    with pytest.raises(ValueError, match="repair budget exhausted"):
        execute(native_code, "print('branch')", decision="branch", repair=first["job_id"])


def test_native_memory_and_workspace_limits(native_code):
    service = native_code[1]
    service.sandbox.policy.max_memory_mb = 128
    memory = execute(native_code, "allocation = bytearray(300 * 1024 * 1024)")
    assert memory["execution_status"] == "failed", memory
    assert "MemoryError" in memory["stderr"], memory
    service.sandbox.policy.max_memory_mb = 512
    service.sandbox.policy.max_disk_mb = 1
    disk = execute(native_code, "from pathlib import Path; import time; Path('large.bin').write_bytes(b'x' * (8*1024*1024)); time.sleep(3)", decision="disk")
    assert disk["failure_kind"] == "workspace_limit_exceeded", disk


@pytest.mark.parametrize("crash_supervisor", [False, True])
def test_native_background_children_stop_on_cancel_or_supervisor_crash(native_code, crash_supervisor):
    from game_test_agent.analysis.windows_job import process_identity
    import ctypes
    from ctypes import wintypes

    async def run():
        view, service, source, configured = native_code
        task = asyncio.create_task(service.execute(view.investigation_id, "child", CodeExecutionRequest(
            language="python", purpose="process tree lifecycle", input_evidence_ids=[source.evidence_id],
            code="""import json, os, subprocess, sys, time
from pathlib import Path
child=subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(60)'])
(Path(os.environ['GTA_OUTPUT_DIR'])/'child.json').write_text(json.dumps({'pid':child.pid}))
time.sleep(60)
""")))
        for _ in range(400):
            jobs = service.repository.list_analysis_jobs(view.investigation_id)
            if jobs:
                directory = service.runner._directory(jobs[0].spec.job_id)
                child_file = directory / "work/output/child.json"
                if child_file.exists():
                    break
            if task.done():
                pytest.fail(f"job ended before child started: {task.result()}")
            await asyncio.sleep(.05)
        else:
            task.cancel()
            pytest.fail("native child did not start")
        child_pid = json.loads(child_file.read_bytes())["pid"]
        assert process_identity(child_pid) is not None
        accepted = (directory / "accepted.json").read_bytes()
        if crash_supervisor:
            owner = json.loads(accepted)
            assert process_identity(owner["pid"]) == owner["creation_token"]
            api = ctypes.WinDLL("kernel32", use_last_error=True)
            api.OpenProcess.argtypes, api.OpenProcess.restype = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE
            api.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
            api.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = api.OpenProcess(1, False, owner["pid"])
            assert handle
            try:
                assert api.TerminateProcess(handle, 1)
            finally:
                api.CloseHandle(handle)
            result = await asyncio.wait_for(task, timeout=15)
            assert result["failure_kind"] == "native_supervisor_lost", result
            resumed = build_code_execution(configured, service.repository, service.artifacts)
            resumed.runner.submit = AsyncMock(side_effect=AssertionError("accepted code must never rerun"))
            replay = await resumed.resume_investigation(view.investigation_id)
            assert replay[0]["job_id"] == result["job_id"]
            assert replay[0]["failure_kind"] == "native_supervisor_lost"
            resumed.runner.submit.assert_not_called()
            assert (directory / "accepted.json").read_bytes() == accepted
        else:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        for _ in range(100):
            if process_identity(child_pid) is None:
                break
            await asyncio.sleep(.05)
        assert process_identity(child_pid) is None, "background child survived sandbox cleanup"
        assert not service.repository.pending_cleanup_resources(view.investigation_id)

    asyncio.run(run())


def test_typed_controller_uses_sandbox_and_observes_failure_before_repair(native_code):
    from game_test_agent.activities import ActionService, ReasoningService
    from game_test_agent.capabilities import CapabilityRegistry
    from game_test_agent.evidence import EvidenceManifestIngestor
    from game_test_agent.mcp_gateway import ActionDispatcher
    from game_test_agent.planning import ActionPlanner
    from game_test_agent.policies import PolicyEngine
    from game_test_agent.reports import ReportService
    from game_test_agent.workflows import InvestigationController
    view, service, source, _ = native_code
    class Model:
        model_name = "typed-sandbox-fixture"
        calls = 0
        async def reason_next_step(self, context):
            self.calls += 1
            base = {"objective_id": "analyze", "expected_observation": "bounded analysis",
                    "reason_summary": "analyze selected evidence"}
            if self.calls == 1:
                return NextStep(kind="execute_code", code_request=CodeExecutionRequest(
                    language="python", code="print(missing)", purpose="first attempt",
                    input_evidence_ids=[source.evidence_id]), **base)
            jobs = context["working_state"]["analysis_jobs"]
            if self.calls == 2:
                assert jobs[-1]["state"] == "failed"
                assert any("NameError" in e["text"] for e in context["excerpts"])
                return NextStep(kind="execute_code", code_request=CodeExecutionRequest(
                    language="python", code="print('corrected-analysis')", purpose="repair attempt",
                    input_evidence_ids=[source.evidence_id], repair_of_job_id=jobs[-1]["job_id"]), **base)
            assert jobs[-1]["state"] == "succeeded"
            assert any("corrected-analysis" in e["text"] for e in context["excerpts"])
            return NextStep(kind="report", **base)
    registry = CapabilityRegistry(service.repository)
    controller = InvestigationController(
        service.repository, registry, ReasoningService(service.repository, Model()),
        ActionPlanner("disabled", enabled=False),
        ActionService(service.repository, PolicyEngine(service.repository, registry), ActionDispatcher()),
        EvidenceManifestIngestor(service.repository, service.artifacts), ReportService(service.repository),
        provider_lookup=lambda _: None, code_execution=service,
    )
    report = asyncio.run(controller.run(view.investigation_id, principal_id="test",
                                       scopes={"runs:read", "evidence:read"}))
    assert report is not None
    assert [j.state for j in service.repository.list_analysis_jobs(view.investigation_id)] == ["failed", "succeeded"]
    assert not service.repository.list_actions(view.investigation_id)


def test_temporal_code_activity_uses_the_same_real_runner(native_code):
    pytest.importorskip("temporalio")
    from game_test_agent.workflows.worker import CoreActivities
    view, service, source, configured = native_code
    activities = CoreActivities(service.repository, configured)
    context = activities.context.build(view.investigation_id, scopes={"evidence:read"})
    prepared = activities.kernel.prepare_round(context, plan_revision=1, model_name="activity-fixture",
                                               prompt_version="sandbox-fixture-v1")
    assert prepared.snapshot is not None
    activities.kernel.accept_decision(prepared.snapshot, NextStep(
        kind="execute_code", objective_id="analyze", expected_observation="analyzed",
        reason_summary="test durable activity integration", code_request=CodeExecutionRequest(
            language="shell", code="Write-Output 'temporal-activity-analysis'", purpose="activity analysis",
            input_evidence_ids=[source.evidence_id],
        ),
    ))
    result = asyncio.run(activities.execute_readonly_decision(view.investigation_id, 1))
    assert result["execution_status"] == "succeeded", result
    assert result["stdout"].strip() == "temporal-activity-analysis"


def test_native_rejects_cross_investigation_input_and_duplicate_program(native_code):
    view, service, source, _ = native_code
    with pytest.raises(PermissionError, match="belong"):
        asyncio.run(service.execute(view.investigation_id, "invalid", CodeExecutionRequest(
            language="python", code="print('no')", purpose="x", input_evidence_ids=["unknown"],
        )))
    assert not service.repository.list_analysis_jobs(view.investigation_id)
    failed = execute(native_code, "raise ValueError('first')")
    with pytest.raises(ValueError, match="identical"):
        execute(native_code, "raise ValueError('first')", decision="duplicate", repair=failed["job_id"])
    with pytest.raises(ValueError, match="failed job"):
        execute(native_code, "print('ok')", decision="invalid-repair", repair="foreign-job")


def test_sandbox_requires_strong_runtime_in_production(settings):
    config = replace(settings, environment="production", workflow_backend="temporal",
        database_backend="mysql", artifact_backend="s3", auth_mode="jwt",
        mysql_user="test", mysql_password="test", s3_bucket="test-bucket",
        jwt_jwks_url="https://auth.test/jwks", jwt_issuer="test", jwt_audience="agent",
        sandbox_backend="windows", sandbox_runtime_digest="sha256:"+"a"*64,
        sandbox_runner_ids=("runner",), sandbox_runtime="unelevated")
    with pytest.raises(ValueError, match="psec"):
        config.validate()


def test_typed_code_request_is_bounded_and_exclusive(repository, settings):
    from game_test_agent.capabilities import CapabilityRegistry
    from game_test_agent.evidence.context import ContextBuilder
    from game_test_agent.planning.decision_validator import DecisionValidator
    view, _ = repository.create_investigation(InvestigationRequest(
        tenant_id="studio-a", project_id="game-a",
        objective={"objective_id": "analyze", "description": "分析"},
    ), "validation")
    context = ContextBuilder(repository, CapabilityRegistry(repository)).build(view.investigation_id, scopes=set())
    request = CodeExecutionRequest(language="shell", code="echo hello", purpose="check", input_evidence_ids=["ev"])
    step = NextStep(kind="execute_code", objective_id="analyze", expected_observation="x", reason_summary="x", code_request=request)
    with pytest.raises(ValueError, match="unavailable"):
        DecisionValidator().validate(context, step)
    with pytest.raises(ValueError, match="only valid"):
        DecisionValidator().validate(context, step.model_copy(update={"kind": "report"}))
