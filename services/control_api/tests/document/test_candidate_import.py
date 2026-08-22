import pytest
from sqlalchemy import func, select

from app.core.errors import DomainError
from app.modules.device.models import DeviceProfileVersion
from app.modules.document.service import DocumentService
from tests.document.test_candidate_review import make_candidate


async def count_profiles(db_session):
    return await db_session.scalar(select(func.count(DeviceProfileVersion.id)))


async def test_import_is_transactional_and_idempotent(db_session):
    candidate = await make_candidate(db_session, status="approved")
    service = DocumentService(db_session)

    first = await service.import_candidate(candidate.id)
    second = await service.import_candidate(candidate.id)

    assert second.id == first.id
    assert first.status == "draft"
    assert await count_profiles(db_session) == 1


async def test_unapproved_candidate_creates_no_device_rows(db_session):
    candidate = await make_candidate(db_session, status="valid")

    with pytest.raises(DomainError) as excinfo:
        await DocumentService(db_session).import_candidate(candidate.id)

    assert excinfo.value.code == "candidate_invalid"
    assert await count_profiles(db_session) == 0


async def test_failed_graph_creation_rolls_back_all_device_rows(db_session):
    candidate = await make_candidate(db_session, status="approved")
    payload = candidate.reviewed_payload.copy()
    payload["protocols"] = [
        payload["protocols"][0],
        {**payload["protocols"][0], "candidate_ref": "protocol:duplicate"},
    ]
    candidate.reviewed_payload = payload
    await db_session.flush()

    with pytest.raises(DomainError):
        await DocumentService(db_session).import_candidate(candidate.id)

    assert await count_profiles(db_session) == 0
