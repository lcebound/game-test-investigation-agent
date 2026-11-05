"""Run real dependency checks in an isolated stack; no model or platform calls.

Requires Docker Compose. Downloads a checksum-pinned Temporal CLI, creates only
uniquely named test containers/volumes, and removes its stack on exit.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import platform
import secrets
import socket
import subprocess
import sys
import tarfile
import time
import urllib.request
import uuid
import zipfile


ROOT = Path(__file__).resolve().parents[1]
CLI_VERSION = "1.9.1"
CLI_SHA256 = {
    "Windows": "babb65844835045c91fb98930fe10b81de28db80c8c609180473d2c0b18c7589",
    "Linux": "09a0326a51db84d02735e53542b9ebd8c4758daf47482a9ab0abce15844e60d5",
}


def temporal_cli(directory, download_proxy=None):
    system = platform.system()
    if system not in CLI_SHA256 or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise RuntimeError("automatic Temporal CLI setup supports Windows/Linux amd64")
    extension = "zip" if system == "Windows" else "tar.gz"
    filename = f"temporal_cli_{CLI_VERSION}_{system.lower()}_amd64.{extension}"
    archive = directory / filename
    if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != CLI_SHA256[system]:
        print(f"Downloading Temporal CLI {CLI_VERSION}", flush=True)
        # Proxy use is explicit; SOCKS-only workstation settings are not HTTP proxies.
        proxies = {"http": download_proxy, "https": download_proxy} if download_proxy else {}
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
        url = f"https://github.com/temporalio/cli/releases/download/v{CLI_VERSION}/{filename}"
        temporary = archive.with_suffix(archive.suffix + ".part")
        with opener.open(url, timeout=30) as response, temporary.open("wb") as output:
            while chunk := response.read(64 * 1024):
                output.write(chunk)
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != CLI_SHA256[system]:
            raise RuntimeError("downloaded Temporal CLI archive checksum mismatch")
        temporary.replace(archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != CLI_SHA256[system]:
        raise RuntimeError("Temporal CLI archive checksum mismatch; remove the partial archive and retry")
    name = "temporal.exe" if system == "Windows" else "temporal"
    binary = directory / name
    # Extract exactly one expected file, never arbitrary archive paths.
    if extension == "zip":
        with zipfile.ZipFile(archive) as source:
            binary.write_bytes(source.read(name))
    else:
        with tarfile.open(archive) as source:
            member = source.extractfile(name)
            if member is None:
                raise RuntimeError("Temporal archive has no CLI binary")
            with member:
                binary.write_bytes(member.read())
        binary.chmod(0o755)
    return binary


def await_ready(probe, *, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if probe():
                return
        except Exception:
            pass
        time.sleep(.5)
    raise TimeoutError("isolated dependency did not become ready; inspect the saved service logs")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--s3-image", help="use an existing compatible image instead of building MinIO source")
    parser.add_argument("--mysql-image", default="mysql:8.4.10")
    parser.add_argument("--download-proxy", help="explicit HTTP(S) proxy for the checksum-verified CLI download")
    args = parser.parse_args()
    project = "gta-deps-" + uuid.uuid4().hex[:12]
    output = ROOT / "data/local" / project
    output.mkdir(parents=True)
    cache = ROOT / "data/local/dependency-tools"
    cache.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, GTA_TEST_MYSQL_PASSWORD=secrets.token_hex(16),
        GTA_TEST_S3_PASSWORD=secrets.token_hex(16), GTA_TEST_MYSQL_IMAGE=args.mysql_image,
        GTA_TEST_S3_KMS="integration:" + base64.b64encode(secrets.token_bytes(32)).decode())
    if args.s3_image:
        env["GTA_TEST_S3_IMAGE"] = args.s3_image
    compose = ["docker", "compose", "--project-name", project,
               "--file", str(ROOT / "deploy/integration/compose.yml")]
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    def docker(*arguments, capture=False, check=True):
        return subprocess.run([*compose, *arguments], cwd=ROOT, env=env, check=check,
            text=True, stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None, **flags)
    server = None
    try:
        binary = temporal_cli(cache, args.download_proxy)
        command = ["up", "--detach", "--no-build"] if args.s3_image else ["up", "--detach", "--build"]
        docker(*command)
        def port(service, target):
            return int(docker("port", service, str(target), capture=True).stdout.strip().rsplit(":", 1)[1])
        mysql_port, s3_port = port("mysql", 3306), port("s3", 9000)
        import pymysql
        def mysql_ready():
            connection = pymysql.connect(host="127.0.0.1", port=mysql_port, user="root",
                password=env["GTA_TEST_MYSQL_PASSWORD"], database="gta_dependency_checks", connect_timeout=2)
            connection.close()
            return True
        await_ready(mysql_ready)
        import httpx
        with httpx.Client(trust_env=False, timeout=2) as client:
            await_ready(lambda: client.get(f"http://127.0.0.1:{s3_port}/minio/health/ready").status_code == 200)
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            temporal_port = reservation.getsockname()[1]
        with (output / "temporal.log").open("wb") as log:
            server = subprocess.Popen([str(binary), "server", "start-dev", "--ip", "127.0.0.1",
                "--port", str(temporal_port), "--headless", "--db-filename", str(output / "temporal.db")],
                env=env, stdout=log, stderr=subprocess.STDOUT, **flags)
        def temporal_ready():
            if server.poll() is not None:
                raise RuntimeError("Temporal CLI exited")
            with socket.create_connection(("127.0.0.1", temporal_port), timeout=1):
                return True
        await_ready(temporal_ready)
        child_env = dict(env, GTA_DEPENDENCY_INTEGRATION="1", GTA_TEST_MYSQL_PORT=str(mysql_port),
            GTA_TEST_S3_ENDPOINT=f"http://127.0.0.1:{s3_port}",
            GTA_TEST_TEMPORAL_ADDRESS=f"127.0.0.1:{temporal_port}", PYTHONUTF8="1")
        completed = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests/integration",
            f"--junitxml={output / 'results.xml'}"], cwd=ROOT, env=child_env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", **flags)
        code = completed.returncode
        text = completed.stdout
        for key in ("GTA_TEST_MYSQL_PASSWORD", "GTA_TEST_S3_PASSWORD", "GTA_TEST_S3_KMS"):
            text = text.replace(env[key], "[redacted]")
        (output / "pytest.log").write_text(text, encoding="utf-8")
        print(text, flush=True)
        report = output / "results.xml"
        if report.exists():
            text = report.read_text(encoding="utf-8")
            for key in ("GTA_TEST_MYSQL_PASSWORD", "GTA_TEST_S3_PASSWORD", "GTA_TEST_S3_KMS"):
                text = text.replace(env[key], "[redacted]")
            report.write_text(text, encoding="utf-8")
        (output / "result.json").write_text(json.dumps({"exit_code": code,
            "temporal_cli_version": CLI_VERSION, "mysql_image": args.mysql_image,
            "s3_image": args.s3_image or "built-official-source-2025-10-15",
            "real_task_evaluation": False}, indent=2), encoding="utf-8")
        print(f"Dependency check artifacts: {output}", flush=True)
        return code
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=10)
        logs = docker("logs", "--no-color", capture=True, check=False)
        # These are isolated service logs. Test passwords are never written to artifacts.
        text = (logs.stdout or "") + (logs.stderr or "")
        for key in ("GTA_TEST_MYSQL_PASSWORD", "GTA_TEST_S3_PASSWORD", "GTA_TEST_S3_KMS"):
            text = text.replace(env[key], "[redacted]")
        (output / "services.log").write_text(text, encoding="utf-8")
        docker("down", "--volumes", "--remove-orphans", check=False)


if __name__ == "__main__":
    raise SystemExit(main())
