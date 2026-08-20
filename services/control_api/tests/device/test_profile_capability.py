import pytest
from sqlalchemy import func, select

from app.core.errors import DomainError
from app.modules.device.models import CapabilityPointBinding
from app.modules.device.service import DeviceService
from tests.device.helpers import make_model, make_profile, make_profile_graph


async def test_profile_capability_is_unique_per_profile_and_capability(db_session):
    graph = await make_profile_graph(db_session)
    service = DeviceService(db_session)
    await service.add_profile_capability(
        graph["profile"].id, graph["start_stop"].id
    )
    with pytest.raises(DomainError) as excinfo:
        await service.add_profile_capability(
            graph["profile"].id, graph["start_stop"].id
        )
    assert excinfo.value.code == "duplicate"


async def test_profile_capability_binds_multiple_points(db_session):
    graph = await make_profile_graph(db_session)
    service = DeviceService(db_session)
    pc, _ = await service.add_profile_capability(
        graph["profile"].id, graph["start_stop"].id
    )
    points = graph["points"]
    await service.bind_capability_point(pc.id, points["control_word"].id, "control")
    await service.bind_capability_point(pc.id, points["run_status"].id, "status")
    await service.bind_capability_point(pc.id, points["fault_code"].id, "verify")
    count = await db_session.scalar(
        select(func.count())
        .select_from(CapabilityPointBinding)
        .where(CapabilityPointBinding.profile_capability_id == pc.id)
    )
    assert count == 3


async def test_one_point_may_serve_multiple_capabilities(db_session):
    graph = await make_profile_graph(db_session)
    service = DeviceService(db_session)
    points = graph["points"]
    pc1, _ = await service.add_profile_capability(
        graph["profile"].id, graph["start_stop"].id
    )
    pc2, _ = await service.add_profile_capability(
        graph["profile"].id, graph["pressure_check"].id
    )
    await service.bind_capability_point(pc1.id, points["run_status"].id, "status")
    await service.bind_capability_point(
        pc2.id, points["run_status"].id, "measurement"
    )


async def test_same_point_and_role_cannot_be_duplicated(db_session):
    graph = await make_profile_graph(db_session)
    service = DeviceService(db_session)
    pc, _ = await service.add_profile_capability(
        graph["profile"].id, graph["start_stop"].id
    )
    point_id = graph["points"]["run_status"].id
    await service.bind_capability_point(pc.id, point_id, "status")
    with pytest.raises(DomainError) as excinfo:
        await service.bind_capability_point(pc.id, point_id, "status")
    assert excinfo.value.code == "duplicate"


async def test_same_point_with_different_role_is_allowed(db_session):
    graph = await make_profile_graph(db_session)
    service = DeviceService(db_session)
    pc, _ = await service.add_profile_capability(
        graph["profile"].id, graph["start_stop"].id
    )
    point_id = graph["points"]["run_status"].id
    await service.bind_capability_point(pc.id, point_id, "status")
    await service.bind_capability_point(pc.id, point_id, "verify")


async def test_bound_point_must_belong_to_same_profile(db_session):
    graph = await make_profile_graph(db_session)
    other_profile = await make_profile(db_session, graph["model"], "2.0")
    from tests.device.helpers import make_points, make_protocol_spec

    other_spec = await make_protocol_spec(db_session, other_profile)
    other_points = await make_points(db_session, other_spec)
    service = DeviceService(db_session)
    pc, _ = await service.add_profile_capability(
        graph["profile"].id, graph["start_stop"].id
    )
    with pytest.raises(DomainError) as excinfo:
        await service.bind_capability_point(
            pc.id, other_points["run_status"].id, "status"
        )
    assert excinfo.value.code == "cross_profile_binding"


async def test_outside_category_recommendation_flag(db_session):
    graph = await make_profile_graph(db_session)
    service = DeviceService(db_session)
    # start_stop is recommended on the Pump category.
    _, outside = await service.add_profile_capability(
        graph["profile"].id, graph["start_stop"].id
    )
    assert outside is False
    # A capability not listed on the category may still be implemented.
    extra = await service.create_capability("frequency_control", "Frequency Control")
    _, outside = await service.add_profile_capability(graph["profile"].id, extra.id)
    assert outside is True


async def test_invalid_role_rejected(db_session):
    graph = await make_profile_graph(db_session)
    service = DeviceService(db_session)
    pc, _ = await service.add_profile_capability(
        graph["profile"].id, graph["start_stop"].id
    )
    with pytest.raises(DomainError) as excinfo:
        await service.bind_capability_point(
            pc.id, graph["points"]["run_status"].id, "bogus"
        )
    assert excinfo.value.code == "invalid_role"


async def test_model_without_category_reports_outside(db_session):
    graph = await make_profile_graph(db_session)
    orphan_model = await make_model(db_session, category=None, model_code="ORPHAN-1")
    orphan_profile = await make_profile(db_session, orphan_model, "1.0")
    service = DeviceService(db_session)
    _, outside = await service.add_profile_capability(
        orphan_profile.id, graph["start_stop"].id
    )
    assert outside is True
