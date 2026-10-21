import asyncio

import pytest
from sqlalchemy import func, select

from app.modules.document import admin_router
from app.modules.document import router as document_router
from app.modules.document.models import ProfileExtractionRun, SourceDocument
from tests.api.test_admin_review import _admin_client


async def make_document(db_session):
    document = SourceDocument(
        original_file_name="manual.docx",
        media_type=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        file_size_bytes=10,
        file_hash_sha256="a" * 64,
        storage_uri="original/manual.docx",
        validation_status="validated",
    )
    db_session.add(document)
    await db_session.flush()
    return document


async def add_run(db_session, document, status):
    run = ProfileExtractionRun(
        source_document_id=document.id,
        status=status,
        extraction_provider="fake",
        extraction_model="extract-v1",
        embedding_provider="fake",
        embedding_model="embed-v1",
    )
    db_session.add(run)
    await db_session.flush()
    return run


async def run_count(db_session):
    return await db_session.scalar(select(func.count(ProfileExtractionRun.id)))


@pytest.fixture
def dispatched(monkeypatch):
    calls = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", calls.append)
    monkeypatch.setattr(admin_router, "dispatch_extraction_task", calls.append)
    return calls


async def test_repeated_start_returns_same_run_and_dispatches_once(
    client, db_session, dispatched
):
    document = await make_document(db_session)
    await db_session.commit()
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
    [
        "queued",
        "validating",
        "parsing",
        "extracting",
        "validating_output",
        "retry_scheduled",
        "awaiting_review",
        "importing",
    ],
)
async def test_unfinished_run_is_returned(client, db_session, dispatched, status):
    document = await make_document(db_session)
    run = await add_run(db_session, document, status)
    await db_session.commit()

    response = await client.post(f"/api/v1/documents/{document.id}/extraction-runs")

    assert response.status_code == 200
    assert response.json()["id"] == run.id
    assert dispatched == []
    assert await run_count(db_session) == 1


@pytest.mark.parametrize("status", ["completed", "failed", "canceled"])
async def test_finished_run_allows_new_run(client, db_session, dispatched, status):
    document = await make_document(db_session)
    old = await add_run(db_session, document, status)
    await db_session.commit()

    response = await client.post(f"/api/v1/documents/{document.id}/extraction-runs")

    assert response.status_code == 202
    assert response.json()["id"] != old.id
    assert response.json()["status"] == "queued"
    assert dispatched == [response.json()["id"]]


async def test_runs_of_other_documents_do_not_block(client, db_session, dispatched):
    other = await make_document(db_session)
    await add_run(db_session, other, "queued")
    document = await make_document(db_session)
    await db_session.commit()

    response = await client.post(f"/api/v1/documents/{document.id}/extraction-runs")

    assert response.status_code == 202
    assert dispatched == [response.json()["id"]]


async def test_start_unknown_document_is_404(client, dispatched):
    response = await client.post("/api/v1/documents/999/extraction-runs")

    assert response.status_code == 404
    assert dispatched == []


async def test_concurrent_starts_create_one_run(client, db_session, dispatched):
    document = await make_document(db_session)
    await db_session.commit()
    url = f"/api/v1/documents/{document.id}/extraction-runs"

    responses = await asyncio.gather(*(client.post(url) for _ in range(8)))

    assert sorted(response.status_code for response in responses) == [200] * 7 + [202]
    assert len({response.json()["id"] for response in responses}) == 1
    assert len(dispatched) == 1
    assert await run_count(db_session) == 1


async def test_failed_run_retry_still_creates_new_run(client, db_session, dispatched):
    document = await make_document(db_session)
    failed = await add_run(db_session, document, "failed")
    await db_session.commit()

    response = await client.post(f"/api/v1/extraction-runs/{failed.id}/retry")

    assert response.status_code == 202
    assert response.json()["id"] != failed.id
    assert dispatched == [response.json()["id"]]


async def test_retry_scheduled_run_cannot_be_retried_manually(
    client, db_session, dispatched
):
    document = await make_document(db_session)
    scheduled = await add_run(db_session, document, "retry_scheduled")
    await db_session.commit()

    response = await client.post(f"/api/v1/extraction-runs/{scheduled.id}/retry")

    assert response.status_code == 409
    assert dispatched == []
    assert await run_count(db_session) == 1


async def test_admin_start_button_is_idempotent(db_session, dispatched, monkeypatch):
    document = await make_document(db_session)
    await db_session.commit()

    async with await _admin_client(monkeypatch) as admin:
        responses = await asyncio.gather(
            *(admin.post(f"/admin/documents/{document.id}/extract") for _ in range(4))
        )

    assert all(response.status_code == 303 for response in responses)
    assert await run_count(db_session) == 1
    assert len(dispatched) == 1
