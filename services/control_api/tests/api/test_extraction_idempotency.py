import asyncio

import pytest
from sqlalchemy import func, select

from app.modules.document import admin_router
from app.modules.document import router as document_router
from app.modules.document.models import ProfileExtractionRun, SourceDocument
from tests.api.test_admin_review import _admin_client


async def make_document(db_session) -> int:
    document = SourceDocument(
        original_file_name="manual.docx",
        media_type="application/pdf",
        file_size_bytes=10,
        file_hash_sha256="a" * 64,
        storage_uri="original/manual.docx",
        validation_status="validated",
    )
    db_session.add(document)
    await db_session.commit()
    return document.id


async def run_count(db_session) -> int:
    return await db_session.scalar(select(func.count(ProfileExtractionRun.id)))


@pytest.fixture
def dispatched(monkeypatch):
    calls = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", calls.append)
    monkeypatch.setattr(admin_router, "dispatch_extraction_task", calls.append)
    return calls


async def test_second_submit_returns_existing_run(client, db_session, dispatched):
    document_id = await make_document(db_session)
    url = f"/api/v1/documents/{document_id}/extraction-runs"

    first = await client.post(url)
    second = await client.post(url)

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert dispatched == [first.json()["id"]]
    assert await run_count(db_session) == 1


@pytest.mark.parametrize(
    "status", ["queued", "retry_scheduled", "extracting", "awaiting_review"]
)
async def test_unfinished_run_is_reused(client, db_session, dispatched, status):
    document_id = await make_document(db_session)
    first = await client.post(f"/api/v1/documents/{document_id}/extraction-runs")
    run = await db_session.get(ProfileExtractionRun, first.json()["id"])
    run.status = status
    await db_session.commit()
    dispatched.clear()

    again = await client.post(f"/api/v1/documents/{document_id}/extraction-runs")

    assert again.status_code == 200
    assert again.json()["id"] == run.id
    assert dispatched == []


@pytest.mark.parametrize("status", ["completed", "failed", "canceled"])
async def test_finished_run_allows_new_run(client, db_session, dispatched, status):
    document_id = await make_document(db_session)
    first = await client.post(f"/api/v1/documents/{document_id}/extraction-runs")
    run = await db_session.get(ProfileExtractionRun, first.json()["id"])
    run.status = status
    await db_session.commit()

    again = await client.post(f"/api/v1/documents/{document_id}/extraction-runs")

    assert again.status_code == 202
    assert again.json()["id"] != run.id
    assert len(dispatched) == 2


async def test_other_documents_are_independent(client, db_session, dispatched):
    first_id = await make_document(db_session)
    second_id = await make_document(db_session)

    first = await client.post(f"/api/v1/documents/{first_id}/extraction-runs")
    second = await client.post(f"/api/v1/documents/{second_id}/extraction-runs")

    assert (first.status_code, second.status_code) == (202, 202)
    assert len(dispatched) == 2


async def test_concurrent_submits_create_one_run(client, db_session, dispatched):
    document_id = await make_document(db_session)
    url = f"/api/v1/documents/{document_id}/extraction-runs"

    responses = await asyncio.gather(*(client.post(url) for _ in range(8)))

    assert sorted(r.status_code for r in responses) == [200] * 7 + [202]
    assert len({r.json()["id"] for r in responses}) == 1
    assert len(dispatched) == 1
    assert await run_count(db_session) == 1


async def test_admin_start_button_is_idempotent(db_session, monkeypatch, dispatched):
    document_id = await make_document(db_session)
    async with await _admin_client(monkeypatch) as admin:
        responses = await asyncio.gather(
            *(admin.post(f"/admin/documents/{document_id}/extract") for _ in range(5))
        )

    assert {r.status_code for r in responses} == {303}
    assert len(dispatched) == 1
    assert await run_count(db_session) == 1


async def test_failed_run_retry_still_creates_new_run(client, db_session, dispatched):
    document_id = await make_document(db_session)
    first = await client.post(f"/api/v1/documents/{document_id}/extraction-runs")
    run = await db_session.get(ProfileExtractionRun, first.json()["id"])
    run.status = "failed"
    await db_session.commit()

    retried = await client.post(f"/api/v1/extraction-runs/{run.id}/retry")

    assert retried.status_code == 202
    assert retried.json()["retry_of_run_id"] == run.id
    assert retried.json()["attempt_number"] == 2


async def test_retry_scheduled_run_cannot_be_retried_manually(
    client, db_session, dispatched
):
    document_id = await make_document(db_session)
    first = await client.post(f"/api/v1/documents/{document_id}/extraction-runs")
    run = await db_session.get(ProfileExtractionRun, first.json()["id"])
    run.status = "retry_scheduled"
    await db_session.commit()
    dispatched.clear()

    response = await client.post(f"/api/v1/extraction-runs/{run.id}/retry")

    assert response.status_code == 409
    assert dispatched == []
    assert await run_count(db_session) == 1
