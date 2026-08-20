async def test_create_category_and_duplicate(client):
    response = await client.post(
        "/api/v1/device-categories", json={"name": "Pump"}
    )
    assert response.status_code == 201
    assert response.json()["name"] == "Pump"

    duplicate = await client.post(
        "/api/v1/device-categories", json={"name": "pump"}
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "duplicate"


async def test_create_and_list_models(client):
    category = (
        await client.post("/api/v1/device-categories", json={"name": "Pump"})
    ).json()
    response = await client.post(
        "/api/v1/device-models",
        json={
            "vendor_name": "ACME",
            "model_code": "PUMP-X200",
            "model_name": "Pump X200",
            "device_category_id": category["id"],
        },
    )
    assert response.status_code == 201
    models = (await client.get("/api/v1/device-models")).json()
    assert [m["model_code"] for m in models] == ["PUMP-X200"]


async def test_create_model_with_missing_category_404(client):
    response = await client.post(
        "/api/v1/device-models",
        json={
            "vendor_name": "ACME",
            "model_code": "X",
            "model_name": "X",
            "device_category_id": 999,
        },
    )
    assert response.status_code == 404


async def test_create_model_malformed_input_422(client):
    response = await client.post("/api/v1/device-models", json={"vendor_name": "A"})
    assert response.status_code == 422


async def test_profile_protocol_point_flow(client):
    category = (
        await client.post("/api/v1/device-categories", json={"name": "Pump"})
    ).json()
    model = (
        await client.post(
            "/api/v1/device-models",
            json={
                "vendor_name": "ACME",
                "model_code": "PUMP-X200",
                "model_name": "Pump X200",
                "device_category_id": category["id"],
            },
        )
    ).json()
    profile_response = await client.post(
        f"/api/v1/device-models/{model['id']}/profiles", json={"version": "1.0"}
    )
    assert profile_response.status_code == 201
    profile = profile_response.json()
    assert profile["status"] == "draft"

    fetched = await client.get(f"/api/v1/profiles/{profile['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["version"] == "1.0"

    spec_response = await client.post(
        f"/api/v1/profiles/{profile['id']}/protocols",
        json={
            "code": "modbus",
            "protocol": "modbus_tcp",
            "config_schema": {"type": "object"},
        },
    )
    assert spec_response.status_code == 201
    spec = spec_response.json()

    point_response = await client.post(
        f"/api/v1/protocols/{spec['id']}/points",
        json={
            "code": "run_status",
            "name": "Run Status",
            "data_type": "int16",
            "address_text": "40001",
        },
    )
    assert point_response.status_code == 201

    duplicate_point = await client.post(
        f"/api/v1/protocols/{spec['id']}/points",
        json={"code": "run_status", "name": "Dup", "data_type": "int16"},
    )
    assert duplicate_point.status_code == 409

    points = (await client.get(f"/api/v1/profiles/{profile['id']}/points")).json()
    assert [p["code"] for p in points] == ["run_status"]


async def test_missing_profile_404(client):
    response = await client.get("/api/v1/profiles/999")
    assert response.status_code == 404


async def test_duplicate_profile_version_409(client):
    model = (
        await client.post(
            "/api/v1/device-models",
            json={"vendor_name": "A", "model_code": "M", "model_name": "M"},
        )
    ).json()
    await client.post(
        f"/api/v1/device-models/{model['id']}/profiles", json={"version": "1.0"}
    )
    duplicate = await client.post(
        f"/api/v1/device-models/{model['id']}/profiles", json={"version": "1.0"}
    )
    assert duplicate.status_code == 409


async def test_invalid_config_schema_rejected(client):
    model = (
        await client.post(
            "/api/v1/device-models",
            json={"vendor_name": "A", "model_code": "M", "model_name": "M"},
        )
    ).json()
    profile = (
        await client.post(
            f"/api/v1/device-models/{model['id']}/profiles", json={"version": "1.0"}
        )
    ).json()
    response = await client.post(
        f"/api/v1/profiles/{profile['id']}/protocols",
        json={
            "code": "modbus",
            "protocol": "modbus_tcp",
            "config_schema": {"type": "bogus-type"},
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_config_schema"
