from httpx import ASGITransport, AsyncClient

from app.main import create_app
from tests.api.test_testing_api import _released_device_graph


async def test_upload_page_is_available_without_database():
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as browser:
        page = await browser.get("/upload")
        assert page.status_code == 200
        assert 'type="file"' in page.text
        assert '.pdf,.docx' in page.text
        assert 'id="upload-form"' in page.text
        home = await browser.get("/")
        assert 'href="/upload"' in home.text


async def test_console_serves_without_database():
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as browser:
        page = await browser.get("/")
        assert page.status_code == 200
        assert "设备工作台" in page.text
        assert 'src="/console/app.js"' in page.text
        for path, content_type in [
            ("/console/app.js", "javascript"),
            ("/console/example.js", "javascript"),
            ("/console/style.css", "text/css"),
        ]:
            asset = await browser.get(path)
            assert asset.status_code == 200
            assert content_type in asset.headers["content-type"]


async def test_console_lists_profiles_and_devices_excluding_deleted(client, db_session):
    from app.modules.device.models import DeviceProfileVersion
    from app.modules.testing.models import DeviceInstance

    graph = await _released_device_graph(client)
    created = await client.post("/api/v1/devices", json={
        "device_model_id": graph["model"]["id"],
        "profile_version_id": graph["profile"]["id"],
        "instance_code": "CONSOLE-001", "instance_name": "Console device",
    })
    assert created.status_code == 201
    graph["device"] = created.json()
    profiles = await client.get("/api/v1/profiles")
    devices = await client.get("/api/v1/devices")
    assert profiles.status_code == devices.status_code == 200
    assert profiles.json()[0]["id"] == graph["profile"]["id"]
    assert devices.json()[0]["id"] == graph["device"]["id"]
    profile = await db_session.get(DeviceProfileVersion, graph["profile"]["id"])
    device = await db_session.get(DeviceInstance, graph["device"]["id"])
    profile.is_deleted = device.is_deleted = True
    await db_session.commit()
    assert (await client.get("/api/v1/profiles")).json() == []
    assert (await client.get("/api/v1/devices")).json() == []
