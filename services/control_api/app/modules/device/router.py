from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.modules.device import repository, schemas
from app.modules.device.service import DeviceService

router = APIRouter(prefix="/api/v1", tags=["device"])


@router.post(
    "/device-categories",
    status_code=201,
    response_model=schemas.DeviceCategoryOut,
)
async def create_category(
    payload: schemas.DeviceCategoryCreate,
    session: AsyncSession = Depends(get_db_session),
) -> object:
    category = await DeviceService(session).create_category(
        name=payload.name, description=payload.description
    )
    await session.commit()
    return category


@router.post(
    "/device-models", status_code=201, response_model=schemas.DeviceModelOut
)
async def create_model(
    payload: schemas.DeviceModelCreate,
    session: AsyncSession = Depends(get_db_session),
) -> object:
    model = await DeviceService(session).create_model(**payload.model_dump())
    await session.commit()
    return model


@router.get("/device-models", response_model=list[schemas.DeviceModelOut])
async def list_models(session: AsyncSession = Depends(get_db_session)) -> object:
    return await repository.list_device_models(session)


@router.post(
    "/capabilities", status_code=201, response_model=schemas.BasicCapabilityOut
)
async def create_capability(
    payload: schemas.BasicCapabilityCreate,
    session: AsyncSession = Depends(get_db_session),
) -> object:
    capability = await DeviceService(session).create_capability(**payload.model_dump())
    await session.commit()
    return capability


@router.get("/capabilities", response_model=list[schemas.BasicCapabilityOut])
async def list_capabilities(
    session: AsyncSession = Depends(get_db_session),
) -> object:
    return await repository.list_capabilities(session)


@router.post(
    "/device-categories/{category_id}/capabilities/{capability_id}",
    status_code=201,
    response_model=schemas.CategoryCapabilityLinkOut,
)
async def link_category_capability(
    category_id: int,
    capability_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> object:
    link = await DeviceService(session).associate_category_capability(
        category_id, capability_id
    )
    await session.commit()
    return link


@router.get(
    "/device-categories/{category_id}/capabilities",
    response_model=list[schemas.BasicCapabilityOut],
)
async def list_category_capabilities(
    category_id: int, session: AsyncSession = Depends(get_db_session)
) -> object:
    links = await repository.list_category_capabilities(session, category_id)
    return [link.basic_capability for link in links]


@router.post(
    "/device-models/{model_id}/profiles",
    status_code=201,
    response_model=schemas.ProfileOut,
)
async def create_profile(
    model_id: int,
    payload: schemas.ProfileCreate,
    session: AsyncSession = Depends(get_db_session),
) -> object:
    profile = await DeviceService(session).create_profile(
        device_model_id=model_id, **payload.model_dump()
    )
    await session.commit()
    return profile


@router.get("/profiles/{profile_id}", response_model=schemas.ProfileOut)
async def get_profile(
    profile_id: int, session: AsyncSession = Depends(get_db_session)
) -> object:
    return await DeviceService(session).get_profile(profile_id)


@router.post(
    "/profiles/{profile_id}/protocols",
    status_code=201,
    response_model=schemas.ProtocolSpecOut,
)
async def create_protocol(
    profile_id: int,
    payload: schemas.ProtocolSpecCreate,
    session: AsyncSession = Depends(get_db_session),
) -> object:
    spec = await DeviceService(session).create_protocol_spec(
        profile_id, **payload.model_dump()
    )
    await session.commit()
    return spec


@router.post(
    "/protocols/{protocol_id}/points",
    status_code=201,
    response_model=schemas.PointOut,
)
async def create_point(
    protocol_id: int,
    payload: schemas.PointCreate,
    session: AsyncSession = Depends(get_db_session),
) -> object:
    point = await DeviceService(session).create_point(
        protocol_id, **payload.model_dump()
    )
    await session.commit()
    return point


@router.get("/profiles/{profile_id}/points", response_model=list[schemas.PointOut])
async def list_profile_points(
    profile_id: int, session: AsyncSession = Depends(get_db_session)
) -> object:
    await DeviceService(session).get_profile(profile_id)
    return await repository.list_profile_points(session, profile_id)


@router.post(
    "/profiles/{profile_id}/capabilities/{capability_id}",
    status_code=201,
    response_model=schemas.ProfileCapabilityCreated,
)
async def add_profile_capability(
    profile_id: int,
    capability_id: int,
    payload: schemas.ProfileCapabilityCreate | None = None,
    session: AsyncSession = Depends(get_db_session),
) -> object:
    payload = payload or schemas.ProfileCapabilityCreate()
    profile_capability, outside = await DeviceService(session).add_profile_capability(
        profile_id=profile_id,
        basic_capability_id=capability_id,
        name_override=payload.name_override,
        metadata=payload.metadata,
    )
    await session.commit()
    return schemas.ProfileCapabilityCreated(
        profile_capability=schemas.ProfileCapabilityOut.model_validate(
            profile_capability
        ),
        outside_category_recommendation=outside,
    )


@router.post(
    "/profile-capabilities/{profile_capability_id}/point-bindings",
    status_code=201,
    response_model=schemas.BindingOut,
)
async def bind_point(
    profile_capability_id: int,
    payload: schemas.BindingCreate,
    session: AsyncSession = Depends(get_db_session),
) -> object:
    binding = await DeviceService(session).bind_capability_point(
        profile_capability_id=profile_capability_id, **payload.model_dump()
    )
    await session.commit()
    return binding


@router.get("/profiles/{profile_id}/capabilities")
async def list_profile_capabilities(
    profile_id: int, session: AsyncSession = Depends(get_db_session)
) -> object:
    await DeviceService(session).get_profile(profile_id)
    capabilities = await repository.list_profile_capabilities(session, profile_id)
    return [
        {
            "id": pc.id,
            "profile_version_id": pc.profile_version_id,
            "capability_code": pc.basic_capability.code,
            "capability_name": pc.name_override or pc.basic_capability.name,
            "bindings": [
                {
                    "id": b.id,
                    "device_point_id": b.device_point_id,
                    "point_code": b.device_point.code,
                    "role": b.role,
                    "is_required": b.is_required,
                    "sort_order": b.sort_order,
                }
                for b in sorted(
                    (b for b in pc.bindings if not b.is_deleted),
                    key=lambda b: (b.sort_order, b.id),
                )
            ],
        }
        for pc in capabilities
    ]


@router.post(
    "/capabilities/{capability_id}/command-templates",
    status_code=201,
    response_model=schemas.CommandTemplateOut,
)
async def create_command_template(
    capability_id: int,
    payload: schemas.CommandTemplateCreate,
    session: AsyncSession = Depends(get_db_session),
) -> object:
    template = await DeviceService(session).create_command_template(
        basic_capability_id=capability_id, **payload.model_dump()
    )
    await session.commit()
    return template


@router.get(
    "/profiles/{profile_id}/commands", response_model=list[schemas.CommandOut]
)
async def list_profile_commands(
    profile_id: int, session: AsyncSession = Depends(get_db_session)
) -> object:
    await DeviceService(session).get_profile(profile_id)
    return await repository.list_profile_commands(session, profile_id)


@router.post(
    "/profiles/{profile_id}/release", response_model=schemas.ReleaseResponse
)
async def release_profile(
    profile_id: int,
    payload: schemas.ReleaseRequest | None = None,
    session: AsyncSession = Depends(get_db_session),
) -> object:
    payload = payload or schemas.ReleaseRequest()
    service = DeviceService(session)
    profile = await service.release_profile(profile_id, make_default=payload.make_default)
    commands = await repository.list_profile_commands(session, profile_id)
    await session.commit()
    return schemas.ReleaseResponse(
        profile=schemas.ProfileOut.model_validate(profile),
        generated_commands=[c.command_code for c in commands],
    )
