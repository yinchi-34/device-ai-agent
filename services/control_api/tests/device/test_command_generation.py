import pytest

from app.core.errors import DomainError
from app.modules.device import repository
from app.modules.device.command_generation import generate_profile_commands
from app.modules.device.service import DeviceService
from tests.device.helpers import (
    START_TEMPLATE,
    add_start_stop_templates,
    bind_start_stop,
    make_profile_graph,
)


async def _load_profile(session, profile_id):
    return await repository.get_profile_with_graph(session, profile_id)


async def test_template_references_roles_not_point_ids(db_session):
    graph = await make_profile_graph(db_session)
    await bind_start_stop(db_session, graph)
    await add_start_stop_templates(db_session, graph)
    templates = await repository.list_command_templates(
        db_session, graph["start_stop"].id
    )
    spec = templates[0].operation_template
    assert "device_point_id" not in str(spec)
    assert spec["operations"][0]["role"] == "control"


async def test_generated_operations_contain_concrete_point_snapshots(db_session):
    graph = await make_profile_graph(db_session)
    await bind_start_stop(db_session, graph)
    await add_start_stop_templates(db_session, graph)
    profile = await _load_profile(db_session, graph["profile"].id)
    commands = generate_profile_commands(profile)
    start = next(c for c in commands if c.command_code == "start_stop.start")
    write_op = start.operations[0]
    assert write_op["op"] == "write"
    assert write_op["value"] == 1
    assert write_op["device_point_id"] == graph["points"]["control_word"].id
    assert write_op["address_text"] == "40010"
    read_op = start.operations[1]
    assert read_op["device_point_id"] == graph["points"]["run_status"].id


async def test_missing_required_role_prevents_generation(db_session):
    graph = await make_profile_graph(db_session)
    service = DeviceService(db_session)
    pc, _ = await service.add_profile_capability(
        graph["profile"].id, graph["start_stop"].id
    )
    # Only bind status; control role required by the template is missing.
    await service.bind_capability_point(
        pc.id, graph["points"]["run_status"].id, "status"
    )
    await add_start_stop_templates(db_session, graph)
    profile = await _load_profile(db_session, graph["profile"].id)
    with pytest.raises(DomainError) as excinfo:
        generate_profile_commands(profile)
    assert excinfo.value.code == "missing_capability_role"


async def test_select_one_with_multiple_bindings_is_ambiguous(db_session):
    graph = await make_profile_graph(db_session)
    service = DeviceService(db_session)
    pc, _ = await service.add_profile_capability(
        graph["profile"].id, graph["start_stop"].id
    )
    points = graph["points"]
    await service.bind_capability_point(pc.id, points["control_word"].id, "control")
    await service.bind_capability_point(pc.id, points["run_status"].id, "status")
    # Second status point makes select=one ambiguous.
    await service.bind_capability_point(pc.id, points["fault_code"].id, "status")
    await add_start_stop_templates(db_session, graph)
    profile = await _load_profile(db_session, graph["profile"].id)
    with pytest.raises(DomainError) as excinfo:
        generate_profile_commands(profile)
    assert excinfo.value.code == "ambiguous_capability_role"


async def test_select_all_expands_bindings_ordered_by_sort_order(db_session):
    graph = await make_profile_graph(db_session)
    service = DeviceService(db_session)
    pc, _ = await service.add_profile_capability(
        graph["profile"].id, graph["pressure_check"].id
    )
    points = graph["points"]
    await service.bind_capability_point(
        pc.id, points["outlet_pressure"].id, "measurement", sort_order=2
    )
    await service.bind_capability_point(
        pc.id, points["inlet_pressure"].id, "measurement", sort_order=1
    )
    await service.create_command_template(
        graph["pressure_check"].id,
        "check",
        "Check Pressures",
        {"operations": [{"op": "read", "role": "measurement", "select": "all"}]},
    )
    profile = await _load_profile(db_session, graph["profile"].id)
    commands = generate_profile_commands(profile)
    check = next(c for c in commands if c.command_code == "pressure_check.check")
    assert [op["point_code"] for op in check.operations] == [
        "inlet_pressure",
        "outlet_pressure",
    ]


async def test_multiple_templates_produce_multiple_namespaced_commands(db_session):
    graph = await make_profile_graph(db_session)
    await bind_start_stop(db_session, graph)
    await add_start_stop_templates(db_session, graph)
    profile = await _load_profile(db_session, graph["profile"].id)
    commands = generate_profile_commands(profile)
    assert {c.command_code for c in commands} == {
        "start_stop.start",
        "start_stop.stop",
    }


async def test_generation_is_deterministic(db_session):
    graph = await make_profile_graph(db_session)
    await bind_start_stop(db_session, graph)
    await add_start_stop_templates(db_session, graph)
    profile = await _load_profile(db_session, graph["profile"].id)
    first = generate_profile_commands(profile)
    second = generate_profile_commands(profile)
    assert [c.source_hash for c in first] == [c.source_hash for c in second]
    assert [c.operations for c in first] == [c.operations for c in second]


async def test_invalid_template_rejected_at_creation(db_session):
    graph = await make_profile_graph(db_session)
    service = DeviceService(db_session)
    with pytest.raises(DomainError) as excinfo:
        await service.create_command_template(
            graph["start_stop"].id, "bad", "Bad", {"operations": []}
        )
    assert excinfo.value.code == "invalid_command_template"
    with pytest.raises(DomainError):
        await service.create_command_template(
            graph["start_stop"].id,
            "bad2",
            "Bad 2",
            {"operations": [{"op": "explode", "role": "control"}]},
        )


async def test_start_template_uses_expected_shape(db_session):
    assert START_TEMPLATE["operations"][0]["select"] == "one"
