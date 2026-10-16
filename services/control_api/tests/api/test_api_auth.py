import time

import httpx
import jwt
import pytest
from httpx import ASGITransport, AsyncClient

from app.core import auth
from app.main import create_app
from tests import oidc


@pytest.fixture
async def bare_client():
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        yield client


async def test_api_rejects_missing_token(bare_client):
    response = await bare_client.get("/api/v1/device-models")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json()["code"] == "unauthorized"


async def test_api_accepts_valid_identity_token(bare_client):
    response = await bare_client.get(
        "/api/v1/device-models", headers=oidc.auth_headers()
    )

    assert response.status_code == 200


@pytest.mark.parametrize(
    "overrides",
    [
        {"signed_by_other_key": True},
        {"iss": "https://evil.test"},
        {"aud": "another-app"},
        {"exp": int(time.time()) - 60},
        {"exp": None},
    ],
    ids=["bad-signature", "wrong-issuer", "wrong-audience", "expired", "no-exp"],
)
async def test_api_rejects_invalid_identity_token(bare_client, overrides):
    response = await bare_client.get(
        "/api/v1/device-models", headers=oidc.auth_headers(**overrides)
    )

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


async def test_api_rejects_unsigned_and_symmetric_tokens(bare_client):
    claims = jwt.decode(oidc.make_token(), options={"verify_signature": False})
    unsigned = jwt.encode(claims, key=None, algorithm="none")
    # A token "signed" with the public key as an HMAC secret must not pass.
    hmac_signed = jwt.encode(claims, "x" * 32, algorithm="HS256")

    for token in (unsigned, hmac_signed):
        response = await bare_client.get(
            "/api/v1/device-models", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 401


async def test_api_rejects_non_bearer_scheme(bare_client):
    response = await bare_client.get(
        "/api/v1/device-models",
        headers={"Authorization": f"Basic {oidc.make_token()}"},
    )

    assert response.status_code == 401


async def test_unknown_key_id_refreshes_jwks_with_cooldown(monkeypatch, bare_client):
    calls = []

    async def fetch(url):
        calls.append(url)
        return oidc.JWKS

    monkeypatch.setattr(auth, "_fetch_json", fetch)
    loaded = await bare_client.get("/api/v1/device-models", headers=oidc.auth_headers())
    auth._cache.fetched_at -= 60  # keys are past the refresh cooldown

    first = await bare_client.get(
        "/api/v1/device-models", headers=oidc.auth_headers(kid="rotated")
    )
    second = await bare_client.get(
        "/api/v1/device-models", headers=oidc.auth_headers(kid="rotated")
    )

    assert loaded.status_code == 200
    assert first.status_code == 401
    assert second.status_code == 401
    # Initial load, one refresh for the unknown kid, then the cooldown holds.
    assert calls == [oidc.JWKS_URL, oidc.JWKS_URL]


async def test_identity_provider_outage_returns_503(monkeypatch, bare_client):
    async def fetch(url):
        raise httpx.ConnectError("idp down")

    monkeypatch.setattr(auth, "_fetch_json", fetch)

    response = await bare_client.get(
        "/api/v1/device-models", headers=oidc.auth_headers()
    )

    assert response.status_code == 503
    assert response.json()["code"] == "identity_provider_unavailable"


async def test_jwks_url_is_discovered_from_issuer(monkeypatch, bare_client):
    monkeypatch.delenv("DEVICE_OIDC_JWKS_URL")
    fetched = []

    async def fetch(url):
        fetched.append(url)
        if url.endswith("/.well-known/openid-configuration"):
            return {"jwks_uri": oidc.JWKS_URL}
        return oidc.JWKS

    monkeypatch.setattr(auth, "_fetch_json", fetch)

    response = await bare_client.get(
        "/api/v1/device-models", headers=oidc.auth_headers()
    )

    assert response.status_code == 200
    assert fetched == [
        f"{oidc.ISSUER}/.well-known/openid-configuration",
        oidc.JWKS_URL,
    ]


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/device-models",
        "/api/v1/capabilities",
        "/api/v1/documents/00000000-0000-0000-0000-000000000000",
        "/api/v1/devices/00000000-0000-0000-0000-000000000000/context",
        "/api/v1/diagnosis-runs/00000000-0000-0000-0000-000000000000",
    ],
)
async def test_every_api_module_requires_identity(bare_client, path):
    response = await bare_client.get(path)

    assert response.status_code == 401


@pytest.mark.parametrize("missing", ["DEVICE_OIDC_ISSUER", "DEVICE_OIDC_AUDIENCE"])
async def test_api_fails_closed_without_oidc_config(monkeypatch, bare_client, missing):
    monkeypatch.setenv(missing, "")

    response = await bare_client.get(
        "/api/v1/device-models", headers=oidc.auth_headers()
    )

    assert response.status_code == 503
    assert response.json()["code"] == "api_auth_not_configured"


@pytest.mark.parametrize(
    ("claims", "expected"),
    [
        ({}, "reviewer@example.com"),
        ({"email": None, "preferred_username": "svc-gateway"}, "svc-gateway"),
        ({"email": None, "azp": "diagnosis-worker"}, "diagnosis-worker"),
        ({"email": None}, "user-123"),
    ],
)
def test_actor_prefers_human_readable_claims(claims, expected):
    token_claims = jwt.decode(
        oidc.make_token(**claims), options={"verify_signature": False}
    )

    assert auth.actor_from_claims(token_claims, auth.DEFAULT_ACTOR_CLAIMS) == expected


async def test_health_does_not_require_identity(bare_client):
    response = await bare_client.get("/health")

    assert response.status_code == 200


async def test_openapi_declares_bearer_security(bare_client):
    schema = (await bare_client.get("/openapi.json")).json()

    assert schema["components"]["securitySchemes"]["HTTPBearer"] == {
        "type": "http",
        "scheme": "bearer",
    }
    device_models = schema["paths"]["/api/v1/device-models"]["get"]
    assert device_models["security"] == [{"HTTPBearer": []}]
    assert "security" not in schema["paths"]["/health"]["get"]
