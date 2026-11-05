from __future__ import annotations

from pathlib import Path

import pytest

from game_test_agent.config import Settings


def test_dev_header_auth_cannot_bind_public_interface_by_default():
    settings = Settings(
        database_path=Path("unused.db"),
        artifact_root=Path("unused-artifacts"),
        api_host="0.0.0.0",
    )

    with pytest.raises(ValueError, match="loopback"):
        settings.validate()


def test_jwks_endpoint_must_be_https():
    settings = Settings(
        database_path=Path("unused.db"),
        artifact_root=Path("unused-artifacts"),
        auth_mode="jwt",
        jwt_jwks_url="http://identity.example/jwks",
        jwt_issuer="https://identity.example/",
        jwt_audience="game-test-agent",
    )

    with pytest.raises(ValueError, match="HTTPS"):
        settings.validate()


def test_production_profile_fails_closed_on_development_backends():
    settings = Settings(
        database_path=Path("unused.db"),
        artifact_root=Path("unused-artifacts"),
        environment="production",
    )

    with pytest.raises(ValueError, match="production profile requires"):
        settings.validate()


def test_s3_security_configuration_is_validated_before_client_creation():
    insecure_endpoint = Settings(
        database_path=Path("unused.db"),
        artifact_root=Path("unused-artifacts"),
        artifact_backend="s3",
        s3_bucket="evidence",
        s3_endpoint_url="http://storage.example",
    )
    missing_kms_key = Settings(
        database_path=Path("unused.db"),
        artifact_root=Path("unused-artifacts"),
        s3_server_side_encryption="aws:kms",
    )

    with pytest.raises(ValueError, match="must use HTTPS"):
        insecure_endpoint.validate()
    with pytest.raises(ValueError, match="requires a key ID"):
        missing_kms_key.validate()


def test_zero_write_budgets_are_valid_but_negative_budgets_are_rejected():
    read_only = Settings(
        database_path=Path("unused.db"),
        artifact_root=Path("unused-artifacts"),
        max_real_runs=0,
        max_tool_reads=0,
        max_device_minutes=0,
    )
    read_only.validate()

    invalid = Settings(
        database_path=Path("unused.db"),
        artifact_root=Path("unused-artifacts"),
        max_tool_reads=-1,
    )
    with pytest.raises(ValueError, match="cannot be negative"):
        invalid.validate()


def test_model_transport_limits_are_validated():
    invalid_api = Settings(
        database_path=Path("unused.db"),
        artifact_root=Path("unused-artifacts"),
        model_api="legacy",
    )
    invalid_timeout = Settings(
        database_path=Path("unused.db"),
        artifact_root=Path("unused-artifacts"),
        model_timeout_seconds=0,
    )
    invalid_reasoning_effort = Settings(
        database_path=Path("unused.db"),
        artifact_root=Path("unused-artifacts"),
        model_reasoning_effort="extreme",
    )
    invalid_context_window = Settings(
        database_path=Path("unused.db"),
        artifact_root=Path("unused-artifacts"),
        model_context_window_tokens=4096,
        model_max_output_tokens=4096,
    )

    with pytest.raises(ValueError, match="model API"):
        invalid_api.validate()
    with pytest.raises(ValueError, match="model timeout"):
        invalid_timeout.validate()
    with pytest.raises(ValueError, match="reasoning effort"):
        invalid_reasoning_effort.validate()
    with pytest.raises(ValueError, match="context window"):
        invalid_context_window.validate()


def test_sandbox_configuration_is_disabled_by_default_and_digest_pinned(monkeypatch):
    defaults = Settings(
        database_path=Path("unused.db"),
        artifact_root=Path("unused-artifacts"),
    )
    defaults.validate()
    assert defaults.sandbox_image_digests == ()

    invalid = Settings(
        database_path=Path("unused.db"),
        artifact_root=Path("unused-artifacts"),
        sandbox_image_digests=("latest",),
        sandbox_runner_ids=("runner",),
    )
    with pytest.raises(ValueError, match="sha256"):
        invalid.validate()

    monkeypatch.setenv("GTA_SANDBOX_IMAGE_DIGESTS", "sha256:" + "a" * 64)
    monkeypatch.setenv("GTA_SANDBOX_RUNNER_IDS", "isolated-runner")
    loaded = Settings.from_env()
    assert loaded.sandbox_image_digests == ("sha256:" + "a" * 64,)
    assert loaded.sandbox_runner_ids == ("isolated-runner",)
