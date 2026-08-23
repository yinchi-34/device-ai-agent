import pytest

from app.core.errors import DomainError
from app.modules.device import repository
from app.modules.device.service import DeviceService
from tests.device.helpers import (
    add_start_stop_templates,
    bind_start_stop,
    make_points,
    make_profile,
    make_profile_graph,
    make_protocol_spec,
)


async def _ready_graph(db_session):
    graph = await make_profile_graph(db_session)
    await bind_start_stop(db_session, graph)
    await add_start_stop_templates(db_session, graph)
    return graph


async def test_release_requires_draft_status(db_session):
    graph = await _ready_graph(db_session)
    service = DeviceService(db_session)
    await service.release_profile(graph["profile"].id)
    with pytest.raises(DomainError) as excinfo:
        await service.release_profile(graph["profile"].id)
    assert excinfo.value.code == "profile_not_draft"


async def test_release_requires_protocol_spec(db_session):
    graph = await make_profile_graph(db_session)
    # Remove the only protocol spec.
    graph["spec"].is_deleted = True
    await db_session.flush()
    service = DeviceService(db_session)
    with pytest.raises(DomainError) as excinfo:
        await service.release_profile(graph["profile"].id)
    assert excinfo.value.code == "profile_incomplete"


async def test_release_requires_points(db_session):
    graph = await make_profile_graph(db_session)
    for point in graph["points"].values():
        point.is_deleted = True
    await db_session.flush()
    service = DeviceService(db_session)
    with pytest.raises(DomainError) as excinfo:
        await service.release_profile(graph["profile"].id)
    assert excinfo.value.code == "profile_incomplete"


async def test_release_rejects_invalid_config_schema(db_session):
    graph = await _ready_graph(db_session)
    graph["spec"].config_schema = {"type": "not-a-real-type"}
    await db_session.flush()
    service = DeviceService(db_session)
    with pytest.raises(DomainError) as excinfo:
        await service.release_profile(graph["profile"].id)
    assert excinfo.value.code == "invalid_config_schema"


async def test_release_rejects_point_without_protocol_codec(db_session):
    graph = await _ready_graph(db_session)
    graph["points"]["inlet_pressure"].data_type = "string"
    await db_session.flush()

    with pytest.raises(DomainError) as excinfo:
        await DeviceService(db_session).release_profile(graph["profile"].id)

    assert excinfo.value.code == "unsupported_point_data_type"


async def test_missing_role_blocks_release(db_session):
    graph = await make_profile_graph(db_session)
    service = DeviceService(db_session)
    pc, _ = await service.add_profile_capability(
        graph["profile"].id, graph["start_stop"].id
    )
    await service.bind_capability_point(
        pc.id, graph["points"]["run_status"].id, "status"
    )
    await add_start_stop_templates(db_session, graph)
    with pytest.raises(DomainError) as excinfo:
        await service.release_profile(graph["profile"].id)
    assert excinfo.value.code == "missing_capability_role"


async def test_release_generates_and_freezes_commands(db_session):
    graph = await _ready_graph(db_session)
    service = DeviceService(db_session)
    profile = await service.release_profile(graph["profile"].id)
    assert profile.status == "released"
    commands = await repository.list_profile_commands(db_session, profile.id)
    assert {c.command_code for c in commands} == {
        "start_stop.start",
        "start_stop.stop",
    }
    start = next(c for c in commands if c.command_code == "start_stop.start")
    assert start.operations[0]["device_point_id"] == graph["points"]["control_word"].id


async def test_make_default_swaps_atomically(db_session):
    graph = await _ready_graph(db_session)
    service = DeviceService(db_session)
    await service.release_profile(graph["profile"].id, make_default=True)

    second = await make_profile(db_session, graph["model"], "2.0")
    second_spec = await make_protocol_spec(db_session, second)
    second_points = await make_points(db_session, second_spec)
    graph2 = {**graph, "profile": second, "points": second_points}
    await bind_start_stop(db_session, graph2)
    await service.release_profile(second.id, make_default=True)

    first_reloaded = await repository.get(
        db_session, type(graph["profile"]), graph["profile"].id
    )
    second_reloaded = await repository.get(
        db_session, type(second), second.id
    )
    assert first_reloaded.is_default is False
    assert second_reloaded.is_default is True


async def test_released_profile_mutations_are_rejected(db_session):
    graph = await _ready_graph(db_session)
    service = DeviceService(db_session)
    await service.release_profile(graph["profile"].id)

    with pytest.raises(DomainError) as excinfo:
        await service.create_point(
            graph["spec"].id, code="extra", name="Extra"
        )
    assert excinfo.value.code == "profile_is_immutable"

    with pytest.raises(DomainError):
        await service.create_protocol_spec(
            graph["profile"].id, "mqtt", "mqtt"
        )

    with pytest.raises(DomainError):
        await service.add_profile_capability(
            graph["profile"].id, graph["pressure_check"].id
        )

    pc_id = (
        await repository.list_profile_capabilities(db_session, graph["profile"].id)
    )[0].id
    with pytest.raises(DomainError):
        await service.bind_capability_point(
            pc_id, graph["points"]["run_status"].id, "status"
        )

    with pytest.raises(DomainError):
        await service.regenerate_commands(graph["profile"].id)


async def test_release_failure_rolls_back_nothing_partial(db_session):
    """A failed release leaves the profile in draft with no commands."""
    graph = await make_profile_graph(db_session)
    service = DeviceService(db_session)
    # Capability bound incompletely -> generation fails.
    pc, _ = await service.add_profile_capability(
        graph["profile"].id, graph["start_stop"].id
    )
    await service.bind_capability_point(
        pc.id, graph["points"]["control_word"].id, "control"
    )
    await add_start_stop_templates(db_session, graph)
    with pytest.raises(DomainError):
        await service.release_profile(graph["profile"].id)
    commands = await repository.list_profile_commands(db_session, graph["profile"].id)
    assert commands == []
