import asyncio

import pytest
from sqlalchemy import func, select

from app.modules.document import admin_router
from app.modules.document import router as document_router
from app.modules.document.models import ProfileExtractionRun
from tests.api.test_admin_review import _admin_client
from tests.document.test_document_models import make_document, make_run

URL = "/api/v1/documents/{}/extraction-runs"


@pytest.fixture
def dispatched(monkeypatch):
    calls = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", calls.append)
    monkeypatch.setattr(admin_router, "dispatch_extraction_task", calls.append)
    return calls


async def _document(db_session):
    document = make_document()
    db_session.add(document)
    await db_session.commit()
    return document.id


async def _run_count(db_session):
    return await db_session.scalar(select(func.count(ProfileExtractionRun.id)))


async def test_second_submit_returns_existing_run(client, db_session, dispatched):
    document_id = await _document(db_session)

    first = await client.post(URL.format(document_id))
    second = await client.post(URL.format(document_id))

    assert (first.status_code, second.status_code) == (202, 200)
    assert second.json()["id"] == first.json()["id"]
    assert await _run_count(db_session) == 1
    assert dispatched == [first.json()["id"]]


@pytest.mark.parametrize("status", ["completed", "failed", "canceled"])
async def test_finished_run_allows_new_run(client, db_session, dispatched, status):
    document_id = await _document(db_session)
    db_session.add(make_run(document_id, status=status))
    await db_session.commit()

    response = await client.post(URL.format(document_id))

    assert response.status_code == 202
    assert await _run_count(db_session) == 2
    assert dispatched == [response.json()["id"]]


@pytest.mark.parametrize("status", ["awaiting_review", "retry_scheduled", "parsing"])
async def test_unfinished_run_is_reused(client, db_session, dispatched, status):
    document_id = await _document(db_session)
    run = make_run(document_id, status=status)
    db_session.add(run)
    await db_session.commit()

    response = await client.post(URL.format(document_id))

    assert response.status_code == 200
    assert response.json()["id"] == run.id
    assert dispatched == []


async def test_concurrent_submits_create_one_run(client, db_session, dispatched):
    document_id = await _document(db_session)

    responses = await asyncio.gather(
        *(client.post(URL.format(document_id)) for _ in range(8))
    )

    assert sorted(r.status_code for r in responses) == [200] * 7 + [202]
    assert len({r.json()["id"] for r in responses}) == 1
    assert await _run_count(db_session) == 1
    assert len(dispatched) == 1


async def test_admin_start_parsing_is_idempotent(db_session, monkeypatch, dispatched):
    document_id = await _document(db_session)

    async with await _admin_client(monkeypatch) as admin:
        for _ in range(2):
            response = await admin.post(f"/admin/documents/{document_id}/extract")
            assert response.status_code == 303

    assert await _run_count(db_session) == 1
    assert len(dispatched) == 1


async def test_retry_scheduled_run_cannot_be_manually_retried(
    client, db_session, dispatched
):
    document_id = await _document(db_session)
    run = make_run(document_id, status="retry_scheduled")
    db_session.add(run)
    await db_session.commit()

    response = await client.post(f"/api/v1/extraction-runs/{run.id}/retry")

    assert response.status_code == 409
    assert dispatched == []


async def test_failed_run_retry_still_works(client, db_session, dispatched):
    document_id = await _document(db_session)
    run = make_run(document_id, status="failed")
    db_session.add(run)
    await db_session.commit()

    response = await client.post(f"/api/v1/extraction-runs/{run.id}/retry")

    assert response.status_code == 202
    assert dispatched == [response.json()["id"]]
