from collections.abc import Sequence
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.modules.device import models as m


def _live(stmt: Select[Any], model: type) -> Select[Any]:
    return stmt.where(model.is_deleted.is_(False))


async def get(session: AsyncSession, model: type, entity_id: int) -> Any | None:
    stmt = _live(select(model).where(model.id == entity_id), model)
    return await session.scalar(stmt)


async def add(session: AsyncSession, entity: Any) -> Any:
    session.add(entity)
    await session.flush()
    return entity


async def get_category_by_name(session: AsyncSession, name: str) -> m.DeviceCategory | None:
    stmt = _live(
        select(m.DeviceCategory).where(func.lower(m.DeviceCategory.name) == name.lower()),
        m.DeviceCategory,
    )
    return await session.scalar(stmt)


async def get_capability_by_code(
    session: AsyncSession, code: str
) -> m.BasicCapability | None:
    stmt = _live(
        select(m.BasicCapability).where(
            func.lower(m.BasicCapability.code) == code.lower()
        ),
        m.BasicCapability,
    )
    return await session.scalar(stmt)


async def list_capabilities(session: AsyncSession) -> Sequence[m.BasicCapability]:
    result = await session.scalars(
        _live(select(m.BasicCapability).order_by(m.BasicCapability.id), m.BasicCapability)
    )
    return result.all()


async def list_device_models(session: AsyncSession) -> Sequence[m.DeviceModel]:
    result = await session.scalars(
        _live(select(m.DeviceModel).order_by(m.DeviceModel.id), m.DeviceModel)
    )
    return result.all()


async def list_category_capabilities(
    session: AsyncSession, category_id: int
) -> Sequence[m.DeviceCategoryCapability]:
    stmt = (
        select(m.DeviceCategoryCapability)
        .options(selectinload(m.DeviceCategoryCapability.basic_capability))
        .where(m.DeviceCategoryCapability.device_category_id == category_id)
        .order_by(m.DeviceCategoryCapability.sort_order, m.DeviceCategoryCapability.id)
    )
    result = await session.scalars(_live(stmt, m.DeviceCategoryCapability))
    return result.all()


async def get_profile_with_graph(
    session: AsyncSession, profile_id: int
) -> m.DeviceProfileVersion | None:
    stmt = (
        select(m.DeviceProfileVersion)
        .where(
            m.DeviceProfileVersion.id == profile_id,
            m.DeviceProfileVersion.is_deleted.is_(False),
        )
        .options(
            selectinload(m.DeviceProfileVersion.protocol_specs).selectinload(
                m.DeviceProtocolSpec.points
            ),
            selectinload(m.DeviceProfileVersion.profile_capabilities).selectinload(
                m.ProfileCapability.bindings
            ).selectinload(m.CapabilityPointBinding.device_point).selectinload(
                m.DevicePoint.protocol_spec
            ),
            selectinload(m.DeviceProfileVersion.profile_capabilities).selectinload(
                m.ProfileCapability.basic_capability
            ).selectinload(m.BasicCapability.command_templates),
            selectinload(m.DeviceProfileVersion.commands),
        )
    )
    return await session.scalar(stmt)


async def list_profile_points(
    session: AsyncSession, profile_id: int
) -> Sequence[m.DevicePoint]:
    stmt = (
        select(m.DevicePoint)
        .join(m.DeviceProtocolSpec)
        .where(
            m.DeviceProtocolSpec.profile_version_id == profile_id,
            m.DeviceProtocolSpec.is_deleted.is_(False),
            m.DevicePoint.is_deleted.is_(False),
        )
        .order_by(m.DevicePoint.protocol_spec_id, m.DevicePoint.sort_order, m.DevicePoint.id)
    )
    result = await session.scalars(stmt)
    return result.all()


async def list_profile_capabilities(
    session: AsyncSession, profile_id: int
) -> Sequence[m.ProfileCapability]:
    stmt = (
        select(m.ProfileCapability)
        .where(
            m.ProfileCapability.profile_version_id == profile_id,
            m.ProfileCapability.is_deleted.is_(False),
        )
        .options(
            selectinload(m.ProfileCapability.basic_capability),
            selectinload(m.ProfileCapability.bindings).selectinload(
                m.CapabilityPointBinding.device_point
            ),
        )
        .order_by(m.ProfileCapability.id)
    )
    result = await session.scalars(stmt)
    return result.all()


async def list_profile_commands(
    session: AsyncSession, profile_id: int
) -> Sequence[m.DeviceCommand]:
    stmt = (
        select(m.DeviceCommand)
        .where(
            m.DeviceCommand.profile_version_id == profile_id,
            m.DeviceCommand.is_deleted.is_(False),
        )
        .order_by(m.DeviceCommand.id)
    )
    result = await session.scalars(stmt)
    return result.all()


async def list_command_templates(
    session: AsyncSession, basic_capability_id: int
) -> Sequence[m.CapabilityCommandTemplate]:
    stmt = (
        select(m.CapabilityCommandTemplate)
        .where(
            m.CapabilityCommandTemplate.basic_capability_id == basic_capability_id,
            m.CapabilityCommandTemplate.is_deleted.is_(False),
        )
        .order_by(m.CapabilityCommandTemplate.id)
    )
    result = await session.scalars(stmt)
    return result.all()
