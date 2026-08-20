import pytest
from sqlalchemy.exc import IntegrityError

from app.core.errors import DomainError
from app.modules.device.models import (
    BasicCapability,
    DeviceCategory,
    DeviceCategoryCapability,
)
from app.modules.device.service import DeviceService


async def test_live_category_name_is_unique(db_session):
    service = DeviceService(db_session)
    await service.create_category("Pump")
    with pytest.raises(DomainError) as excinfo:
        await service.create_category("pump")
    assert excinfo.value.code == "duplicate"


async def test_live_model_vendor_code_is_unique(db_session):
    service = DeviceService(db_session)
    await service.create_model("ACME", "X200", "Pump X200")
    with pytest.raises(DomainError) as excinfo:
        await service.create_model("ACME", "X200", "Another X200")
    assert excinfo.value.code == "duplicate"


async def test_capability_code_is_globally_unique(db_session):
    service = DeviceService(db_session)
    await service.create_capability("start_stop", "Start / Stop")
    with pytest.raises(DomainError) as excinfo:
        await service.create_capability("START_STOP", "Duplicate")
    assert excinfo.value.code == "duplicate"


async def test_same_category_cannot_bind_same_capability_twice(db_session):
    service = DeviceService(db_session)
    category = await service.create_category("Pump")
    capability = await service.create_capability("start_stop", "Start / Stop")
    await service.associate_category_capability(category.id, capability.id)
    with pytest.raises(DomainError) as excinfo:
        await service.associate_category_capability(category.id, capability.id)
    assert excinfo.value.code == "duplicate"


async def test_one_capability_binds_multiple_categories(db_session):
    service = DeviceService(db_session)
    capability = await service.create_capability("start_stop", "Start / Stop")
    pump = await service.create_category("Pump")
    fan = await service.create_category("Fan")
    await service.associate_category_capability(pump.id, capability.id)
    await service.associate_category_capability(fan.id, capability.id)
    links = await db_session.scalars(
        DeviceCategoryCapability.__table__.select().where(
            DeviceCategoryCapability.basic_capability_id == capability.id
        )
    )
    assert len(links.all()) == 2


async def test_one_category_binds_multiple_capabilities(db_session):
    service = DeviceService(db_session)
    category = await service.create_category("Pump")
    first = await service.create_capability("start_stop", "Start / Stop")
    second = await service.create_capability("fault_check", "Fault Check")
    await service.associate_category_capability(category.id, first.id)
    await service.associate_category_capability(category.id, second.id)


async def test_soft_deleted_category_name_can_be_reused(db_session):
    service = DeviceService(db_session)
    category = await service.create_category("Pump")
    category.is_deleted = True
    await db_session.flush()
    await service.create_category("Pump")


async def test_database_level_partial_unique_index(db_session):
    db_session.add(DeviceCategory(name="Pump"))
    await db_session.flush()
    db_session.add(DeviceCategory(name="pump"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_basic_capability_has_no_protocol_fields(db_session):
    capability = BasicCapability(code="x", name="X")
    assert not hasattr(capability, "address_text")
    assert not hasattr(capability, "device_point_id")
    assert not hasattr(capability, "protocol")


async def test_model_labels_default_empty_list(db_session):
    service = DeviceService(db_session)
    model = await service.create_model("ACME", "X200", "Pump X200")
    assert model.labels == []
    assert model.status == "draft"
