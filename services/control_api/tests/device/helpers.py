"""Shared builders for device domain tests."""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.device.models import (
    BasicCapability,
    DeviceCategory,
    DeviceModel,
    DevicePoint,
    DeviceProfileVersion,
    DeviceProtocolSpec,
)
from app.modules.device.service import DeviceService

POINT_DEFS: list[dict[str, Any]] = [
    {"code": "run_status", "address_text": "40001", "data_type": "int16"},
    {"code": "fault_code", "address_text": "40002", "data_type": "int16"},
    {"code": "inlet_pressure", "address_text": "40003", "data_type": "float32"},
    {"code": "outlet_pressure", "address_text": "40004", "data_type": "float32"},
    {
        "code": "control_word",
        "address_text": "40010",
        "data_type": "int16",
        "access_mode": "RW",
    },
]

START_TEMPLATE = {
    "operations": [
        {"op": "write", "role": "control", "select": "one", "value": 1},
        {"op": "read", "role": "status", "select": "one"},
    ]
}

STOP_TEMPLATE = {
    "operations": [
        {"op": "write", "role": "control", "select": "one", "value": 0},
        {"op": "read", "role": "status", "select": "one"},
    ]
}


async def make_category(session: AsyncSession, name: str = "Pump") -> DeviceCategory:
    category = DeviceCategory(name=name)
    session.add(category)
    await session.flush()
    return category


async def make_capability(
    session: AsyncSession, code: str = "start_stop", name: str = "Start / Stop"
) -> BasicCapability:
    capability = BasicCapability(code=code, name=name)
    session.add(capability)
    await session.flush()
    return capability


async def make_model(
    session: AsyncSession,
    category: DeviceCategory | None = None,
    vendor_name: str = "ACME",
    model_code: str = "PUMP-X200",
) -> DeviceModel:
    model = DeviceModel(
        vendor_name=vendor_name,
        model_code=model_code,
        model_name="Pump X200",
        device_category_id=category.id if category else None,
    )
    session.add(model)
    await session.flush()
    return model


async def make_profile(
    session: AsyncSession,
    model: DeviceModel,
    version: str = "1.0",
) -> DeviceProfileVersion:
    profile = DeviceProfileVersion(
        device_model_id=model.id, version=version, profile_name=f"v{version}"
    )
    session.add(profile)
    await session.flush()
    return profile


async def make_protocol_spec(
    session: AsyncSession,
    profile: DeviceProfileVersion,
    code: str = "modbus",
    config_schema: dict[str, Any] | None = None,
) -> DeviceProtocolSpec:
    spec = DeviceProtocolSpec(
        profile_version_id=profile.id,
        code=code,
        protocol="modbus_tcp",
        config_schema=config_schema
        or {
            "type": "object",
            "properties": {
                "host": {"type": "string"},
                "port": {"type": "integer"},
            },
            "required": ["host", "port"],
        },
    )
    session.add(spec)
    await session.flush()
    return spec


async def make_points(
    session: AsyncSession, spec: DeviceProtocolSpec
) -> dict[str, DevicePoint]:
    points: dict[str, DevicePoint] = {}
    for fields in POINT_DEFS:
        point = DevicePoint(
            protocol_spec_id=spec.id,
            name=fields["code"].replace("_", " ").title(),
            **fields,
        )
        session.add(point)
        points[fields["code"]] = point
    await session.flush()
    return points


async def make_profile_graph(session: AsyncSession) -> dict[str, Any]:
    """Category + capabilities + model + draft profile + spec + points."""
    category = await make_category(session)
    start_stop = await make_capability(session)
    pressure = await make_capability(session, "pressure_check", "Pressure Check")
    service = DeviceService(session)
    await service.associate_category_capability(category.id, start_stop.id)
    await service.associate_category_capability(category.id, pressure.id)
    model = await make_model(session, category)
    profile = await make_profile(session, model)
    spec = await make_protocol_spec(session, profile)
    points = await make_points(session, spec)
    return {
        "category": category,
        "start_stop": start_stop,
        "pressure_check": pressure,
        "model": model,
        "profile": profile,
        "spec": spec,
        "points": points,
    }


async def bind_start_stop(
    session: AsyncSession, graph: dict[str, Any]
) -> Any:
    service = DeviceService(session)
    pc, _ = await service.add_profile_capability(
        graph["profile"].id, graph["start_stop"].id
    )
    points = graph["points"]
    await service.bind_capability_point(pc.id, points["control_word"].id, "control")
    await service.bind_capability_point(pc.id, points["run_status"].id, "status")
    await service.bind_capability_point(pc.id, points["fault_code"].id, "verify")
    return pc


async def add_start_stop_templates(
    session: AsyncSession, graph: dict[str, Any]
) -> None:
    service = DeviceService(session)
    capability_id = graph["start_stop"].id
    await service.create_command_template(
        capability_id, "start", "Start", START_TEMPLATE, access_mode="RW"
    )
    await service.create_command_template(
        capability_id, "stop", "Stop", STOP_TEMPLATE, access_mode="RW"
    )
