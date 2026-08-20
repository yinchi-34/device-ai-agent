async def _bootstrap_profile(client):
    category = (
        await client.post("/api/v1/device-categories", json={"name": "Pump"})
    ).json()
    start_stop = (
        await client.post(
            "/api/v1/capabilities",
            json={"code": "start_stop", "name": "Start / Stop"},
        )
    ).json()
    await client.post(
        f"/api/v1/device-categories/{category['id']}/capabilities/{start_stop['id']}"
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
            json={"code": "modbus", "protocol": "modbus_tcp"},
        )
    ).json()
    points = {}
    for code, address in {
        "run_status": "40001",
        "fault_code": "40002",
        "control_word": "40010",
    }.items():
        point = (
            await client.post(
                f"/api/v1/protocols/{spec['id']}/points",
                json={
                    "code": code,
                    "name": code.replace("_", " ").title(),
                    "data_type": "int16",
                    "address_text": address,
                    "access_mode": "RW" if code == "control_word" else "R",
                },
            )
        ).json()
        points[code] = point
    return {
        "category": category,
        "start_stop": start_stop,
        "model": model,
        "profile": profile,
        "spec": spec,
        "points": points,
    }


async def test_capability_dictionary_and_category_link(client):
    response = await client.post(
        "/api/v1/capabilities",
        json={"code": "start_stop", "name": "Start / Stop"},
    )
    assert response.status_code == 201

    duplicate = await client.post(
        "/api/v1/capabilities",
        json={"code": "START_STOP", "name": "Duplicate"},
    )
    assert duplicate.status_code == 409

    capabilities = (await client.get("/api/v1/capabilities")).json()
    assert [c["code"] for c in capabilities] == ["start_stop"]

    category = (
        await client.post("/api/v1/device-categories", json={"name": "Pump"})
    ).json()
    link = await client.post(
        f"/api/v1/device-categories/{category['id']}/capabilities/"
        f"{capabilities[0]['id']}"
    )
    assert link.status_code == 201

    repeat = await client.post(
        f"/api/v1/device-categories/{category['id']}/capabilities/"
        f"{capabilities[0]['id']}"
    )
    assert repeat.status_code == 409

    listed = (
        await client.get(f"/api/v1/device-categories/{category['id']}/capabilities")
    ).json()
    assert [c["code"] for c in listed] == ["start_stop"]


async def test_profile_capability_binding_and_release(client):
    graph = await _bootstrap_profile(client)
    profile_id = graph["profile"]["id"]
    capability_id = graph["start_stop"]["id"]
    points = graph["points"]

    created = await client.post(
        f"/api/v1/profiles/{profile_id}/capabilities/{capability_id}", json={}
    )
    assert created.status_code == 201
    body = created.json()
    assert body["outside_category_recommendation"] is False
    pc_id = body["profile_capability"]["id"]

    for point_code, role in [
        ("control_word", "control"),
        ("run_status", "status"),
        ("fault_code", "verify"),
    ]:
        binding = await client.post(
            f"/api/v1/profile-capabilities/{pc_id}/point-bindings",
            json={"device_point_id": points[point_code]["id"], "role": role},
        )
        assert binding.status_code == 201

    duplicate_binding = await client.post(
        f"/api/v1/profile-capabilities/{pc_id}/point-bindings",
        json={"device_point_id": points["run_status"]["id"], "role": "status"},
    )
    assert duplicate_binding.status_code == 409

    capabilities = (
        await client.get(f"/api/v1/profiles/{profile_id}/capabilities")
    ).json()
    assert capabilities[0]["capability_code"] == "start_stop"
    assert len(capabilities[0]["bindings"]) == 3

    for code, name, value in [("start", "Start", 1), ("stop", "Stop", 0)]:
        template = await client.post(
            f"/api/v1/capabilities/{capability_id}/command-templates",
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

    release = await client.post(f"/api/v1/profiles/{profile_id}/release", json={})
    assert release.status_code == 200
    assert release.json()["profile"]["status"] == "released"
    assert set(release.json()["generated_commands"]) == {
        "start_stop.start",
        "start_stop.stop",
    }

    commands = (await client.get(f"/api/v1/profiles/{profile_id}/commands")).json()
    start = next(c for c in commands if c["command_code"] == "start_stop.start")
    assert start["operations"][0]["device_point_id"] == points["control_word"]["id"]


async def test_release_then_mutation_returns_409(client):
    graph = await _bootstrap_profile(client)
    profile_id = graph["profile"]["id"]
    capability_id = graph["start_stop"]["id"]
    points = graph["points"]

    created = await client.post(
        f"/api/v1/profiles/{profile_id}/capabilities/{capability_id}", json={}
    )
    pc_id = created.json()["profile_capability"]["id"]
    for point_code, role in [
        ("control_word", "control"),
        ("run_status", "status"),
    ]:
        await client.post(
            f"/api/v1/profile-capabilities/{pc_id}/point-bindings",
            json={"device_point_id": points[point_code]["id"], "role": role},
        )
    await client.post(
        f"/api/v1/capabilities/{capability_id}/command-templates",
        json={
            "command_code": "start",
            "command_name": "Start",
            "operation_template": {
                "operations": [
                    {"op": "write", "role": "control", "select": "one", "value": 1}
                ]
            },
        },
    )
    release = await client.post(f"/api/v1/profiles/{profile_id}/release", json={})
    assert release.status_code == 200

    mutation = await client.post(
        f"/api/v1/protocols/{graph['spec']['id']}/points",
        json={"code": "extra", "name": "Extra", "data_type": "int16"},
    )
    assert mutation.status_code == 409
    assert mutation.json()["code"] == "profile_is_immutable"


async def test_release_with_missing_role_returns_409(client):
    graph = await _bootstrap_profile(client)
    profile_id = graph["profile"]["id"]
    capability_id = graph["start_stop"]["id"]
    created = await client.post(
        f"/api/v1/profiles/{profile_id}/capabilities/{capability_id}", json={}
    )
    pc_id = created.json()["profile_capability"]["id"]
    await client.post(
        f"/api/v1/profile-capabilities/{pc_id}/point-bindings",
        json={
            "device_point_id": graph["points"]["run_status"]["id"],
            "role": "status",
        },
    )
    await client.post(
        f"/api/v1/capabilities/{capability_id}/command-templates",
        json={
            "command_code": "start",
            "command_name": "Start",
            "operation_template": {
                "operations": [
                    {"op": "write", "role": "control", "select": "one", "value": 1}
                ]
            },
        },
    )
    release = await client.post(f"/api/v1/profiles/{profile_id}/release", json={})
    assert release.status_code == 409
    assert release.json()["code"] == "missing_capability_role"


async def test_invalid_template_returns_422(client):
    capability = (
        await client.post(
            "/api/v1/capabilities", json={"code": "x", "name": "X"}
        )
    ).json()
    response = await client.post(
        f"/api/v1/capabilities/{capability['id']}/command-templates",
        json={
            "command_code": "bad",
            "command_name": "Bad",
            "operation_template": {"operations": [{"op": "nope", "role": "control"}]},
        },
    )
    assert response.status_code == 422


async def test_missing_entities_return_404(client):
    response = await client.post(
        "/api/v1/device-categories/999/capabilities/999"
    )
    assert response.status_code == 404

    response = await client.post(
        "/api/v1/profile-capabilities/999/point-bindings",
        json={"device_point_id": 1, "role": "status"},
    )
    assert response.status_code == 404
