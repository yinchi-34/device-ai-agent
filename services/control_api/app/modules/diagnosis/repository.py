from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.modules.device.models import DevicePoint, DeviceProtocolSpec
from app.modules.diagnosis.models import DiagnosisRun
from app.modules.testing.models import DeviceConnection


async def get_run(session: AsyncSession, run_id: int) -> DiagnosisRun | None:
    statement = (
        select(DiagnosisRun)
        .where(DiagnosisRun.id == run_id)
        .options(selectinload(DiagnosisRun.steps))
        .execution_options(populate_existing=True)
    )
    return await session.scalar(statement)


async def list_profile_points(
    session: AsyncSession,
    profile_version_id: int,
    *,
    point_code: str | None = None,
) -> Sequence[DevicePoint]:
    statement = (
        select(DevicePoint)
        .join(DeviceProtocolSpec, DeviceProtocolSpec.id == DevicePoint.protocol_spec_id)
        .where(
            DeviceProtocolSpec.profile_version_id == profile_version_id,
            DeviceProtocolSpec.is_deleted.is_(False),
            DevicePoint.is_deleted.is_(False),
        )
        .order_by(DevicePoint.sort_order, DevicePoint.id)
    )
    if point_code is not None:
        statement = statement.where(DevicePoint.code == point_code)
    return (await session.scalars(statement)).all()


async def get_device_connection(
    session: AsyncSession, device_id: int, connection_id: int
) -> DeviceConnection | None:
    statement = select(DeviceConnection).where(
        DeviceConnection.id == connection_id,
        DeviceConnection.device_instance_id == device_id,
        DeviceConnection.is_deleted.is_(False),
    )
    return await session.scalar(statement)
