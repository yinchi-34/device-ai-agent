from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.modules.diagnosis.router import build_diagnosis_provider
from tests.api.test_diagnosis_api import _make_released_device


class FailingProvider:
    async def decide(self, *, diagnosis, steps, tools):
        raise RuntimeError("provider unavailable")


async def test_model_failure_is_persisted_as_queryable_run_state(db_session):
    _, device, _ = await _make_released_device(db_session)
    app = create_app()
    app.dependency_overrides[build_diagnosis_provider] = lambda: FailingProvider()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/api/v1/diagnosis-runs",
            json={"device_id": device.id, "symptom": "device offline"},
        )
        executed = await client.post(
            f"/api/v1/diagnosis-runs/{created.json()['id']}/execute"
        )

        assert executed.status_code == 200
        assert executed.json()["status"] == "failed"
        assert executed.json()["error_code"] == "diagnosis_execution_failed"

        fetched = await client.get(
            f"/api/v1/diagnosis-runs/{created.json()['id']}"
        )
        assert fetched.json()["status"] == "failed"

