from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from starlette.requests import Request

from game_test_agent.api.auth import JwtAuthenticator
from game_test_agent.api import create_app
from game_test_agent.config import Settings


class SigningKey:
    def __init__(self, key) -> None:
        self.key = key


class StaticJwksClient:
    def __init__(self, key) -> None:
        self.key = key

    def get_signing_key_from_jwt(self, token):
        return SigningKey(self.key)


def _request(token: str, tenant: str = "studio-a", project: str = "game-a") -> Request:
    headers = [
        (b"authorization", f"Bearer {token}".encode()),
        (b"x-tenant-id", tenant.encode()),
        (b"x-project-id", project.encode()),
    ]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers})


def _settings() -> Settings:
    return Settings(
        database_path=Path("unused.db"),
        artifact_root=Path("unused-artifacts"),
        auth_mode="jwt",
        jwt_jwks_url="https://identity.example/.well-known/jwks.json",
        jwt_issuer="https://identity.example/",
        jwt_audience="game-test-agent",
    )


def test_jwt_auth_uses_fixed_algorithm_and_claim_scope():
    async def scenario() -> None:
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.now(timezone.utc)
        token = jwt.encode(
            {
                "sub": "engineer-1",
                "iss": "https://identity.example/",
                "aud": "game-test-agent",
                "iat": now,
                "exp": now + timedelta(minutes=5),
                "tenant_id": "studio-a",
                "project_ids": ["game-a", "game-b"],
                "scope": "runs:submit agent:ops",
            },
            private_key,
            algorithm="RS256",
            headers={"kid": "test-key"},
        )
        authenticator = JwtAuthenticator(
            _settings(), jwks_client=StaticJwksClient(private_key.public_key())
        )

        principal = await authenticator(_request(token))
        assert principal.principal_id == "engineer-1"
        assert principal.project_id == "game-a"
        assert principal.scopes == {"runs:submit", "agent:ops"}

        with pytest.raises(HTTPException) as denied:
            await authenticator(_request(token, project="other-game"))
        assert denied.value.status_code == 403

    asyncio.run(scenario())


def test_jwt_auth_rejects_wrong_audience_and_missing_bearer():
    async def scenario() -> None:
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.now(timezone.utc)
        token = jwt.encode(
            {
                "sub": "engineer-1",
                "iss": "https://identity.example/",
                "aud": "some-other-service",
                "iat": now,
                "exp": now + timedelta(minutes=5),
                "tenant_id": "studio-a",
                "project_ids": ["game-a"],
                "scope": "",
            },
            private_key,
            algorithm="RS256",
        )
        authenticator = JwtAuthenticator(
            _settings(), jwks_client=StaticJwksClient(private_key.public_key())
        )
        with pytest.raises(HTTPException) as invalid:
            await authenticator(_request(token))
        assert invalid.value.status_code == 401

        missing = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
        with pytest.raises(HTTPException) as absent:
            await authenticator(missing)
        assert absent.value.status_code == 401

    asyncio.run(scenario())


def test_api_uses_verified_jwt_identity_instead_of_principal_header(tmp_path):
    async def scenario() -> None:
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.now(timezone.utc)
        token = jwt.encode(
            {
                "sub": "jwt-user",
                "iss": "https://identity.example/",
                "aud": "game-test-agent",
                "iat": now,
                "exp": now + timedelta(minutes=5),
                "tenant_id": "studio-a",
                "project_ids": ["game-a"],
                "scope": "",
            },
            private_key,
            algorithm="RS256",
        )
        settings = Settings(
            database_path=tmp_path / "jwt-api.db",
            artifact_root=tmp_path / "artifacts",
            auth_mode="jwt",
            jwt_jwks_url="https://identity.example/.well-known/jwks.json",
            jwt_issuer="https://identity.example/",
            jwt_audience="game-test-agent",
        )
        authenticator = JwtAuthenticator(
            settings, jwks_client=StaticJwksClient(private_key.public_key())
        )
        app = create_app(settings, principal_authenticator=authenticator)
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/v1/investigations",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "X-Principal-ID": "spoofed-user",
                        "X-Tenant-ID": "studio-a",
                        "X-Project-ID": "game-a",
                        "Idempotency-Key": "jwt-create",
                    },
                    json={
                        "tenant_id": "studio-a",
                        "project_id": "game-a",
                        "objective": {"objective_id": "jwt", "description": "JWT 调查"},
                    },
                )
                assert response.status_code == 202
                authorization = app.state.container.repository.get_authorization(
                    response.json()["investigation_id"]
                )
                assert authorization["principal_id"] == "jwt-user"

    asyncio.run(scenario())
