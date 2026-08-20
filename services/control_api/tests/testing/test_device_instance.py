import pytest

from app.core.errors import DomainError
from app.modules.device.service import DeviceService
from app.modules.testing.service import TestingService
from tests.device.helpers import (
    add_start_stop_templates,
    bind_start_stop,
    make_model,
    make_profile,
    make_profile_graph,
)


async def _released_graph(db_session):
    graph = await make_profile_graph(db_session)
    await bind_start_stop(db_session, graph)
    await add_start_stop_templates(db_session, graph)
    await DeviceService(db_session).release_profile(graph["profile"].id)
    return graph


async def test_instance_only_binds_released_profile(db_session):
    graph = await make_profile_graph(db_session)
    service = TestingService(db_session)
    with pytest.raises(DomainError) as excinfo:
        await service.create_instance(
            graph["model"].id, graph["profile"].id, "PUMP-001", "Pump 001"
        )
    assert excinfo.value.code == "profile_not_released"


async def test_profile_must_belong_to_selected_model(db_session):
    graph = await _released_graph(db_session)
    other_model = await make_model(db_session, model_code="OTHER-1")
    service = TestingService(db_session)
    with pytest.raises(DomainError) as excinfo:
        await service.create_instance(
            other_model.id, graph["profile"].id, "PUMP-001", "Pump 001"
        )
    assert excinfo.value.code == "profile_model_mismatch"


async def test_instance_code_is_unique(db_session):
    graph = await _released_graph(db_session)
    service = TestingService(db_session)
    await service.create_instance(
        graph["model"].id, graph["profile"].id, "PUMP-001", "Pump 001"
    )
    with pytest.raises(DomainError) as excinfo:
        await service.create_instance(
            graph["model"].id, graph["profile"].id, "PUMP-001", "Duplicate"
        )
    assert excinfo.value.code == "duplicate"


async def test_connection_protocol_must_belong_to_instance_profile(db_session):
    graph = await _released_graph(db_session)
    service = TestingService(db_session)
    instance = await service.create_instance(
        graph["model"].id, graph["profile"].id, "PUMP-001", "Pump 001"
    )

    other_profile = await make_profile(db_session, graph["model"], "2.0")
    from tests.device.helpers import make_protocol_spec

    other_spec = await make_protocol_spec(db_session, other_profile)
    with pytest.raises(DomainError) as excinfo:
        await service.create_connection(instance.id, other_spec.id, "other")
    assert excinfo.value.code == "protocol_profile_mismatch"


async def test_connection_created_with_matching_spec(db_session):
    graph = await _released_graph(db_session)
    service = TestingService(db_session)
    instance = await service.create_instance(
        graph["model"].id, graph["profile"].id, "PUMP-001", "Pump 001"
    )
    connection = await service.create_connection(
        instance.id,
        graph["spec"].id,
        "fieldbus",
        config={"host": "10.0.0.5", "port": 502},
    )
    assert connection.status == "draft"


async def test_duplicate_connection_name_rejected(db_session):
    graph = await _released_graph(db_session)
    service = TestingService(db_session)
    instance = await service.create_instance(
        graph["model"].id, graph["profile"].id, "PUMP-001", "Pump 001"
    )
    await service.create_connection(instance.id, graph["spec"].id, "fieldbus")
    with pytest.raises(DomainError) as excinfo:
        await service.create_connection(instance.id, graph["spec"].id, "fieldbus")
    assert excinfo.value.code == "duplicate"
