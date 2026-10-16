import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app


@pytest.fixture
async def bare_client():
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        yield client


@pytest.fixture
def api_tokens(monkeypatch):
    monkeypatch.setenv("DEVICE_API_TOKENS", "current-token, previous-token")


async def test_api_rejects_missing_token(api_tokens, bare_client):
    response = await bare_client.get("/api/v1/device-models")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json()["code"] == "unauthorized"


async def test_api_rejects_wrong_token(api_tokens, bare_client):
    response = await bare_client.get(
        "/api/v1/device-models",
        headers={"Authorization": "Bearer not-a-token"},
    )

    assert response.status_code == 401


async def test_api_rejects_non_bearer_scheme(api_tokens, bare_client):
    response = await bare_client.get(
        "/api/v1/device-models",
        headers={"Authorization": "Basic current-token"},
    )

    assert response.status_code == 401


@pytest.mark.parametrize("token", ["current-token", "previous-token"])
async def test_api_accepts_any_configured_token(api_tokens, bare_client, token):
    response = await bare_client.get(
        "/api/v1/device-models",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200


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
async def test_every_api_module_requires_token(api_tokens, bare_client, path):
    response = await bare_client.get(path)

    assert response.status_code == 401


async def test_api_fails_closed_without_configured_tokens(
    monkeypatch, bare_client
):
    monkeypatch.setenv("DEVICE_API_TOKENS", " , ")

    response = await bare_client.get(
        "/api/v1/device-models",
        headers={"Authorization": "Bearer "},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "api_auth_not_configured"


async def test_health_does_not_require_token(api_tokens, bare_client):
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
