import asyncio

import pytest
from sqlalchemy import func, select

from app.modules.document import admin_router
from app.modules.document import router as document_router
from app.modules.document.models import ProfileExtractionRun
from tests.api.test_admin_review import _admin_client
from tests.document.test_document_models import make_document, make_run


@pytest.fixture
async def document_id(db_session):
    document = make_document()
    db_session.add(document)
    await db_session.commit()
    return document.id


async def _run_count(db_session):
    return await db_session.scalar(select(func.count(ProfileExtractionRun.id)))


async def test_repeat_submit_returns_active_run(client, db_session, document_id, monkeypatch):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    url = f"/api/v1/documents/{document_id}/extraction-runs"

    first = await client.post(url)
    second = await client.post(url)

    assert (first.status_code, second.status_code) == (202, 200)
    assert second.json()["id"] == first.json()["id"]
    assert dispatched == [first.json()["id"]]
    assert await _run_count(db_session) == 1


@pytest.mark.parametrize("status", ["completed", "failed", "canceled"])
async def test_finished_run_allows_new_run(
    client, db_session, document_id, monkeypatch, status
):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    old = make_run(document_id, status=status)
    db_session.add(old)
    await db_session.commit()

    response = await client.post(f"/api/v1/documents/{document_id}/extraction-runs")

    assert response.status_code == 202
    assert response.json()["id"] != old.id
    assert dispatched == [response.json()["id"]]


@pytest.mark.parametrize("status", ["awaiting_review", "retry_scheduled", "parsing"])
async def test_unfinished_run_is_reused(
    client, db_session, document_id, monkeypatch, status
):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    active = make_run(document_id, status=status)
    db_session.add(active)
    await db_session.commit()

    response = await client.post(f"/api/v1/documents/{document_id}/extraction-runs")

    assert response.status_code == 200
    assert response.json()["id"] == active.id
    assert dispatched == []


async def test_concurrent_submits_create_one_run(
    client, db_session, document_id, monkeypatch
):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    url = f"/api/v1/documents/{document_id}/extraction-runs"

    responses = await asyncio.gather(*(client.post(url) for _ in range(8)))

    assert sorted(r.status_code for r in responses) == [200] * 7 + [202]
    assert len({r.json()["id"] for r in responses}) == 1
    assert len(dispatched) == 1
    assert await _run_count(db_session) == 1


async def test_admin_start_button_is_idempotent(db_session, document_id, monkeypatch):
    dispatched = []
    monkeypatch.setattr(admin_router, "dispatch_extraction_task", dispatched.append)
    admin = await _admin_client(monkeypatch)
    async with admin:
        responses = await asyncio.gather(
            *(admin.post(f"/admin/documents/{document_id}/extract") for _ in range(4))
        )

    assert all(r.status_code == 303 for r in responses)
    assert len(dispatched) == 1
    assert await _run_count(db_session) == 1


async def test_retry_endpoint_unchanged(client, db_session, document_id, monkeypatch):
    dispatched = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", dispatched.append)
    failed = make_run(document_id, status="failed")
    scheduled = make_run(document_id, status="retry_scheduled")
    db_session.add_all([failed, scheduled])
    await db_session.commit()

    ok = await client.post(f"/api/v1/extraction-runs/{failed.id}/retry")
    blocked = await client.post(f"/api/v1/extraction-runs/{scheduled.id}/retry")

    assert ok.status_code == 202
    assert ok.json()["retry_of_run_id"] == failed.id
    assert dispatched == [ok.json()["id"]]
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "extraction_run_not_retryable"
