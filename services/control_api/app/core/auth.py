import secrets

from fastapi import Request

from app.core.config import Settings
from app.core.errors import DomainError


def _configured_tokens(settings: Settings) -> list[str]:
    return [token.strip() for token in settings.api_tokens.split(",") if token.strip()]


def _bearer_token(request: Request) -> str | None:
    scheme, _, credentials = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not credentials.strip():
        return None
    return credentials.strip()


async def require_api_token(request: Request) -> None:
    """Reject /api/v1 requests without a configured bearer token.

    Fails closed: when no tokens are configured the API answers 503
    instead of serving unauthenticated traffic.
    """
    tokens = _configured_tokens(Settings())
    if not tokens:
        raise DomainError(
            "api_auth_not_configured",
            "API authentication is not configured",
            status_code=503,
        )
    presented = _bearer_token(request)
    # Compare against every token so timing does not reveal which one matched.
    matched = False
    for token in tokens:
        if presented is not None and secrets.compare_digest(
            presented.encode(), token.encode()
        ):
            matched = True
    if not matched:
        raise DomainError(
            "unauthorized",
            "Missing or invalid API token",
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
