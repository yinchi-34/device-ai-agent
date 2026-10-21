import asyncio

import pytest
from sqlalchemy import func, select

from app.modules.document import admin_router
from app.modules.document import router as document_router
from app.modules.document.models import ProfileExtractionRun, SourceDocument
from app.modules.document.service import create_extraction_run
from tests.api.test_admin_review import _admin_client


@pytest.fixture
def dispatched(monkeypatch):
    calls = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", calls.append)
    monkeypatch.setattr(admin_router, "dispatch_extraction_task", calls.append)
    return calls


async def make_document(db_session) -> SourceDocument:
    document = SourceDocument(
        original_file_name="manual.pdf",
        media_type="application/pdf",
        file_size_bytes=1,
        file_hash_sha256="0" * 64,
        storage_uri="original/manual.pdf",
        validation_status="validated",
    )
    db_session.add(document)
    await db_session.commit()
    return document


async def make_run(db_session, document_id: int, status: str):
    run = await create_extraction_run(
        db_session,
        document_id,
        extraction_provider="fake",
        extraction_model="extract-v1",
        embedding_provider="fake",
        embedding_model="embed-v1",
    )
    run.status = status
    await db_session.commit()
    return run


async def run_count(db_session) -> int:
    db_session.expire_all()
    return await db_session.scalar(select(func.count(ProfileExtractionRun.id)))


async def test_second_submit_returns_active_run_without_dispatch(
    client, db_session, dispatched
):
    document = await make_document(db_session)
    url = f"/api/v1/documents/{document.id}/extraction-runs"

    first = await client.post(url)
    second = await client.post(url)

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert dispatched == [first.json()["id"]]
    assert await run_count(db_session) == 1


@pytest.mark.parametrize(
    "status",
    ["queued", "parsing", "extracting", "awaiting_review", "importing", "retry_scheduled"],
)
async def test_unfinished_run_is_reused(client, db_session, dispatched, status):
    document = await make_document(db_session)
    run = await make_run(db_session, document.id, status)

    response = await client.post(f"/api/v1/documents/{document.id}/extraction-runs")

    assert response.status_code == 200
    assert response.json()["id"] == run.id
    assert dispatched == []


@pytest.mark.parametrize("status", ["completed", "failed", "canceled"])
async def test_finished_run_does_not_block_new_run(
    client, db_session, dispatched, status
):
    document = await make_document(db_session)
    old = await make_run(db_session, document.id, status)

    response = await client.post(f"/api/v1/documents/{document.id}/extraction-runs")

    assert response.status_code == 202
    assert response.json()["id"] != old.id
    assert dispatched == [response.json()["id"]]


async def test_active_run_of_other_document_does_not_block(
    client, db_session, dispatched
):
    first = await make_document(db_session)
    second = await make_document(db_session)
    await make_run(db_session, first.id, "queued")

    response = await client.post(f"/api/v1/documents/{second.id}/extraction-runs")

    assert response.status_code == 202


async def test_concurrent_submits_create_one_run(client, db_session, dispatched):
    document = await make_document(db_session)
    url = f"/api/v1/documents/{document.id}/extraction-runs"

    responses = await asyncio.gather(*(client.post(url) for _ in range(8)))

    assert sorted(r.status_code for r in responses) == [200] * 7 + [202]
    assert len({r.json()["id"] for r in responses}) == 1
    assert len(dispatched) == 1
    assert await run_count(db_session) == 1


async def test_retry_failed_run_still_creates_new_run(client, db_session, dispatched):
    document = await make_document(db_session)
    failed = await make_run(db_session, document.id, "failed")

    response = await client.post(f"/api/v1/extraction-runs/{failed.id}/retry")

    assert response.status_code == 202
    assert response.json()["id"] != failed.id
    assert dispatched == [response.json()["id"]]


async def test_retry_scheduled_run_cannot_be_retried_manually(
    client, db_session, dispatched
):
    document = await make_document(db_session)
    run = await make_run(db_session, document.id, "retry_scheduled")

    response = await client.post(f"/api/v1/extraction-runs/{run.id}/retry")

    assert response.status_code == 409
    assert dispatched == []
    assert await run_count(db_session) == 1


async def test_admin_start_button_is_idempotent(db_session, monkeypatch, dispatched):
    document = await make_document(db_session)

    async with await _admin_client(monkeypatch) as client:
        responses = await asyncio.gather(
            *(client.post(f"/admin/documents/{document.id}/extract") for _ in range(5))
        )

    assert {r.status_code for r in responses} == {303}
    assert len(dispatched) == 1
    assert await run_count(db_session) == 1
