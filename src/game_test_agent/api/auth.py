from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from fastapi import HTTPException, Request

from game_test_agent.config import Settings


@dataclass(frozen=True, slots=True)
class Principal:
    principal_id: str
    tenant_id: str
    project_id: str
    scopes: frozenset[str]


def _required_header(request: Request, name: str, *, max_length: int) -> str:
    value = request.headers.get(name, "").strip()
    if not value or len(value) > max_length:
        raise HTTPException(status_code=422, detail=f"valid {name} header required")
    return value


class JwtAuthenticator:
    ALGORITHMS = ["RS256"]

    def __init__(self, settings: Settings, *, jwks_client: Any | None = None) -> None:
        if not settings.jwt_jwks_url or not settings.jwt_issuer or not settings.jwt_audience:
            raise ValueError("JWT mode requires JWKS URL, issuer and audience")
        self.settings = settings
        if jwks_client is None:
            try:
                import jwt
            except ImportError as error:  # pragma: no cover - optional dependency
                raise RuntimeError("install the 'auth' optional dependency for JWT auth") from error
            jwks_client = jwt.PyJWKClient(
                settings.jwt_jwks_url,
                cache_keys=True,
                lifespan=settings.jwt_jwks_cache_seconds,
                timeout=settings.jwt_jwks_timeout_seconds,
            )
        self.jwks_client = jwks_client

    async def __call__(self, request: Request) -> Principal:
        authorization = request.headers.get("Authorization", "")
        scheme, separator, token = authorization.partition(" ")
        if separator != " " or scheme.lower() != "bearer" or not token.strip():
            raise HTTPException(status_code=401, detail="Bearer token required")
        try:
            import jwt

            signing_key = self.jwks_client.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=self.ALGORITHMS,
                audience=self.settings.jwt_audience,
                issuer=self.settings.jwt_issuer,
                leeway=self.settings.jwt_leeway_seconds,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except Exception as error:
            raise HTTPException(status_code=401, detail="invalid access token") from error
        principal_id = claims.get("sub")
        tenant_id = claims.get("tenant_id")
        projects = claims.get("project_ids", [])
        if not isinstance(principal_id, str) or not principal_id:
            raise HTTPException(status_code=403, detail="token subject is missing")
        if not isinstance(tenant_id, str) or not tenant_id:
            raise HTTPException(status_code=403, detail="token tenant_id is missing")
        if not isinstance(projects, list) or not all(
            isinstance(item, str) and item for item in projects
        ):
            raise HTTPException(status_code=403, detail="token project_ids is invalid")
        selected_tenant = _required_header(request, "X-Tenant-ID", max_length=128)
        selected_project = _required_header(request, "X-Project-ID", max_length=128)
        if selected_tenant != tenant_id or selected_project not in projects:
            raise HTTPException(status_code=403, detail="requested scope is not granted by token")
        raw_scopes = claims.get("scope", claims.get("scopes", []))
        if isinstance(raw_scopes, str):
            scopes = raw_scopes.split()
        elif isinstance(raw_scopes, list) and all(isinstance(item, str) for item in raw_scopes):
            scopes = raw_scopes
        else:
            raise HTTPException(status_code=403, detail="token scopes claim is invalid")
        return Principal(
            principal_id=principal_id,
            tenant_id=tenant_id,
            project_id=selected_project,
            scopes=frozenset(scopes),
        )


def build_principal_dependency(
    settings: Settings, *, jwt_authenticator: JwtAuthenticator | None = None
) -> Callable[[Request], Awaitable[Principal]]:
    if settings.auth_mode == "jwt":
        authenticator = jwt_authenticator or JwtAuthenticator(settings)
        return authenticator
    if settings.auth_mode != "dev_headers":
        raise ValueError(f"unsupported auth mode: {settings.auth_mode}")

    async def development_headers(request: Request) -> Principal:
        principal_id = _required_header(request, "X-Principal-ID", max_length=256)
        tenant_id = _required_header(request, "X-Tenant-ID", max_length=128)
        project_id = _required_header(request, "X-Project-ID", max_length=128)
        raw_scopes = request.headers.get("X-Scopes", "")
        return Principal(
            principal_id=principal_id,
            tenant_id=tenant_id,
            project_id=project_id,
            scopes=frozenset(item.strip() for item in raw_scopes.split(",") if item.strip()),
        )

    return development_headers
