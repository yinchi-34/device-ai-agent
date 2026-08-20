import pytest
from sqlalchemy.exc import IntegrityError

from app.core.errors import DomainError
from app.modules.device.models import DevicePoint, DeviceProfileVersion
from app.modules.device.service import DeviceService
from tests.device.helpers import (
    make_model,
    make_points,
    make_profile,
    make_protocol_spec,
)


async def test_same_model_version_cannot_coexist_as_two_live_profiles(db_session):
    service = DeviceService(db_session)
    model = await make_model(db_session)
    await service.create_profile(model.id, "1.0")
    with pytest.raises(DomainError) as excinfo:
        await service.create_profile(model.id, "1.0")
    assert excinfo.value.code == "duplicate"


async def test_only_one_live_default_profile_per_model(db_session):
    model = await make_model(db_session)
    first = await make_profile(db_session, model, "1.0")
    first.is_default = True
    await db_session.flush()
    second = await make_profile(db_session, model, "2.0")
    second.is_default = True
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_same_spec_cannot_have_duplicate_live_point_code(db_session):
    service = DeviceService(db_session)
    model = await make_model(db_session)
    profile = await make_profile(db_session, model)
    spec = await make_protocol_spec(db_session, profile)
    await service.create_point(spec.id, code="run_status", name="Run Status")
    with pytest.raises(DomainError) as excinfo:
        await service.create_point(spec.id, code="run_status", name="Duplicate")
    assert excinfo.value.code == "duplicate"


async def test_two_points_may_share_address_text(db_session):
    model = await make_model(db_session)
    profile = await make_profile(db_session, model)
    spec = await make_protocol_spec(db_session, profile)
    points = await make_points(db_session, spec)
    db_session.add(
        DevicePoint(
            protocol_spec_id=spec.id,
            code="run_status_bit0",
            name="Run Status Bit 0",
            data_type="bool",
            address_text=points["run_status"].address_text,
        )
    )
    await db_session.flush()


async def test_profile_defaults_to_draft(db_session):
    model = await make_model(db_session)
    profile = await make_profile(db_session, model)
    assert profile.status == "draft"
    assert profile.is_default is False


async def test_profile_exposes_composite_unique_for_instance_fk(db_session):
    constraint_names = {
        c.name for c in DeviceProfileVersion.__table__.constraints
    }
    assert "uq_profile_id_model" in constraint_names
