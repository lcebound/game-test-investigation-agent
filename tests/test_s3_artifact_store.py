from __future__ import annotations

import io

import pytest

from game_test_agent.storage import LocalArtifactStore, S3ArtifactStore


class FakeS3Client:
    def __init__(self) -> None:
        self.objects = {}
        self.last_put = None

    def put_object(self, **parameters):
        self.last_put = parameters
        self.objects[(parameters["Bucket"], parameters["Key"])] = {
            "content": parameters["Body"],
            "metadata": parameters["Metadata"],
        }

    def get_object(self, *, Bucket, Key):
        item = self.objects[(Bucket, Key)]
        return {
            "Body": io.BytesIO(item["content"]),
            "ContentLength": len(item["content"]),
            "Metadata": item["metadata"],
        }


def test_s3_artifacts_are_content_addressed_encrypted_and_hash_verified():
    client = FakeS3Client()
    store = S3ArtifactStore(
        "evidence-bucket",
        prefix="agents/evidence",
        server_side_encryption="aws:kms",
        kms_key_id="alias/game-test",
        client=client,
    )

    stored = store.put_bytes("studio-a", "game-a", b"artifact-content")

    assert stored.ref.startswith("s3+sha256://evidence-bucket/agents/evidence/studio-a/game-a/")
    assert client.last_put["ServerSideEncryption"] == "aws:kms"
    assert client.last_put["SSEKMSKeyId"] == "alias/game-test"
    assert client.last_put["Metadata"]["sha256"] == stored.sha256
    assert store.validate_scope(stored.ref, "studio-a", "game-a")
    assert not store.validate_scope(stored.ref, "studio-a", "other-game")
    assert store.read_bytes(stored.ref) == b"artifact-content"

    key = client.last_put["Key"]
    client.objects[("evidence-bucket", key)]["content"] = b"tampered"
    with pytest.raises(IOError, match="hash mismatch"):
        store.read_bytes(stored.ref)


def test_s3_store_rejects_scope_escape_and_invalid_encryption():
    client = FakeS3Client()
    store = S3ArtifactStore("evidence-bucket", client=client)

    with pytest.raises(ValueError):
        store.put_bytes("../tenant", "game-a", b"data")
    with pytest.raises(ValueError, match="configured S3 bucket"):
        store.read_bytes("s3+sha256://other/game-test-agent/a/b/00/" + "0" * 64)
    with pytest.raises(ValueError, match="content-addressed"):
        store.read_bytes("s3+sha256://evidence-bucket/game-test-agent/a/b/extra/00/" + "0" * 64)
    with pytest.raises(ValueError, match="requires a key ID"):
        S3ArtifactStore("evidence-bucket", server_side_encryption="aws:kms", client=client)


def test_local_store_applies_the_same_object_size_boundary(tmp_path):
    store = LocalArtifactStore(tmp_path / "artifacts", max_object_bytes=4)

    stored = store.put_bytes("studio", "game", b"1234")
    assert store.validate_scope(stored.ref, "studio", "game")
    assert not store.validate_scope(stored.ref, "other", "game")
    assert store.read_bytes(stored.ref) == b"1234"
    with pytest.raises(ValueError, match="object size limit"):
        store.put_bytes("studio", "game", b"12345")
