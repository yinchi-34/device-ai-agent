import asyncio

import pytest
from sqlalchemy import func, select

from app.modules.document import admin_router
from app.modules.document import router as document_router
from app.modules.document.models import ProfileExtractionRun, SourceDocument
from tests.api.test_admin_review import _admin_client


async def make_document(db_session, name="manual.pdf"):
    document = SourceDocument(
        original_file_name=name,
        media_type="application/pdf",
        file_size_bytes=10,
        file_hash_sha256="f" * 64,
        storage_uri=f"original/{name}",
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


async def count_runs(db_session, document_id):
    db_session.expire_all()
    return await db_session.scalar(
        select(func.count(ProfileExtractionRun.id)).where(
            ProfileExtractionRun.source_document_id == document_id
        )
    )


@pytest.fixture
def dispatched(monkeypatch):
    calls = []
    monkeypatch.setattr(document_router, "dispatch_extraction_task", calls.append)
    monkeypatch.setattr(admin_router, "dispatch_extraction_task", calls.append)
    return calls


async def test_repeated_submit_returns_same_run_without_dispatch(
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
    assert await count_runs(db_session, document.id) == 1


@pytest.mark.parametrize(
    "status",
    [
        "queued",
        "validating",
        "parsing",
        "extracting",
        "retry_scheduled",
        "validating_output",
        "awaiting_review",
        "importing",
    ],
)
async def test_unfinished_run_is_returned(client, db_session, dispatched, status):
    document = await make_document(db_session)
    existing = await add_run(db_session, document, status)
    await db_session.commit()

    response = await client.post(f"/api/v1/documents/{document.id}/extraction-runs")

    assert response.status_code == 200
    assert response.json()["id"] == existing.id
    assert dispatched == []
    assert await count_runs(db_session, document.id) == 1


@pytest.mark.parametrize("status", ["completed", "failed", "canceled"])
async def test_finished_run_allows_new_run(client, db_session, dispatched, status):
    document = await make_document(db_session)
    existing = await add_run(db_session, document, status)
    await db_session.commit()

    response = await client.post(f"/api/v1/documents/{document.id}/extraction-runs")

    assert response.status_code == 202
    assert response.json()["id"] != existing.id
    assert dispatched == [response.json()["id"]]


async def test_active_run_of_other_document_does_not_block(
    client, db_session, dispatched
):
    document = await make_document(db_session, "a.pdf")
    other = await make_document(db_session, "b.pdf")
    await add_run(db_session, other, "parsing")
    await db_session.commit()

    response = await client.post(f"/api/v1/documents/{document.id}/extraction-runs")

    assert response.status_code == 202
    assert len(dispatched) == 1


async def test_concurrent_submits_create_one_run(client, db_session, dispatched):
    document = await make_document(db_session)
    await db_session.commit()
    url = f"/api/v1/documents/{document.id}/extraction-runs"

    responses = await asyncio.gather(*(client.post(url) for _ in range(8)))

    assert sorted(r.status_code for r in responses) == [200] * 7 + [202]
    assert len({r.json()["id"] for r in responses}) == 1
    assert len(dispatched) == 1
    assert await count_runs(db_session, document.id) == 1


async def test_missing_document_still_404(client, dispatched):
    response = await client.post("/api/v1/documents/999/extraction-runs")

    assert response.status_code == 404
    assert dispatched == []


async def test_admin_start_button_double_click_dispatches_once(
    db_session, monkeypatch, dispatched
):
    document = await make_document(db_session)
    await db_session.commit()

    async with await _admin_client(monkeypatch) as admin:
        responses = [
            await admin.post(f"/admin/documents/{document.id}/extract")
            for _ in range(2)
        ]
        concurrent = await asyncio.gather(
            *(admin.post(f"/admin/documents/{document.id}/extract") for _ in range(3))
        )

    assert all(r.status_code == 303 for r in [*responses, *concurrent])
    assert len(dispatched) == 1
    assert await count_runs(db_session, document.id) == 1


async def test_manual_retry_of_failed_run_unchanged(client, db_session, dispatched):
    document = await make_document(db_session)
    failed = await add_run(db_session, document, "failed")
    await db_session.commit()

    response = await client.post(f"/api/v1/extraction-runs/{failed.id}/retry")

    assert response.status_code == 202
    assert response.json()["retry_of_run_id"] == failed.id
    assert response.json()["attempt_number"] == 2
    assert dispatched == [response.json()["id"]]


async def test_manual_retry_of_retry_scheduled_run_conflicts(
    client, db_session, dispatched
):
    document = await make_document(db_session)
    scheduled = await add_run(db_session, document, "retry_scheduled")
    await db_session.commit()

    response = await client.post(f"/api/v1/extraction-runs/{scheduled.id}/retry")

    assert response.status_code == 409
    assert dispatched == []
    assert await count_runs(db_session, document.id) == 1
