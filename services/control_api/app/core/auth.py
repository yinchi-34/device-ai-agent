"""Identity for /api/v1 requests.

The gateway authenticates callers against the identity provider and forwards
the provider-signed JWT. The API verifies that token itself (signature,
issuer, audience, expiry) instead of trusting plain identity headers, so a
request that bypasses the gateway cannot impersonate anyone.
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from fastapi import Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import Settings
from app.core.errors import DomainError

# Asymmetric only: "none" and HMAC algorithms are never accepted.
_ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "ES256", "ES384"]
_JWKS_TTL_SECONDS = 300
_JWKS_REFRESH_COOLDOWN_SECONDS = 30
_ACTOR_MAX_LENGTH = 255
DEFAULT_ACTOR_CLAIMS = ("email", "preferred_username", "azp", "client_id", "sub")

# auto_error=False keeps our own 401/503 bodies; the scheme still appears in OpenAPI.
_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Identity:
    actor: str
    claims: dict[str, Any]


@dataclass
class _JwksCache:
    jwks_url: str | None = None
    keys: jwt.PyJWKSet | None = None
    fetched_at: float = 0.0


_cache = _JwksCache()
_cache_lock = asyncio.Lock()


def reset_jwks_cache() -> None:
    global _cache
    _cache = _JwksCache()


async def _fetch_json(url: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.json()


async def _jwks_url(settings: Settings) -> str:
    if settings.oidc_jwks_url:
        return settings.oidc_jwks_url
    if _cache.jwks_url is None:
        discovery_url = (
            settings.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration"
        )
        _cache.jwks_url = (await _fetch_json(discovery_url))["jwks_uri"]
    return _cache.jwks_url


async def _signing_keys(settings: Settings, *, refresh: bool) -> jwt.PyJWKSet:
    async with _cache_lock:
        age = time.monotonic() - _cache.fetched_at
        stale = _cache.keys is None or age > _JWKS_TTL_SECONDS
        # Unknown key ids trigger a refresh (key rotation), but at most once
        # per cooldown so forged kids cannot hammer the identity provider.
        if stale or (refresh and age > _JWKS_REFRESH_COOLDOWN_SECONDS):
            jwks = await _fetch_json(await _jwks_url(settings))
            _cache.keys = jwt.PyJWKSet.from_dict(jwks)
            _cache.fetched_at = time.monotonic()
        return _cache.keys


def _find_key(keys: jwt.PyJWKSet, kid: str | None) -> jwt.PyJWK | None:
    for key in keys.keys:
        if kid is None or key.key_id == kid:
            return key
    return None


def actor_from_claims(claims: dict[str, Any], actor_claims: tuple[str, ...]) -> str:
    for name in actor_claims:
        value = claims.get(name)
        if isinstance(value, str) and value:
            return value[:_ACTOR_MAX_LENGTH]
    raise _unauthorized()


def _unauthorized() -> DomainError:
    return DomainError(
        "unauthorized",
        "Missing or invalid identity token",
        status_code=401,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def _verify(token: str, settings: Settings) -> dict[str, Any]:
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise _unauthorized() from exc
    if header.get("alg") not in _ALGORITHMS:
        raise _unauthorized()

    try:
        key = _find_key(await _signing_keys(settings, refresh=False), header.get("kid"))
        if key is None:
            key = _find_key(
                await _signing_keys(settings, refresh=True), header.get("kid")
            )
    except (httpx.HTTPError, KeyError, ValueError, jwt.PyJWTError) as exc:
        raise DomainError(
            "identity_provider_unavailable",
            "Could not load identity provider signing keys",
            status_code=503,
        ) from exc
    if key is None:
        raise _unauthorized()

    try:
        return jwt.decode(
            token,
            key.key,
            algorithms=_ALGORITHMS,
            audience=settings.oidc_audience,
            issuer=settings.oidc_issuer,
            leeway=settings.oidc_leeway_seconds,
            options={"require": ["exp", "iss", "aud"]},
        )
    except jwt.PyJWTError as exc:
        raise _unauthorized() from exc


async def require_identity(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> Identity:
    """Authenticate an /api/v1 request from its identity-provider JWT.

    Fails closed: without an issuer and audience configured the API answers
    503 instead of serving unauthenticated traffic.
    """
    settings = Settings()
    if not settings.oidc_issuer or not settings.oidc_audience:
        raise DomainError(
            "api_auth_not_configured",
            "API authentication is not configured",
            status_code=503,
        )
    if credentials is None or not credentials.credentials.strip():
        raise _unauthorized()

    claims = await _verify(credentials.credentials.strip(), settings)
    actor_claims = tuple(
        name.strip() for name in settings.oidc_actor_claims.split(",") if name.strip()
    )
    return Identity(actor=actor_from_claims(claims, actor_claims), claims=claims)
