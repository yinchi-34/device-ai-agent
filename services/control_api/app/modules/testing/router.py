from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.modules.testing import schemas
from app.modules.testing.service import TestingService

router = APIRouter(prefix="/api/v1", tags=["testing"])


@router.post("/devices", status_code=201, response_model=schemas.DeviceInstanceOut)
async def create_device(
    payload: schemas.DeviceInstanceCreate,
    session: AsyncSession = Depends(get_db_session),
) -> object:
    instance = await TestingService(session).create_instance(**payload.model_dump())
    await session.commit()
    return instance


@router.get(
    "/devices/{device_id}/context", response_model=schemas.DeviceContextOut
)
async def get_device_context(
    device_id: int, session: AsyncSession = Depends(get_db_session)
) -> object:
    instance = await TestingService(session).get_device_context(device_id)
    profile = instance.profile_version
    specs = [s for s in profile.protocol_specs if not s.is_deleted]
    return schemas.DeviceContextOut(
        device=schemas.DeviceInstanceOut.model_validate(instance),
        profile={
            "id": profile.id,
            "device_model_id": profile.device_model_id,
            "version": profile.version,
            "profile_name": profile.profile_name,
            "status": profile.status,
        },
        protocol_specs=[
            {
                "id": s.id,
                "code": s.code,
                "protocol": s.protocol,
                "display_name": s.display_name,
                "config_schema": s.config_schema,
            }
            for s in specs
        ],
        connections=[
            schemas.DeviceConnectionOut.model_validate(c)
            for c in instance.connections
            if not c.is_deleted
        ],
    )


@router.post(
    "/devices/{device_id}/connections",
    status_code=201,
    response_model=schemas.DeviceConnectionOut,
)
async def create_connection(
    device_id: int,
    payload: schemas.DeviceConnectionCreate,
    session: AsyncSession = Depends(get_db_session),
) -> object:
    connection = await TestingService(session).create_connection(
        device_id=device_id, **payload.model_dump()
    )
    await session.commit()
    return connection


@router.post(
    "/connections/{connection_id}/validate-config",
    response_model=schemas.ConnectionValidationOut,
)
async def validate_connection_config(
    connection_id: int, session: AsyncSession = Depends(get_db_session)
) -> object:
    connection = await TestingService(session).validate_connection(connection_id)
    await session.commit()
    return schemas.ConnectionValidationOut(
        connection_id=connection.id,
        status=connection.status,
        errors=connection.config_validation_error.get("errors", []),
    )


@router.get(
    "/devices/{device_id}/test-options", response_model=schemas.TestOptionsOut
)
async def get_test_options(
    device_id: int, session: AsyncSession = Depends(get_db_session)
) -> object:
    return await TestingService(session).get_test_options(device_id)
