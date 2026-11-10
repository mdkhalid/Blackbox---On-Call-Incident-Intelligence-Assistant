"""Authentication and tenant context.

Auth is disabled for local development only. In production `AUTH_ENABLED=true` is mandatory;
tokens must carry a tenant and are rejected without one (fail closed).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_bearer = HTTPBearer(auto_error=False)
_BEARER_DEPENDENCY = Depends(_bearer)


class AuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class Principal:
    subject: str
    tenant_id: str
    roles: frozenset[str] = frozenset()
    claims: dict[str, Any] = field(default_factory=dict)

    def has_role(self, *roles: str) -> bool:
        return bool(self.roles.intersection(roles))


@lru_cache(maxsize=8)
def _jwks_client(url: str):
    from jwt import PyJWKClient

    return PyJWKClient(url)


def _claim_roles(claims: dict[str, Any]) -> frozenset[str]:
    raw = claims.get("roles") or claims.get("role") or claims.get("scope") or []
    if isinstance(raw, str):
        raw = raw.replace(",", " ").split()
    if not isinstance(raw, (list, tuple, set)):
        return frozenset()
    return frozenset(str(role).lower() for role in raw if str(role).strip())


def _tenant_from_claims(claims: dict[str, Any]) -> str:
    for key in ("tenant_id", "tid", "org_id"):
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise AuthError("token is missing a tenant claim")


def decode_principal(token: str, settings) -> Principal:
    import jwt

    kwargs: dict[str, Any] = {}
    if settings.auth_jwt_audience:
        kwargs["audience"] = settings.auth_jwt_audience
    if settings.auth_jwt_issuer:
        kwargs["issuer"] = settings.auth_jwt_issuer

    try:
        if settings.auth_jwt_secret:
            claims = jwt.decode(
                token,
                settings.auth_jwt_secret,
                algorithms=["HS256"],
                **kwargs,
            )
        elif settings.auth_jwks_url:
            key = _jwks_client(settings.auth_jwks_url).get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256", "RS384", "RS512", "ES256", "ES384"],
                **kwargs,
            )
        else:  # pragma: no cover - settings validation prevents this
            raise AuthError("no JWT verification material configured")
    except AuthError:
        raise
    except Exception as exc:
        raise AuthError("invalid or expired token") from exc

    return Principal(
        subject=str(claims.get("sub") or claims.get("email") or "unknown"),
        tenant_id=_tenant_from_claims(claims),
        roles=_claim_roles(claims),
        claims=dict(claims),
    )


def require_roles(*allowed: str):
    """FastAPI dependency factory enforcing authentication and role membership."""

    def dependency(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = _BEARER_DEPENDENCY,
    ) -> Principal:
        settings = request.app.state.settings
        if not settings.auth_enabled:
            # Local development only; production settings reject this configuration.
            return Principal(
                subject="dev",
                tenant_id=settings.auth_default_tenant,
                roles=frozenset({"admin", "operator", "viewer"}),
                claims={"dev": True},
            )

        if credentials is None:
            raise HTTPException(
                status_code=401,
                detail="missing bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            principal = decode_principal(credentials.credentials, settings)
        except AuthError as exc:
            raise HTTPException(
                status_code=401,
                detail=str(exc),
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc

        if allowed and not principal.has_role(*allowed):
            raise HTTPException(status_code=403, detail="insufficient role")
        return principal

    return dependency
