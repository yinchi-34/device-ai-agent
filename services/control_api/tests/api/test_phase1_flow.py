"""Phase 1 end-to-end acceptance: the exact chain from the plan, Task 11."""


async def test_phase1_full_business_flow(client):
    # 1. create DeviceCategory Pump
    category = (
        await client.post("/api/v1/device-categories", json={"name": "Pump"})
    ).json()
    # 2-3. create BasicCapability pressure_check / start_stop
    pressure = (
        await client.post(
            "/api/v1/capabilities",
            json={"code": "pressure_check", "name": "压力检查"},
        )
    ).json()
    start_stop = (
        await client.post(
            "/api/v1/capabilities",
            json={"code": "start_stop", "name": "Start / Stop"},
        )
    ).json()
    # 4. associate both with Pump
    for capability in (pressure, start_stop):
        link = await client.post(
            f"/api/v1/device-categories/{category['id']}"
            f"/capabilities/{capability['id']}"
        )
        assert link.status_code == 201
    # 5. create DeviceModel PUMP-X200
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
    # 6. create DRAFT Profile v1.0
    profile = (
        await client.post(
            f"/api/v1/device-models/{model['id']}/profiles", json={"version": "1.0"}
        )
    ).json()
    assert profile["status"] == "draft"
    # 7. create Modbus TCP ProtocolSpec
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
    # 8-12. create points
    points = {}
    point_defs = {
        "run_status": ("40001", "R"),
        "fault_code": ("40002", "R"),
        "inlet_pressure": ("40003", "R"),
        "outlet_pressure": ("40004", "R"),
        "control_word": ("40010", "RW"),
    }
    for code, (address, access) in point_defs.items():
        response = await client.post(
            f"/api/v1/protocols/{spec['id']}/points",
            json={
                "code": code,
                "name": code,
                "data_type": "int16",
                "address_text": address,
                "access_mode": access,
            },
        )
        assert response.status_code == 201
        points[code] = response.json()
    # 13-14. ProfileCapability pressure_check + measurement bindings
    pressure_pc = (
        await client.post(
            f"/api/v1/profiles/{profile['id']}/capabilities/{pressure['id']}",
            json={},
        )
    ).json()["profile_capability"]
    for code in ("inlet_pressure", "outlet_pressure"):
        binding = await client.post(
            f"/api/v1/profile-capabilities/{pressure_pc['id']}/point-bindings",
            json={"device_point_id": points[code]["id"], "role": "measurement"},
        )
        assert binding.status_code == 201
    # 15-16. ProfileCapability start_stop + role bindings
    start_stop_pc = (
        await client.post(
            f"/api/v1/profiles/{profile['id']}/capabilities/{start_stop['id']}",
            json={},
        )
    ).json()["profile_capability"]
    for code, role in [
        ("control_word", "control"),
        ("run_status", "status"),
        ("fault_code", "verify"),
    ]:
        binding = await client.post(
            f"/api/v1/profile-capabilities/{start_stop_pc['id']}/point-bindings",
            json={"device_point_id": points[code]["id"], "role": role},
        )
        assert binding.status_code == 201
    # 17. create start/stop global command templates
    for code, name, value in [("start", "Start", 1), ("stop", "Stop", 0)]:
        template = await client.post(
            f"/api/v1/capabilities/{start_stop['id']}/command-templates",
            json={
                "command_code": code,
                "command_name": name,
                "access_mode": "RW",
                "operation_template": {
                    "operations": [
                        {
                            "op": "write",
                            "role": "control",
                            "select": "one",
                            "value": value,
                        },
                        {"op": "read", "role": "status", "select": "one"},
                    ]
                },
            },
        )
        assert template.status_code == 201
    check_template = await client.post(
        f"/api/v1/capabilities/{pressure['id']}/command-templates",
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
    assert check_template.status_code == 201
    # 18. release profile
    release = await client.post(f"/api/v1/profiles/{profile['id']}/release", json={})
    assert release.status_code == 200
    # 19. generated start/stop DeviceCommands exist
    assert {"start_stop.start", "start_stop.stop"} <= set(
        release.json()["generated_commands"]
    )
    commands = (await client.get(f"/api/v1/profiles/{profile['id']}/commands")).json()
    start_cmd = next(c for c in commands if c["command_code"] == "start_stop.start")
    assert start_cmd["operations"][0]["device_point_id"] == points["control_word"]["id"]
    # 20. profile mutation now returns 409
    mutation = await client.post(
        f"/api/v1/protocols/{spec['id']}/points",
        json={"code": "extra", "name": "Extra", "data_type": "int16"},
    )
    assert mutation.status_code == 409
    assert mutation.json()["code"] == "profile_is_immutable"
    # 21. create DeviceInstance PUMP-001
    device_response = await client.post(
        "/api/v1/devices",
        json={
            "device_model_id": model["id"],
            "profile_version_id": profile["id"],
            "instance_code": "PUMP-001",
            "instance_name": "Pump 001",
        },
    )
    assert device_response.status_code == 201
    device = device_response.json()
    # 22. create Modbus Connection config
    connection = await client.post(
        f"/api/v1/devices/{device['id']}/connections",
        json={
            "protocol_spec_id": spec["id"],
            "name": "fieldbus",
            "config": {"host": "10.0.0.5", "port": 502},
        },
    )
    assert connection.status_code == 201
    # 23. validate config structure
    validation = await client.post(
        f"/api/v1/connections/{connection.json()['id']}/validate-config"
    )
    assert validation.status_code == 200
    assert validation.json()["status"] == "valid"
    # 24-26. GET test-options: capability quick-select + raw point advanced-select
    options = (
        await client.get(f"/api/v1/devices/{device['id']}/test-options")
    ).json()
    capability_codes = {c["code"] for c in options["capabilities"]}
    assert {"pressure_check", "start_stop"} <= capability_codes
    point_codes = {p["code"] for p in options["points"]}
    assert set(point_defs) <= point_codes
