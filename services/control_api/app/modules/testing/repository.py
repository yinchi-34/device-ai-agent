from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.modules.device.models import DeviceProfileVersion
from app.modules.testing.models import DeviceConnection, DeviceInstance


async def get_instance(
    session: AsyncSession, device_id: int
) -> DeviceInstance | None:
    stmt = (
        select(DeviceInstance)
        .where(
            DeviceInstance.id == device_id,
            DeviceInstance.is_deleted.is_(False),
        )
        .options(
            selectinload(DeviceInstance.profile_version).selectinload(
                DeviceProfileVersion.protocol_specs
            ),
            selectinload(DeviceInstance.connections),
        )
    )
    return await session.scalar(stmt)


async def get_connection(
    session: AsyncSession, connection_id: int
) -> DeviceConnection | None:
    stmt = (
        select(DeviceConnection)
        .where(
            DeviceConnection.id == connection_id,
            DeviceConnection.is_deleted.is_(False),
        )
        .options(selectinload(DeviceConnection.protocol_spec))
    )
    return await session.scalar(stmt)


async def list_instance_connections(
    session: AsyncSession, device_id: int
) -> Sequence[DeviceConnection]:
    stmt = (
        select(DeviceConnection)
        .where(
            DeviceConnection.device_instance_id == device_id,
            DeviceConnection.is_deleted.is_(False),
        )
        .order_by(DeviceConnection.id)
    )
    result = await session.scalars(stmt)
    return result.all()
