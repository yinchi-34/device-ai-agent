from app.modules.device.service import DeviceService
from app.modules.testing.config_validation import validate_connection_config
from app.modules.testing.service import TestingService
from tests.device.helpers import (
    add_start_stop_templates,
    bind_start_stop,
    make_profile_graph,
)

SCHEMA = {
    "type": "object",
    "properties": {
        "host": {"type": "string"},
        "port": {"type": "integer", "minimum": 1, "maximum": 65535},
    },
    "required": ["host", "port"],
}


def test_valid_config_produces_no_errors():
    assert validate_connection_config(SCHEMA, {"host": "a", "port": 502}) == []


def test_missing_required_and_wrong_type_reported():
    errors = validate_connection_config(SCHEMA, {"port": "not-an-int"})
    messages = {e["message"] for e in errors}
    assert any("host" in m for m in messages)
    paths = [e["path"] for e in errors]
    assert ["port"] in paths


def test_empty_schema_accepts_anything():
    assert validate_connection_config({}, {"anything": 1}) == []


async def test_validate_connection_persists_verdict(db_session):
    graph = await make_profile_graph(db_session)
    await bind_start_stop(db_session, graph)
    await add_start_stop_templates(db_session, graph)
    await DeviceService(db_session).release_profile(graph["profile"].id)
    service = TestingService(db_session)
    instance = await service.create_instance(
        graph["model"].id, graph["profile"].id, "PUMP-001", "Pump 001"
    )
    bad = await service.create_connection(
        instance.id, graph["spec"].id, "bad", config={"host": "x"}
    )
    bad = await service.validate_connection(bad.id)
    assert bad.status == "invalid"
    assert bad.config_validation_error["errors"]
    assert bad.config_validated_at is not None

    good = await service.create_connection(
        instance.id,
        graph["spec"].id,
        "good",
        config={"host": "10.0.0.5", "port": 502},
    )
    good = await service.validate_connection(good.id)
    assert good.status == "valid"
    assert good.config_validation_error == {}
