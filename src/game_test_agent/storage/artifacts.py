from __future__ import annotations

import hashlib
import uuid
from base64 import b64encode
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import quote, unquote, urlparse

if TYPE_CHECKING:
    from game_test_agent.config import Settings


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    ref: str
    sha256: str
    size: int


class ArtifactStore(Protocol):
    def put_bytes(self, tenant_id: str, project_id: str, content: bytes) -> ArtifactRef: ...

    def read_bytes(self, ref: str) -> bytes: ...

    def validate_scope(self, ref: str, tenant_id: str, project_id: str) -> bool: ...


class LocalArtifactStore:
    """Content-addressed development store; callers never choose filesystem paths."""

    def __init__(self, root: Path | str, *, max_object_bytes: int = 128 * 1024 * 1024) -> None:
        if max_object_bytes < 1:
            raise ValueError("max_object_bytes must be positive")
        self.root = Path(root).resolve()
        self.max_object_bytes = max_object_bytes
        self.root.mkdir(parents=True, exist_ok=True)

    def put_bytes(self, tenant_id: str, project_id: str, content: bytes) -> ArtifactRef:
        if len(content) > self.max_object_bytes:
            raise ValueError("artifact exceeds the configured object size limit")
        digest = hashlib.sha256(content).hexdigest()
        safe_tenant = self._segment(tenant_id)
        safe_project = self._segment(project_id)
        target = (self.root / safe_tenant / safe_project / digest[:2] / digest).resolve()
        if self.root not in target.parents:
            raise ValueError("artifact path escaped configured root")
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            temporary = target.with_name(f".{digest}.{uuid.uuid4().hex}.tmp")
            try:
                temporary.write_bytes(content)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        return ArtifactRef(
            ref=f"artifact://{safe_tenant}/{safe_project}/{digest}",
            sha256=digest,
            size=len(content),
        )

    def read_bytes(self, ref: str) -> bytes:
        prefix = "artifact://"
        if not ref.startswith(prefix):
            raise ValueError("unsupported artifact reference")
        parts = ref[len(prefix) :].split("/")
        if len(parts) != 3 or len(parts[2]) != 64:
            raise ValueError("invalid artifact reference")
        tenant, project, digest = (self._segment(item) for item in parts)
        target = (self.root / tenant / project / digest[:2] / digest).resolve()
        if self.root not in target.parents:
            raise ValueError("artifact path escaped configured root")
        if target.stat().st_size > self.max_object_bytes:
            raise ValueError("stored artifact exceeds the configured size limit")
        with target.open("rb") as stream:
            content = stream.read(self.max_object_bytes + 1)
        if len(content) > self.max_object_bytes:
            raise ValueError("stored artifact exceeds the configured size limit")
        if hashlib.sha256(content).hexdigest() != digest:
            raise IOError("artifact content hash mismatch")
        return content

    def validate_scope(self, ref: str, tenant_id: str, project_id: str) -> bool:
        prefix = "artifact://"
        if not ref.startswith(prefix):
            return False
        parts = ref[len(prefix) :].split("/")
        if len(parts) != 3:
            return False
        try:
            tenant = self._segment(tenant_id)
            project = self._segment(project_id)
        except ValueError:
            return False
        return parts[0] == tenant and parts[1] == project

    @staticmethod
    def _segment(value: str) -> str:
        if not value or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
            for character in value
        ):
            raise ValueError("artifact identifiers may only contain letters, digits, '-' and '_'")
        return value


class S3ArtifactStore:
    """Content-addressed S3 store with strict reference and checksum validation."""

    def __init__(
        self,
        bucket: str,
        *,
        prefix: str = "game-test-agent",
        region: str | None = None,
        endpoint_url: str | None = None,
        server_side_encryption: str = "AES256",
        kms_key_id: str | None = None,
        max_object_bytes: int = 128 * 1024 * 1024,
        client: Any | None = None,
    ) -> None:
        if not bucket or any(character in bucket for character in "/\\"):
            raise ValueError("S3 bucket must be a non-empty bucket name")
        normalized_prefix = prefix.strip("/")
        if any(part in {"", ".", ".."} for part in normalized_prefix.split("/")):
            raise ValueError("S3 prefix contains an unsafe segment")
        if server_side_encryption not in {"AES256", "aws:kms"}:
            raise ValueError("S3 encryption must be AES256 or aws:kms")
        if server_side_encryption == "aws:kms" and not kms_key_id:
            raise ValueError("S3 KMS encryption requires a key ID")
        if max_object_bytes < 1:
            raise ValueError("max_object_bytes must be positive")
        self.bucket = bucket
        self.prefix = normalized_prefix
        self.server_side_encryption = server_side_encryption
        self.kms_key_id = kms_key_id
        self.max_object_bytes = max_object_bytes
        if client is None:
            try:
                import boto3
            except ImportError as error:  # pragma: no cover - optional dependency
                raise RuntimeError(
                    "install the 's3' optional dependency for S3 artifacts"
                ) from error
            client = boto3.client("s3", region_name=region, endpoint_url=endpoint_url)
        self.client = client

    def put_bytes(self, tenant_id: str, project_id: str, content: bytes) -> ArtifactRef:
        if len(content) > self.max_object_bytes:
            raise ValueError("artifact exceeds the configured S3 object size limit")
        tenant = LocalArtifactStore._segment(tenant_id)
        project = LocalArtifactStore._segment(project_id)
        digest = hashlib.sha256(content).hexdigest()
        key = f"{self.prefix}/{tenant}/{project}/{digest[:2]}/{digest}"
        parameters: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": key,
            "Body": content,
            "ContentType": "application/octet-stream",
            "Metadata": {
                "sha256": digest,
                "tenant": tenant,
                "project": project,
            },
            "ServerSideEncryption": self.server_side_encryption,
            "ChecksumSHA256": b64encode(bytes.fromhex(digest)).decode("ascii"),
        }
        if self.kms_key_id:
            parameters["SSEKMSKeyId"] = self.kms_key_id
        self.client.put_object(**parameters)
        return ArtifactRef(
            ref=f"s3+sha256://{quote(self.bucket, safe='')}/{quote(key, safe='/')}",
            sha256=digest,
            size=len(content),
        )

    def read_bytes(self, ref: str) -> bytes:
        parsed = urlparse(ref)
        if parsed.scheme != "s3+sha256" or unquote(parsed.netloc) != self.bucket:
            raise ValueError("artifact reference does not belong to the configured S3 bucket")
        key = unquote(parsed.path.lstrip("/"))
        expected_prefix = f"{self.prefix}/"
        if not key.startswith(expected_prefix):
            raise ValueError("artifact reference escaped the configured S3 prefix")
        prefix_parts = self.prefix.split("/")
        parts = key.split("/")
        scoped_parts = parts[len(prefix_parts) :]
        if parts[: len(prefix_parts)] != prefix_parts or len(scoped_parts) != 4:
            raise ValueError("invalid content-addressed S3 artifact reference")
        tenant, project, digest_prefix, digest = scoped_parts
        LocalArtifactStore._segment(tenant)
        LocalArtifactStore._segment(project)
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("invalid artifact digest")
        if digest_prefix != digest[:2]:
            raise ValueError("artifact digest prefix mismatch")
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        length = int(response.get("ContentLength", 0))
        if length > self.max_object_bytes:
            raise ValueError("stored artifact exceeds the configured size limit")
        body = response["Body"]
        try:
            content = body.read(self.max_object_bytes + 1)
        finally:
            close = getattr(body, "close", None)
            if close:
                close()
        if len(content) > self.max_object_bytes:
            raise ValueError("stored artifact exceeds the configured size limit")
        actual = hashlib.sha256(content).hexdigest()
        metadata_digest = response.get("Metadata", {}).get("sha256")
        if actual != digest or (metadata_digest and metadata_digest != digest):
            raise IOError("S3 artifact content hash mismatch")
        return content

    def validate_scope(self, ref: str, tenant_id: str, project_id: str) -> bool:
        parsed = urlparse(ref)
        if parsed.scheme != "s3+sha256" or unquote(parsed.netloc) != self.bucket:
            return False
        key_parts = unquote(parsed.path.lstrip("/")).split("/")
        prefix_parts = self.prefix.split("/")
        scoped = key_parts[len(prefix_parts) :]
        if key_parts[: len(prefix_parts)] != prefix_parts or len(scoped) != 4:
            return False
        try:
            tenant = LocalArtifactStore._segment(tenant_id)
            project = LocalArtifactStore._segment(project_id)
        except ValueError:
            return False
        return scoped[0] == tenant and scoped[1] == project


def create_artifact_store(settings: "Settings") -> ArtifactStore:
    if settings.artifact_backend == "local":
        return LocalArtifactStore(
            settings.artifact_root, max_object_bytes=settings.artifact_max_bytes
        )
    if settings.artifact_backend == "s3":
        return S3ArtifactStore(
            settings.s3_bucket,
            prefix=settings.s3_prefix,
            region=settings.s3_region,
            endpoint_url=settings.s3_endpoint_url,
            server_side_encryption=settings.s3_server_side_encryption,
            kms_key_id=settings.s3_kms_key_id,
            max_object_bytes=settings.artifact_max_bytes,
        )
    raise ValueError(f"unsupported artifact backend: {settings.artifact_backend}")
