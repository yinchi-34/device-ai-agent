async def _released_device_graph(client):
    """Build a released Pump profile through the public API."""
    category = (
        await client.post("/api/v1/device-categories", json={"name": "Pump"})
    ).json()
    capability = (
        await client.post(
            "/api/v1/capabilities",
            json={"code": "pressure_check", "name": "压力检查"},
        )
    ).json()
    await client.post(
        f"/api/v1/device-categories/{category['id']}/capabilities/{capability['id']}"
    )
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
    profile = (
        await client.post(
            f"/api/v1/device-models/{model['id']}/profiles", json={"version": "1.0"}
        )
    ).json()
    spec = (
        await client.post(
            f"/api/v1/profiles/{profile['id']}/protocols",
            json={
                "code": "modbus",
                "protocol": "modbus_tcp",
                "config_schema": {
                    "type": "object",
                    "properties": {
                        "host": {"type": "string"},
                        "port": {"type": "integer"},
                    },
                    "required": ["host", "port"],
                },
            },
        )
    ).json()
    points = {}
    for code, address in {
        "inlet_pressure": "40003",
        "outlet_pressure": "40004",
    }.items():
        point = (
            await client.post(
                f"/api/v1/protocols/{spec['id']}/points",
                json={
                    "code": code,
                    "name": code,
                    "data_type": "float32",
                    "address_text": address,
                },
            )
        ).json()
        points[code] = point
    pc = (
        await client.post(
            f"/api/v1/profiles/{profile['id']}/capabilities/{capability['id']}",
            json={},
        )
    ).json()["profile_capability"]
    for point in points.values():
        await client.post(
            f"/api/v1/profile-capabilities/{pc['id']}/point-bindings",
            json={"device_point_id": point["id"], "role": "measurement"},
        )
    await client.post(
        f"/api/v1/capabilities/{capability['id']}/command-templates",
        json={
            "command_code": "check",
            "command_name": "Check Pressures",
            "operation_template": {
                "operations": [
                    {"op": "read", "role": "measurement", "select": "all"}
                ]
            },
        },
    )
    release = await client.post(f"/api/v1/profiles/{profile['id']}/release", json={})
    assert release.status_code == 200
    return {
        "model": model,
        "profile": profile,
        "spec": spec,
        "points": points,
    }


async def test_device_context_and_test_options(client):
    graph = await _released_device_graph(client)

    created = await client.post(
        "/api/v1/devices",
        json={
            "device_model_id": graph["model"]["id"],
            "profile_version_id": graph["profile"]["id"],
            "instance_code": "PUMP-001",
            "instance_name": "Pump 001",
        },
    )
    assert created.status_code == 201
    device = created.json()

    context = (await client.get(f"/api/v1/devices/{device['id']}/context")).json()
    assert context["device"]["instance_code"] == "PUMP-001"
    assert context["profile"]["status"] == "released"
    assert context["protocol_specs"][0]["protocol"] == "modbus_tcp"

    connection = await client.post(
        f"/api/v1/devices/{device['id']}/connections",
        json={
            "protocol_spec_id": graph["spec"]["id"],
            "name": "fieldbus",
            "config": {"host": "10.0.0.5"},
        },
    )
    assert connection.status_code == 201
    connection_id = connection.json()["id"]

    validation = await client.post(
        f"/api/v1/connections/{connection_id}/validate-config"
    )
    assert validation.status_code == 200
    assert validation.json()["status"] == "invalid"
    assert validation.json()["errors"]

    options = (
        await client.get(f"/api/v1/devices/{device['id']}/test-options")
    ).json()
    capability = options["capabilities"][0]
    assert capability["code"] == "pressure_check"
    assert capability["name"] == "压力检查"
    assert {p["code"] for p in capability["points"]} == {
        "inlet_pressure",
        "outlet_pressure",
    }
    assert capability["commands"] == ["pressure_check.check"]
    assert {p["code"] for p in options["points"]} == {
        "inlet_pressure",
        "outlet_pressure",
    }


async def test_create_device_rejects_draft_profile(client):
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
        "/api/v1/devices",
        json={
            "device_model_id": model["id"],
            "profile_version_id": profile["id"],
            "instance_code": "X-1",
            "instance_name": "X 1",
        },
    )
    assert response.status_code == 409
    assert response.json()["code"] == "profile_not_released"


async def test_validate_config_needs_existing_connection(client):
    response = await client.post("/api/v1/connections/999/validate-config")
    assert response.status_code == 404


async def test_connection_rejects_foreign_protocol_spec(client):
    graph = await _released_device_graph(client)
    device = (
        await client.post(
            "/api/v1/devices",
            json={
                "device_model_id": graph["model"]["id"],
                "profile_version_id": graph["profile"]["id"],
                "instance_code": "PUMP-002",
                "instance_name": "Pump 002",
            },
        )
    ).json()
    # A protocol spec from another (draft) profile.
    other_profile = (
        await client.post(
            f"/api/v1/device-models/{graph['model']['id']}/profiles",
            json={"version": "2.0"},
        )
    ).json()
    other_spec = (
        await client.post(
            f"/api/v1/profiles/{other_profile['id']}/protocols",
            json={"code": "mqtt", "protocol": "mqtt"},
        )
    ).json()
    response = await client.post(
        f"/api/v1/devices/{device['id']}/connections",
        json={"protocol_spec_id": other_spec["id"], "name": "bad"},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "protocol_profile_mismatch"
